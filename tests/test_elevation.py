"""Elevation v2: the arma-grant script AND the router that reads what it writes.

Run:  python3 -m unittest -v tests/test_elevation.py     (or: pytest tests/)
      Run as your normal user: arma-grant refuses to run as root, by design.

The script is exercised as a real subprocess under a throwaway $HOME, invoked through the
same two names it is installed as (arma-elevate / arma-revoke symlinks), because argv[0]
dispatch is part of what is being tested. The router tests point config.ELEVATION_PATH at
that same throwaway home, so they read exactly what the script wrote -- the file format is
the contract between the two, and testing either side alone would not prove it holds.

Mutation-checked 2026-11-23: reverting router.resolve() to the pre-fix condition
(`if root is None:`) fails the three upgrade tests; with the fix all 29 pass.

Design: Substrate dom1/context/blocks/domain1-arma-exec.md ("Elevation v2", "BUG to fix").
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from armamentarium.mcp_router import config as cfg          # noqa: E402
from armamentarium.mcp_router.config import Root, RouterConfig  # noqa: E402
from armamentarium.mcp_router.router import (                # noqa: E402
    AccessDeniedError, FilesystemRouter, PathNotAllowedError,
)

SCRIPT = REPO / "bin" / "arma-grant"
ME = socket.gethostname().split(".")[0].lower()


class Home:
    """A throwaway $HOME with arma-elevate / arma-revoke symlinked to the script."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name in ("arma-elevate", "arma-revoke"):
            (self.bin / name).symlink_to(SCRIPT)
        self.elevate = self.home / ".config" / "armamentarium" / "elevate.json"
        self.ledger = self.home / ".local" / "state" / "armamentarium" / "grants.log"

    def run(self, name, *args):
        env = dict(os.environ, HOME=str(self.home))
        return subprocess.run([sys.executable, str(self.bin / name), *args],
                              env=env, capture_output=True, text=True)

    def grants(self):
        if not self.elevate.exists():
            return []
        d = json.loads(self.elevate.read_text())
        assert d["version"] == 2
        return d["grants"]

    def ledger_lines(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text().splitlines() if l.strip()]

    def write_raw(self, data):
        self.elevate.parent.mkdir(parents=True, exist_ok=True)
        self.elevate.write_text(json.dumps(data))

    def close(self):
        self.tmp.cleanup()


@unittest.skipIf(os.geteuid() == 0, "arma-grant refuses to run as root; run tests as yourself")
class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.h = Home()

    def tearDown(self):
        self.h.close()

    def test_path_defaults_rw_1h_on_this_host(self):
        r = self.h.run("arma-elevate", "path", str(self.h.home))
        self.assertEqual(r.returncode, 0, r.stderr)
        [g] = self.h.grants()
        self.assertEqual((g["kind"], g["access"], g["host"], g["granted_on"]),
                         ("path", "rw", ME, ME))
        self.assertAlmostEqual(g["expires_at"] - g["granted_at"], 3600, delta=2)

    def test_v1_form_still_works(self):
        r = self.h.run("arma-elevate", str(self.h.home), "r", "120")
        self.assertEqual(r.returncode, 0, r.stderr)
        [g] = self.h.grants()
        self.assertEqual((g["kind"], g["access"]), ("path", "r"))
        self.assertAlmostEqual(g["expires_at"] - g["granted_at"], 120, delta=2)

    def test_exec_defaults_15m_and_LOCAL_host_not_hecate(self):
        r = self.h.run("arma-elevate", "exec", "git-push")
        self.assertEqual(r.returncode, 0, r.stderr)
        [g] = self.h.grants()
        self.assertEqual((g["kind"], g["rule"], g["host"], g["granted_on"]),
                         ("exec", "git-push", ME, ME))
        self.assertAlmostEqual(g["expires_at"] - g["granted_at"], 900, delta=2)

    def test_exec_host_and_ttl_in_either_order(self):
        self.h.run("arma-elevate", "exec", "git-push", "haphaestus", "30m")
        self.h.run("arma-elevate", "exec", "pr-create", "10m", "Haphaestus")
        g = {x["rule"]: x for x in self.h.grants()}
        self.assertEqual(g["git-push"]["host"], "haphaestus")
        self.assertEqual(g["git-push"]["granted_on"], ME)
        self.assertEqual(g["pr-create"]["host"], "haphaestus")      # lowercased
        self.assertAlmostEqual(g["pr-create"]["expires_at"] - g["pr-create"]["granted_at"],
                               600, delta=2)

    def test_exec_rejects_a_raw_command(self):
        r = self.h.run("arma-elevate", "exec", "rm -rf /")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a rule name", r.stderr)
        self.assertEqual(self.h.grants(), [])

    def test_ttl_units_and_4h_clamp(self):
        r = self.h.run("arma-elevate", "path", str(self.h.home), "9h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("clamped", r.stderr)
        [g] = self.h.grants()
        self.assertAlmostEqual(g["expires_at"] - g["granted_at"], 4 * 3600, delta=2)

    def test_grants_coexist(self):
        self.h.run("arma-elevate", "path", str(self.h.home), "r")
        self.h.run("arma-elevate", "exec", "git-push")
        self.assertEqual(len(self.h.grants()), 2)

    def test_same_target_replaces_and_ledgers_the_old_one(self):
        self.h.run("arma-elevate", "path", str(self.h.home), "r")
        self.h.run("arma-elevate", "path", str(self.h.home), "rw")
        [g] = self.h.grants()
        self.assertEqual(g["access"], "rw")
        [old] = self.h.ledger_lines()
        self.assertEqual((old["end_reason"], old["access"]), ("replaced", "r"))

    def test_bare_revoke_refuses_and_lists(self):
        self.h.run("arma-elevate", "exec", "git-push")
        r = self.h.run("arma-revoke")
        self.assertEqual(r.returncode, 2)
        self.assertIn("git-push", r.stderr)
        self.assertEqual(len(self.h.grants()), 1)          # nothing was revoked

    def test_revoke_one_keeps_the_others(self):
        self.h.run("arma-elevate", "path", str(self.h.home), "r")
        self.h.run("arma-elevate", "exec", "git-push")
        r = self.h.run("arma-revoke", "exec", "git-push")
        self.assertEqual(r.returncode, 0, r.stderr)
        [left] = self.h.grants()
        self.assertEqual(left["kind"], "path")
        [rec] = self.h.ledger_lines()
        self.assertEqual((rec["rule"], rec["end_reason"]), ("git-push", "revoked"))

    def test_revoke_last_grant_removes_the_file(self):
        self.h.run("arma-elevate", "exec", "git-push")
        self.h.run("arma-revoke", "exec", "git-push")
        self.assertFalse(self.h.elevate.exists())

    def test_revoke_all_ledgers_everything_then_removes_file(self):
        self.h.run("arma-elevate", "path", str(self.h.home), "r")
        self.h.run("arma-elevate", "exec", "git-push", "haphaestus")
        r = self.h.run("arma-revoke", "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.h.elevate.exists())
        reasons = sorted(x["end_reason"] for x in self.h.ledger_lines())
        self.assertEqual(reasons, ["revoke-all", "revoke-all"])

    def test_revoke_nonexistent_is_an_error_not_a_silent_noop(self):
        r = self.h.run("arma-revoke", "exec", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no matching", r.stderr)

    def test_list_active_only_unless_expired_is_asked_for(self):
        now = time.time()
        self.h.write_raw({"version": 2, "grants": [
            {"kind": "exec", "rule": "old", "host": ME, "granted_on": ME,
             "granted_at": now - 100, "expires_at": now - 10},
            {"kind": "exec", "rule": "fresh", "host": ME, "granted_on": ME,
             "granted_at": now, "expires_at": now + 600},
        ]})
        r = self.h.run("arma-elevate", "list")
        self.assertIn("fresh", r.stdout)
        self.assertNotIn("old", r.stdout)
        # ...and `list` pruned the expired one into the ledger
        self.assertEqual([g["rule"] for g in self.h.grants()], ["fresh"])
        [rec] = self.h.ledger_lines()
        self.assertEqual((rec["rule"], rec["end_reason"]), ("old", "expired"))
        r = self.h.run("arma-elevate", "list", "expired")
        self.assertIn("old", r.stdout)
        self.assertIn("expired", r.stdout)
        self.assertNotIn("fresh", r.stdout)

    def test_v1_file_is_migrated_not_lost(self):
        self.h.write_raw({"roots": [{"path": "/srv", "access": "r"}],
                          "expires_at": time.time() + 600})
        self.h.run("arma-elevate", "exec", "git-push")
        kinds = sorted(g["kind"] for g in self.h.grants())
        self.assertEqual(kinds, ["exec", "path"])

    def test_garbled_file_is_left_alone(self):
        self.h.elevate.parent.mkdir(parents=True)
        self.h.elevate.write_text("{not json")
        r = self.h.run("arma-elevate", "exec", "git-push")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.h.elevate.read_text(), "{not json")

    def test_symlinked_elevate_json_refused(self):
        self.h.elevate.parent.mkdir(parents=True)
        target = self.h.home / "elsewhere.json"
        target.write_text("{}")
        self.h.elevate.symlink_to(target)
        r = self.h.run("arma-elevate", "exec", "git-push")
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(target.read_text(), "{}")

    def test_files_are_private(self):
        self.h.run("arma-elevate", "exec", "git-push")
        self.h.run("arma-revoke", "exec", "git-push")
        self.h.run("arma-elevate", "exec", "git-push")
        self.assertEqual(self.h.elevate.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.h.ledger.stat().st_mode & 0o777, 0o600)


@unittest.skipIf(os.geteuid() == 0, "arma-grant refuses to run as root; run tests as yourself")
class RouterTests(unittest.TestCase):
    """The router reading what the script wrote, including the r->rw upgrade fix."""

    def setUp(self):
        self.h = Home()
        self.base = self.h.home / "data"
        (self.base / "sub").mkdir(parents=True)
        (self.base / "other").mkdir()
        self._saved = cfg.ELEVATION_PATH
        cfg.ELEVATION_PATH = self.h.elevate

    def tearDown(self):
        cfg.ELEVATION_PATH = self._saved
        self.h.close()

    def router(self, access):
        root = Root(path=os.path.realpath(self.base), access=access, raw=str(self.base))
        return FilesystemRouter(RouterConfig(roots=[root]))

    def write(self, r, rel):
        return r.resolve(str(self.base / rel), for_write=True)

    def test_base_r_plus_elevated_rw_allows_write_then_revoke_closes_it(self):
        r = self.router("r")
        with self.assertRaises(AccessDeniedError):
            self.write(r, "f")
        self.h.run("arma-elevate", "path", str(self.base), "rw", "10m")
        _, root = self.write(r, "f")
        self.assertTrue(root.writable)
        self.h.run("arma-revoke", "path", str(self.base))
        with self.assertRaises(AccessDeniedError):
            self.write(r, "f")

    def test_upgrade_ends_at_expiry(self):
        r = self.router("r")
        now = time.time()
        self.h.write_raw({"version": 2, "grants": [
            {"kind": "path", "target": str(self.base), "access": "rw", "host": ME,
             "granted_on": ME, "granted_at": now - 100, "expires_at": now - 1}]})
        with self.assertRaises(AccessDeniedError):
            self.write(r, "f")

    def test_elevation_never_downgrades(self):
        r = self.router("rw")
        self.h.run("arma-elevate", "path", str(self.base), "r", "10m")
        _, root = self.write(r, "f")
        self.assertTrue(root.writable)

    def test_subpath_upgrade_is_scoped_to_the_subpath(self):
        r = self.router("r")
        self.h.run("arma-elevate", "path", str(self.base / "sub"), "rw", "10m")
        _, root = self.write(r, "sub/f")
        self.assertTrue(root.writable)
        with self.assertRaises(AccessDeniedError):
            self.write(r, "other/f")

    def test_reads_unchanged_by_elevation(self):
        r = self.router("r")
        _, root = r.resolve(str(self.base / "sub"))
        self.assertFalse(root.writable)

    def test_elevation_still_ADDS_an_unlisted_root(self):
        r = FilesystemRouter(RouterConfig(roots=[]))
        with self.assertRaises(PathNotAllowedError):
            r.resolve(str(self.base))
        self.h.run("arma-elevate", "path", str(self.base), "r", "10m")
        r.resolve(str(self.base))                                   # no raise
        with self.assertRaises(AccessDeniedError):
            r.resolve(str(self.base / "f"), for_write=True)          # r grant, so no write

    def test_v1_file_still_honoured_by_router(self):
        self.h.write_raw({"roots": [{"path": str(self.base), "access": "rw"}],
                          "expires_at": time.time() + 600})
        _, root = self.write(self.router("r"), "f")
        self.assertTrue(root.writable)

    def test_other_hosts_path_grant_ignored(self):
        now = time.time()
        self.h.write_raw({"version": 2, "grants": [
            {"kind": "path", "target": str(self.base), "access": "rw", "host": "not-" + ME,
             "granted_on": ME, "granted_at": now, "expires_at": now + 600}]})
        with self.assertRaises(AccessDeniedError):
            self.write(self.router("r"), "f")

    def test_exec_grants_never_open_paths(self):
        self.h.run("arma-elevate", "exec", "git-push")
        with self.assertRaises(PathNotAllowedError):
            FilesystemRouter(RouterConfig(roots=[])).resolve(str(self.base))

    def test_status_reports_each_grant_with_its_own_clock(self):
        self.h.run("arma-elevate", "path", str(self.base), "r", "10m")
        self.h.run("arma-elevate", "exec", "git-push", "5m")
        st = self.router("r").status()["elevation"]
        self.assertTrue(st["active"])
        left = {g.get("rule") or "path": g["expires_in_seconds"] for g in st["grants"]}
        self.assertAlmostEqual(left["path"], 600, delta=3)
        self.assertAlmostEqual(left["git-push"], 300, delta=3)

    def test_garbled_entry_fails_closed(self):
        self.h.write_raw({"version": 2, "grants": [
            "junk", {"kind": "path"}, {"kind": "path", "target": str(self.base),
                                       "access": "superuser", "host": ME,
                                       "expires_at": time.time() + 600}]})
        with self.assertRaises(AccessDeniedError):
            self.write(self.router("r"), "f")


if __name__ == "__main__":
    unittest.main()
