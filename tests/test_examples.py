"""The shipped config examples must load with the real loaders and behave as documented,
so the README and examples can't drift from the code. Pure; runs nothing."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from armamentarium.exec_policy import decide, load_policy

CONFIG = Path(__file__).resolve().parent.parent / "config"


class TestExecExample(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(os.path.realpath(self.tmp.name))
        self.repo = t / "projects" / "someone" / "repo"   # the example's ~/projects/*/*
        self.repo.mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(t)})
        self.env.start()
        p = t / "exec.json"
        shutil.copy(CONFIG / "exec.example.json", p)
        os.chmod(p, 0o644)   # the loader refuses group/world-writable policies
        self.policy = load_policy(p)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_is_valid_json_with_notes(self):
        data = json.loads((CONFIG / "exec.example.json").read_text())
        self.assertIn("_notes", data)

    def test_reads_allowed(self):
        for argv in (["uptime"], ["df", "-h"], ["ping", "-c", "2", "example.org"],
                     ["journalctl", "--no-pager", "-b", "-n", "5"]):
            self.assertTrue(decide(argv, "anyhost", self.policy, []).allowed, argv)

    def test_shell_and_unlisted_refused(self):
        for argv in (["bash", "-c", "true"], ["sh"], ["rm", "-rf", "/tmp/x"],
                     ["journalctl", "--no-pager", "--vacuum-time=1s"]):
            self.assertFalse(decide(argv, "anyhost", self.policy, []).allowed, argv)

    def test_status_allowed_push_needs_grant_and_says_how(self):
        repo = str(self.repo)
        self.assertTrue(decide(["git", "-C", repo, "status", "-s"], "anyhost",
                               self.policy, []).allowed)
        d = decide(["git", "-C", repo, "push"], "anyhost", self.policy, [])
        self.assertFalse(d.allowed)
        self.assertIn("arma-elevate exec git-push", d.hint)


if __name__ == "__main__":
    unittest.main()
