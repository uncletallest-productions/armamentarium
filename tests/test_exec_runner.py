"""Tests for exec_runner (real subprocesses, sandboxed in temp dirs) and the router's
protected-path write denial. Run as a NON-root user."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from armamentarium import exec_runner
from armamentarium.exec_runner import SAFE_PATH, execute, harden
from armamentarium.mcp_router.config import Root, RouterConfig, local_host
from armamentarium.mcp_router.router import AccessDeniedError, FilesystemRouter

ME = local_host()


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(os.path.realpath(self.tmp.name))
        self.home = self.t / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Test\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n")
        self.repo = self.t / "repos" / "r"
        self.repo.mkdir(parents=True)
        self.log = self.t / "exec.log"
        self.patches = [mock.patch.object(exec_runner, "EXEC_LOG", self.log),
                        mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                     "ARMA_TEST_SECRET": "hunter2"})]
        for p in self.patches:
            p.start()
        self.policy = self.t / "exec.json"
        self.policy.write_text(json.dumps({
            "version": 1,
            "roots": {"git": [str(self.t / "repos" / "*")],
                      "zero": {"patterns": ["/dev/zero"], "resolve": False}},
            "rules": {
                "true": {"mode": "always", "argv": ["true"]},
                "false": {"mode": "always", "argv": ["false"]},
                "env": {"mode": "always", "argv": ["env"]},
                "sleep": {"mode": "always", "argv": ["sleep"], "args": ["{int}"], "timeout": 1},
                "head": {"mode": "always", "argv": ["head", "-c", "{int}"],
                         "args": ["{path:zero}"], "max_output": 100},
                "elev": {"mode": "elevated", "argv": ["true", "elevated"]},
                "git-status": {"mode": "always", "argv": ["git", "-C", "{path:git}", "status"],
                               "options": {"-s": None}},
                "git-add": {"mode": "always", "argv": ["git", "-C", "{path:git}", "add", "-A"]},
                "git-commit": {"mode": "always", "argv": ["git", "-C", "{path:git}", "commit"],
                               "options": {"-m": "{text}"}},
            }}))
        self.policy.chmod(0o644)   # independent of the caller's umask

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_(self, argv, **kw):
        return execute(argv, policy_path=self.policy, grants=kw.pop("grants", []), **kw)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                       env={**os.environ, "PATH": SAFE_PATH})

    def logged(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]


class RunnerTests(RunnerBase):
    def test_allowed_runs(self):
        r = self.run_(["true"])
        self.assertTrue(r.allowed)
        self.assertEqual(r.exit_code, 0)
        self.assertEqual(r.ran[0], shutil.which("true", path=SAFE_PATH))

    def test_failure_surfaces_exit(self):
        r = self.run_(["false"])
        self.assertTrue(r.allowed)
        self.assertEqual(r.exit_code, 1)
        self.assertIn("exited 1", r.reason)

    def test_refused_never_runs_and_is_logged(self):
        r = self.run_(["rm", "-rf", str(self.t)])
        self.assertFalse(r.allowed)
        self.assertTrue(self.t.exists())
        last = self.logged()[-1]
        self.assertFalse(last["allowed"])
        self.assertEqual(last["argv"][0], "rm")

    def test_elevated_hint_passthrough(self):
        r = self.run_(["true", "elevated"])
        self.assertFalse(r.allowed)
        self.assertIn("arma-elevate exec elev", r.hint)
        g = [{"kind": "exec", "rule": "elev", "host": ME, "expires_at": time.time() + 60}]
        self.assertTrue(self.run_(["true", "elevated"], grants=g).allowed)

    def test_remote_refused(self):
        r = self.run_(["true"], host="definitely-not-" + ME)
        self.assertFalse(r.allowed)
        self.assertIn("not built yet", r.reason)

    def test_missing_policy_fails_closed(self):
        r = execute(["true"], policy_path=self.t / "none.json", grants=[])  # missing file
        self.assertFalse(r.allowed)
        self.assertIn("nothing may run", r.reason)

    def test_env_is_minimal(self):
        r = self.run_(["env"])
        env = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        self.assertEqual(env["PATH"], SAFE_PATH)
        self.assertNotIn("ARMA_TEST_SECRET", env)
        self.assertEqual(env["HOME"], str(self.home))

    def test_path_shadowing_ignored(self):
        fake = self.t / "evilbin"
        fake.mkdir()
        (fake / "true").write_text("#!/bin/sh\necho PWNED\n")
        (fake / "true").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ.get('PATH', '')}"}):
            r = self.run_(["true"])
        self.assertNotIn("PWNED", r.stdout)
        self.assertFalse(r.ran[0].startswith(str(fake)))

    def test_timeout_kills(self):
        t0 = time.time()
        r = self.run_(["sleep", "30"])
        self.assertTrue(r.timed_out)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("KILLED", r.reason)

    def test_output_capped(self):
        r = self.run_(["head", "-c", "5000", "/dev/zero"])
        self.assertTrue(r.truncated)
        self.assertEqual(len(r.stdout), 100)

    def test_runs_checked_path_not_typed_one(self):
        self.git("init", "-q")
        r = self.run_(["git", "-C", str(self.repo) + "/.", "status", "-s"])
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertIn(str(self.repo), r.ran)
        self.assertNotIn(str(self.repo) + "/.", r.ran)

    def test_tilde_path_runs(self):
        link = self.home / "r"
        os.symlink(self.repo, link)   # ~/r -> repo, inside the git root once resolved
        self.git("init", "-q")
        r = self.run_(["git", "-C", "~/r", "status", "-s"])
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertIn(str(self.repo), r.ran)

    def test_long_args_clipped_in_log(self):
        self.git("init", "-q")
        (self.repo / "f").write_text("x")
        self.run_(["git", "-C", str(self.repo), "add", "-A"])
        self.run_(["git", "-C", str(self.repo), "commit", "-m", "m" * 1000])
        self.assertLess(len(self.logged()[-1]["argv"][-1]), 300)


class DetachedChildTests(RunnerBase):
    """A helper that leaves a detached child holding stdout/stderr open (the ssh ProxyCommand
    case) must not hang the runner -- neither on success nor after a timeout."""

    def setUp(self):
        super().setUp()
        self.bin = self.t / "bin"
        self.bin.mkdir()
        for name, body in {
            "leaky-ok": "(setsid sleep 30 &)\necho done\n",
            "leaky-slow": "(setsid sleep 30 &)\nsleep 30\n",
            "noisy": "head -c 300000 /dev/zero | tr '\\0' x\necho\n",
        }.items():
            f = self.bin / name
            f.write_text("#!/bin/sh\n" + body)
            f.chmod(0o755)
        pol = json.loads(self.policy.read_text())
        pol["rules"].update({
            "leaky-ok": {"mode": "always", "argv": ["leaky-ok"], "timeout": 20},
            "leaky-slow": {"mode": "always", "argv": ["leaky-slow"], "timeout": 2},
            "noisy": {"mode": "always", "argv": ["noisy"], "max_output": 1000},
        })
        self.policy.write_text(json.dumps(pol))
        self.policy.chmod(0o644)
        p = mock.patch.object(exec_runner, "SAFE_PATH", f"{self.bin}:{SAFE_PATH}")
        p.start()
        self.addCleanup(p.stop)

    def test_detached_child_after_success(self):
        t0 = time.time()
        r = self.run_(["leaky-ok"])
        self.assertLess(time.time() - t0, 10, "runner waited on a detached child's pipes")
        self.assertEqual(r.exit_code, 0)
        self.assertIn("done", r.stdout)

    def test_detached_child_after_timeout(self):
        t0 = time.time()
        r = self.run_(["leaky-slow"])
        self.assertLess(time.time() - t0, 15, "runner hung after the timeout kill")
        self.assertTrue(r.timed_out)

    def test_big_output_drained_and_capped(self):
        r = self.run_(["noisy"])
        self.assertEqual(r.exit_code, 0)
        self.assertTrue(r.truncated)
        self.assertEqual(len(r.stdout), 1000)


class GitHardeningTests(RunnerBase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.marker = self.t / "PWNED"
        evil = self.t / "evil.sh"
        evil.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 0\n")
        evil.chmod(0o755)
        self.evil = evil

    def test_harden_shape(self):
        h = harden(["git", "-C", "/r", "diff", "--stat"])
        self.assertEqual(h[0], "git")
        self.assertIn("core.fsmonitor=false", h)
        i = h.index("diff")
        self.assertEqual(h[i - 2:i], ["-C", "/r"])
        self.assertEqual(h[i + 1:i + 3], ["--no-ext-diff", "--no-textconv"])
        self.assertEqual(harden(["true"]), ["true"])

    def test_fsmonitor_not_run(self):
        self.git("config", "core.fsmonitor", str(self.evil))
        r = self.run_(["git", "-C", str(self.repo), "status", "-s"])
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertFalse(self.marker.exists(), "repo core.fsmonitor was executed")

    def test_repo_hook_not_run(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        shutil.copy(self.evil, hook)
        (self.repo / "f").write_text("x")
        self.run_(["git", "-C", str(self.repo), "add", "-A"])
        r = self.run_(["git", "-C", str(self.repo), "commit", "-m", "agent commit"])
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertFalse(self.marker.exists(), "repo pre-commit hook was executed")

    def test_repo_hookspath_overridden(self):
        hooks = self.repo / "tests" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy(self.evil, hooks / "pre-commit")
        self.git("config", "core.hooksPath", "tests/hooks")
        (self.repo / "f").write_text("x")
        self.run_(["git", "-C", str(self.repo), "add", "-A"])
        r = self.run_(["git", "-C", str(self.repo), "commit", "-m", "agent commit"])
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertFalse(self.marker.exists(), "repo core.hooksPath hook was executed")


class ProtectedWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.t, "repo", ".git", "hooks"))
        self.router = FilesystemRouter(RouterConfig(roots=[Root(self.t, "rw", self.t)]))

    def tearDown(self):
        self.tmp.cleanup()

    def denied(self, rel):
        with self.assertRaises(AccessDeniedError) as cm:
            self.router.resolve(os.path.join(self.t, rel), for_write=True)
        self.assertIn("never", str(cm.exception))

    def test_git_internals_denied(self):
        self.denied("repo/.git/config")
        self.denied("repo/.git/hooks/pre-commit")
        self.denied("repo/.git")              # incl. a `.git` FILE (gitdir: pointer)
        self.denied("other/.git")

    def test_hook_dirs_denied(self):
        self.denied("repo/tests/hooks/pre-commit")
        self.denied("repo/tests/hooks")
        self.denied("repo/.githooks/pre-push")

    def test_ordinary_writes_and_reads_ok(self):
        for rel in ("repo/src/x.py", "repo/tests/test_hooks.py", "repo/.gitignore",
                    "repo/.gitattributes"):
            self.router.resolve(os.path.join(self.t, rel), for_write=True)
        # reading .git is still fine
        self.router.resolve(os.path.join(self.t, "repo/.git/config"), for_write=False)


if __name__ == "__main__":
    unittest.main()
