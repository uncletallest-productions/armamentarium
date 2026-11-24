"""Tests for session_env: XDG_RUNTIME_DIR / session bus discovery with verification.
Uses a fake /run/user under a temp dir and the real uid. Run as a NON-root user."""

import os
import socket
import tempfile
import unittest
from unittest import mock

from armamentarium import exec_runner, session_env
from armamentarium.session_env import resolve

UID = os.getuid()


class SessionEnvBase(unittest.TestCase):
    def setUp(self):
        # short path: unix socket paths are limited to ~108 bytes
        self.tmp = tempfile.mkdtemp(prefix="se", dir="/tmp")
        self.run_user = os.path.join(self.tmp, "ru")
        os.mkdir(self.run_user, 0o755)
        self.rt = os.path.join(self.run_user, str(UID))
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()
        for root, dirs, files in os.walk(self.tmp, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                p = os.path.join(root, d)
                os.unlink(p) if os.path.islink(p) else os.rmdir(p)
        os.rmdir(self.tmp)

    def make_rt(self, mode=0o700):
        os.mkdir(self.rt)
        os.chmod(self.rt, mode)

    def make_bus(self, path=None):
        path = path or os.path.join(self.rt, "bus")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        self.socks.append(s)
        return path

    def resolve(self, environ=None):
        return resolve(environ or {}, UID, run_user=self.run_user)


class TestDerive(SessionEnvBase):
    def test_both_derived_when_absent(self):
        self.make_rt()
        bus = self.make_bus()
        env, notes = self.resolve()
        self.assertEqual(env, {"XDG_RUNTIME_DIR": self.rt,
                               "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + bus})
        self.assertTrue(any("derived" in n for n in notes))

    def test_no_runtime_dir_gives_nothing(self):
        env, notes = self.resolve()
        self.assertEqual(env, {})
        self.assertTrue(any("does not exist" in n for n in notes))

    def test_runtime_without_bus_gives_xdg_only(self):
        self.make_rt()
        env, notes = self.resolve()
        self.assertEqual(env, {"XDG_RUNTIME_DIR": self.rt})
        self.assertTrue(any("no user bus running" in n for n in notes))


class TestVerify(SessionEnvBase):
    def test_wrong_mode_refused(self):
        self.make_rt(0o755)
        env, notes = self.resolve()
        self.assertEqual(env, {})
        self.assertTrue(any("0700" in n for n in notes))

    def test_symlinked_runtime_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real, 0o700)
        os.chmod(real, 0o700)
        os.symlink(real, self.rt)
        env, _ = self.resolve()
        self.assertNotIn("XDG_RUNTIME_DIR", env)

    def test_bus_that_is_not_a_socket_refused(self):
        self.make_rt()
        with open(os.path.join(self.rt, "bus"), "w") as f:
            f.write("x")
        env, notes = self.resolve()
        self.assertEqual(env, {"XDG_RUNTIME_DIR": self.rt})
        self.assertTrue(any("not a socket" in n for n in notes))

    def test_foreign_owner_refused(self):
        self.make_rt()
        with mock.patch.object(session_env.os, "lstat") as lstat:
            st = os.stat(self.rt)
            fake = os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, UID + 1,
                                   st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            lstat.return_value = fake
            env, notes = self.resolve()
        self.assertEqual(env, {})
        self.assertTrue(any(f"not {UID}" in n for n in notes))


class TestInherited(SessionEnvBase):
    def test_good_inherited_values_kept(self):
        self.make_rt()
        bus = self.make_bus()
        env, notes = self.resolve({"XDG_RUNTIME_DIR": self.rt,
                                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + bus})
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=" + bus)
        self.assertTrue(any("inherited, verified" in n for n in notes))

    def test_stale_inherited_runtime_replaced_by_derived(self):
        self.make_rt()
        self.make_bus()
        env, notes = self.resolve({"XDG_RUNTIME_DIR": "/nonexistent/run/42"})
        self.assertEqual(env["XDG_RUNTIME_DIR"], self.rt)
        self.assertTrue(any("inherited but dropped" in n for n in notes))

    def test_bus_outside_runtime_dir_dropped(self):
        self.make_rt()
        self.make_bus()
        elsewhere = self.make_bus(os.path.join(self.tmp, "evil"))
        env, notes = self.resolve({"DBUS_SESSION_BUS_ADDRESS": "unix:path=" + elsewhere})
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=" + os.path.join(self.rt, "bus"))
        self.assertTrue(any("not directly inside" in n for n in notes))

    def test_guid_suffix_accepted(self):
        self.make_rt()
        bus = self.make_bus()
        addr = "unix:path=%s,guid=bfde9a498b70f5d497031fc06b05d7a9" % bus
        env, notes = self.resolve({"XDG_RUNTIME_DIR": self.rt,
                                   "DBUS_SESSION_BUS_ADDRESS": addr})
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], addr)

    def test_private_tmp_bus_dropped_for_location_not_format(self):
        # hecate 2026-11-24: dbus-run-session bus in /tmp, guid suffix
        self.make_rt()
        self.make_bus()
        tmpbus = self.make_bus(os.path.join(self.tmp, "dbus-X"))
        env, notes = self.resolve({"DBUS_SESSION_BUS_ADDRESS":
                                   "unix:path=%s,guid=abc" % tmpbus})
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=" + os.path.join(self.rt, "bus"))
        self.assertTrue(any("not directly inside" in n for n in notes), notes)

    def test_unknown_key_or_duplicate_refused(self):
        self.make_rt()
        bus = self.make_bus()
        for addr in ("unix:path=%s,noncefile=/tmp/n" % bus,
                     "unix:path=%s,path=/tmp/other" % bus,
                     "unix:guid=abc"):
            env, notes = self.resolve({"XDG_RUNTIME_DIR": self.rt,
                                       "DBUS_SESSION_BUS_ADDRESS": addr})
            self.assertTrue(any("inherited but dropped" in n for n in notes), (addr, notes))

    def test_non_unix_or_multi_address_dropped(self):
        self.make_rt()
        for addr in ("tcp:host=10.0.0.1,port=1234",
                     "unix:path=%s/bus;unix:path=/tmp/x" % self.rt,
                     "unix:abstract=/tmp/dbus-XYZ"):
            env, notes = self.resolve({"DBUS_SESSION_BUS_ADDRESS": addr})
            self.assertNotEqual(env.get("DBUS_SESSION_BUS_ADDRESS"), addr, addr)


class TestRunnerUsesIt(unittest.TestCase):
    def test_env_has_verified_values_and_drops_bad_inherited(self):
        fake = {"XDG_RUNTIME_DIR": "/run/user/%d" % UID,
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % UID}
        with mock.patch.object(exec_runner.session_env, "session_env", return_value=fake), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/bogus",
                                          "DBUS_SESSION_BUS_ADDRESS": "tcp:host=evil"}):
            env = exec_runner._env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], fake["XDG_RUNTIME_DIR"])
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], fake["DBUS_SESSION_BUS_ADDRESS"])

    def test_env_omits_both_when_unverifiable(self):
        with mock.patch.object(exec_runner.session_env, "session_env", return_value={}), \
             mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/bogus"}):
            env = exec_runner._env()
        self.assertNotIn("XDG_RUNTIME_DIR", env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)


if __name__ == "__main__":
    unittest.main()
