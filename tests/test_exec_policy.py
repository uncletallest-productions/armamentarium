"""Tests for armamentarium.exec_policy -- matcher, validation, grants. Pure; runs nothing."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from armamentarium.exec_policy import PolicyError, decide, load_policy

HOST = "hecate"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / "repos" / "armamentarium").mkdir(parents=True)
        (self.root / "outside").mkdir()
        os.symlink(self.root / "outside", self.root / "repos" / "escape")
        self.policy = self.make({
            "version": 1,
            "roots": {
                "git": [str(self.root / "repos" / "*")],
                "disk": {"patterns": ["/dev/disk/by-id/*"], "resolve": False},
            },
            "rules": {
                "git-status": {"mode": "always", "argv": ["git", "-C", "{path:git}", "status"],
                               "options": {"-s": None, "-b": None, "--porcelain": None}},
                "git-log": {"mode": "always", "argv": ["git", "-C", "{path:git}", "log"],
                            "options": {"--oneline": None, "-n": "{int}"}},
                "git-push": {"mode": "elevated", "argv": ["git", "-C", "{path:git}", "push"],
                             "args": ["{word?}", "{name?}"]},
                "journal": {"mode": "always", "sudo": True,
                            "argv": ["journalctl", "--no-pager"],
                            "options": {"-b": "{int?}", "-n": "{int}", "-u": "{unit}",
                                        "--since": "{time}", "-o": "short-iso|json"}},
                "unit-status": {"mode": "always",
                                "argv": ["systemctl", "status|is-active|is-failed"],
                                "options": {"--no-pager": None}, "args": ["{unit}"]},
                "smart": {"mode": "always", "sudo": True, "argv": ["smartctl", "-a|-H"],
                          "args": ["{path:disk}"]},
                "zpool-status": {"mode": "always", "hosts": ["haphaestus"],
                                 "argv": ["zpool", "status"], "args": ["{name?}"]},
                "commit": {"mode": "always", "argv": ["git", "-C", "{path:git}", "commit"],
                           "options": {"-m": "{text}"}},
                "resolve": {"mode": "always", "argv": ["resolvectl", "query"],
                            "args": ["{host}"]},
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, data):
        p = self.root / "exec.json"
        p.write_text(json.dumps(data))
        return load_policy(p)

    def ok(self, argv, host=HOST, grants=()):
        d = decide(argv, host, self.policy, list(grants))
        self.assertTrue(d.allowed, d.message())
        return d

    def no(self, argv, host=HOST, grants=(), contains=None):
        d = decide(argv, host, self.policy, list(grants))
        self.assertFalse(d.allowed, f"unexpectedly allowed: {argv} -> {d.reason}")
        if contains:
            self.assertIn(contains, d.message())
        return d

    @property
    def repo(self):
        return str(self.root / "repos" / "armamentarium")


class MatchTests(Base):
    def test_basic_always(self):
        d = self.ok(["git", "-C", self.repo, "status", "-s", "-b"])
        self.assertEqual(d.rule, "git-status")

    def test_unlisted_option_refused(self):
        self.no(["git", "-C", self.repo, "log", "--output=/tmp/x"], contains="--output")
        self.no(["git", "-C", self.repo, "log", "-p"], contains="'-p' is not allowed")

    def test_config_injection_refused(self):
        # -c before the subcommand never matches the fixed prefix
        self.no(["git", "-c", "core.sshCommand=sh", "-C", self.repo, "status"])

    def test_option_values(self):
        self.ok(["git", "-C", self.repo, "log", "-n", "5", "--oneline"])
        self.ok(["git", "-C", self.repo, "log", "-n=5"])
        self.no(["git", "-C", self.repo, "log", "-n", "five"], contains="not a valid int")
        self.no(["git", "-C", self.repo, "log", "-n"], contains="needs a value")
        self.no(["git", "-C", self.repo, "status", "-s=1"], contains="takes no value")

    def test_double_dash_refused(self):
        self.no(["git", "-C", self.repo, "status", "--"], contains="'--'")

    def test_extra_positional_refused(self):
        self.no(["git", "-C", self.repo, "status", "extra"], contains="unexpected argument")

    def test_journal_boot_optional_int(self):
        self.ok(["journalctl", "--no-pager", "-b", "-1", "-n", "50"])
        self.ok(["journalctl", "--no-pager", "-b", "-n", "50"])
        self.ok(["journalctl", "--no-pager", "-b"])
        self.ok(["journalctl", "--no-pager", "-u", "zen1-c6-disable.service", "-o", "json"])
        self.no(["journalctl", "--no-pager", "-o", "cat"], contains="expected")
        self.no(["journalctl", "--no-pager", "--vacuum-size=1M"], contains="--vacuum-size")
        self.no(["journalctl"], contains="incomplete")

    def test_since_values(self):
        self.ok(["journalctl", "--no-pager", "--since", "2 hours ago"])
        self.ok(["journalctl", "--no-pager", "--since=2026-11-23 19:40:00"])
        self.no(["journalctl", "--no-pager", "--since", "now; rm -rf /"])
        self.no(["journalctl", "--no-pager", "--since", "x" * 41])

    def test_alternation(self):
        self.ok(["systemctl", "status", "sshd.service"])
        self.ok(["systemctl", "is-failed", "nfs-server"])
        self.no(["systemctl", "restart", "sshd"], contains="expected")
        self.no(["systemctl", "status"], contains="missing argument")

    def test_name_metachar_refused(self):
        self.no(["systemctl", "status", "sshd;reboot"])
        self.no(["systemctl", "status", "$(id)"])
        self.no(["systemctl", "status", "-sshd"])     # option-shaped, not listed

    def test_host_type(self):
        self.ok(["resolvectl", "query", "haphaestus.lethe.lan"])
        self.no(["resolvectl", "query", "bad_host!"])

    def test_text_type(self):
        self.ok(["git", "-C", self.repo, "commit", "-m", "exec: matcher\n\nBody; $(not a shell)"])
        self.no(["git", "-C", self.repo, "commit", "-m", "bell\x07"])
        self.no(["git", "-C", self.repo, "commit", "-m", "x" * 4097])
        self.no(["git", "-C", self.repo, "commit", "-F", "/etc/shadow"], contains="'-F'")
        self.no(["git", "-C", self.repo, "commit", "--amend", "-m", "x"])

    def test_ref_and_dataset_types(self):
        pol = self.make({"version": 1, "rules": {
            "show": {"mode": "always", "argv": ["git", "show"], "args": ["{ref}"]},
            "zget": {"mode": "always", "argv": ["zfs", "get"], "args": ["{name}", "{dataset}"]}}})
        for r in ("HEAD~1", "main..feature", "HEAD@{1}", "v1.0^", "origin/main"):
            self.assertTrue(decide(["git", "show", r], HOST, pol, []).allowed, r)
        self.assertFalse(decide(["git", "show", "HEAD;id"], HOST, pol, []).allowed)
        self.assertTrue(decide(["zfs", "get", "compression", "reliquary/Substratum@daily"],
                               HOST, pol, []).allowed)
        self.assertFalse(decide(["zfs", "get", "all", "/reliquary"], HOST, pol, []).allowed)

    def test_unknown_program(self):
        self.no(["rm", "-rf", "/"], contains="no rule runs 'rm'")

    def test_bad_argv(self):
        self.assertFalse(decide([], HOST, self.policy, []).allowed)
        self.assertFalse(decide(["git", "a\x00b"], HOST, self.policy, []).allowed)
        self.assertFalse(decide("git status", HOST, self.policy, []).allowed)


class PathTests(Base):
    def test_traversal_normalised_out(self):
        self.no(["git", "-C", self.repo + "/../../outside", "status"], contains="not under root")

    def test_traversal_staying_inside_ok(self):
        self.ok(["git", "-C", self.repo + "/sub/..", "status"])

    def test_symlink_escape(self):
        d = self.no(["git", "-C", str(self.root / "repos" / "escape"), "status"])
        self.assertIn("resolves to", d.reason)

    def test_relative_refused(self):
        self.no(["git", "-C", "repos/armamentarium", "status"], contains="absolute")

    def test_root_glob_itself_and_below(self):
        self.ok(["git", "-C", self.repo + "/subdir/deeper", "status"])
        self.no(["git", "-C", str(self.root / "repos"), "status"])  # parent of the glob

    def test_unresolved_root(self):
        # by-id names are symlinks to /dev/sdX; resolve:false keeps the by-id spelling
        self.ok(["smartctl", "-a", "/dev/disk/by-id/ata-CT500MX500SSD1_ABC"])
        self.no(["smartctl", "-a", "/dev/sda"], contains="not under root")
        self.no(["smartctl", "-a", "/dev/disk/by-id/../../sda"])
        self.no(["smartctl", "-t", "long", "/dev/disk/by-id/x"])


class GrantTests(Base):
    def grant(self, rule="git-push", host=HOST, ttl=600, kind="exec"):
        return {"kind": kind, "rule": rule, "host": host, "expires_at": time.time() + ttl}

    def test_elevated_needs_grant(self):
        d = self.no(["git", "-C", self.repo, "push"], contains="arma-elevate exec git-push")
        self.assertEqual(d.mode, "elevated")

    def test_elevated_with_grant(self):
        d = self.ok(["git", "-C", self.repo, "push", "origin", "main"],
                    grants=[self.grant()])
        self.assertEqual(d.mode, "elevated")
        self.assertIsNotNone(d.grant)

    def test_grant_other_host(self):
        self.no(["git", "-C", self.repo, "push"], grants=[self.grant(host="sisyphus")])

    def test_grant_expired(self):
        self.no(["git", "-C", self.repo, "push"], grants=[self.grant(ttl=-1)])

    def test_grant_other_rule_or_kind(self):
        self.no(["git", "-C", self.repo, "push"], grants=[self.grant(rule="git-log")])
        self.no(["git", "-C", self.repo, "push"], grants=[self.grant(kind="path")])

    def test_grant_does_not_widen_match(self):
        # a push grant never admits options the rule doesn't list
        self.no(["git", "-C", self.repo, "push", "--force"], grants=[self.grant()])

    def test_hint_names_remote_host(self):
        d = self.no(["git", "-C", self.repo, "push"], host="some-remote")
        self.assertIn("arma-elevate exec git-push some-remote", d.hint)

    def test_hosts_restriction(self):
        self.ok(["zpool", "status"], host="haphaestus")
        self.no(["zpool", "status"], host="hecate", contains="does not apply on hecate")

    def test_nearest_rule_message(self):
        d = self.no(["git", "-C", self.repo, "log", "--stat"])
        self.assertEqual(d.rule, "git-log")


class ValidationTests(Base):
    def bad(self, rules, roots=None, contains=None):
        with self.assertRaises(PolicyError) as cm:
            self.make({"version": 1, "roots": roots or {}, "rules": rules})
        if contains:
            self.assertIn(contains, str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(PolicyError):
            load_policy(self.root / "nope.json")

    def test_bad_json_and_version(self):
        p = self.root / "x.json"
        p.write_text("{")
        with self.assertRaises(PolicyError):
            load_policy(p)
        p.write_text(json.dumps({"version": 2, "rules": {"a": {"argv": ["ls"]}}}))
        with self.assertRaises(PolicyError):
            load_policy(p)

    def test_writable_policy_refused(self):
        p = self.root / "loose.json"
        p.write_text(json.dumps({"version": 1, "rules": {"a": {"argv": ["ls"]}}}))
        p.chmod(0o666)
        with self.assertRaises(PolicyError) as cm:
            load_policy(p)
        self.assertIn("world-writable", str(cm.exception))
        p.chmod(0o644)
        load_policy(p)

    def test_unknown_key(self):
        self.bad({"r": {"argv": ["ls"], "opitons": {}}}, contains="opitons")

    def test_unknown_placeholder(self):
        self.bad({"r": {"argv": ["ls", "{anything}"]}}, contains="unknown placeholder")

    def test_undefined_root(self):
        self.bad({"r": {"argv": ["ls", "{path:nope}"]}}, contains="undefined root")

    def test_argv0_literal(self):
        self.bad({"r": {"argv": ["{name}"]}})
        self.bad({"r": {"argv": ["sh|bash"]}})

    def test_bad_mode_and_option(self):
        self.bad({"r": {"argv": ["ls"], "mode": "sometimes"}})
        self.bad({"r": {"argv": ["ls"], "options": {"--x=1": None}}})
        self.bad({"r": {"argv": ["ls"], "options": {"--": None}}})

    def test_default_mode_is_elevated(self):
        pol = self.make({"version": 1, "rules": {"r": {"argv": ["uptime"]}}})
        self.assertEqual(pol.rules["r"].mode, "elevated")
        self.assertFalse(decide(["uptime"], HOST, pol, []).allowed)


if __name__ == "__main__":
    unittest.main()
