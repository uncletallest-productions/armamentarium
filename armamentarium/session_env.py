"""session_env -- find this user's XDG_RUNTIME_DIR and session D-Bus, and prove they are ours.

Why this exists: processes started outside a login session (MCP servers launched by an app,
cron, systemd system units, ssh without PAM's systemd hooks) often have neither
XDG_RUNTIME_DIR nor DBUS_SESSION_BUS_ADDRESS, so `systemctl --user`, `journalctl --user`,
notify-send, gsettings etc. fail with "Failed to connect to user scope bus". On a
systemd-logind system both have predictable homes:

    XDG_RUNTIME_DIR          = /run/user/<uid>
    DBUS_SESSION_BUS_ADDRESS = unix:path=/run/user/<uid>/bus

Predictable is not the same as trustworthy, so nothing is returned unless it checks out:
the runtime dir must be a real directory (not a symlink) owned by this uid with mode 0700
(the XDG spec's requirement), and the bus must be a socket owned by this uid inside it.
An inherited value is held to the same test as a derived one; a stale or foreign value
from the environment is dropped rather than passed on.

Stdlib only. Use it as a library (``session_env()``), or from a shell:

    eval "$(python3 -m armamentarium.session_env)"     # export what is missing/verified
    python3 -m armamentarium.session_env --check       # explain, exit 1 if anything is unusable
"""

from __future__ import annotations

import os
import stat
import sys
from typing import Dict, List, Optional, Tuple

RUN_USER = "/run/user"
BUS_PREFIX = "unix:path="


def _check_runtime_dir(path: str, uid: int) -> Optional[str]:
    """Return None if ``path`` is a usable runtime dir for ``uid``, else the reason it isn't."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} does not exist (no logind session for uid {uid}?)"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a symlink"
    if not stat.S_ISDIR(st.st_mode):
        return f"{path} is not a directory"
    if st.st_uid != uid:
        return f"{path} is owned by uid {st.st_uid}, not {uid}"
    if stat.S_IMODE(st.st_mode) != 0o700:
        return f"{path} has mode {stat.S_IMODE(st.st_mode):04o}, XDG requires 0700"
    return None


def _check_bus(address: str, runtime_dir: Optional[str], uid: int) -> Optional[str]:
    """Return None if ``address`` is a usable session bus for ``uid``, else why not.

    Only a single ``unix:path=`` address is verified, optionally with a ``guid=`` key (which
    dbus-daemon/dbus-launch append and which names the server, not a location). Any other
    transport or key is refused rather than trusted blindly. The socket must sit inside the
    verified runtime dir, so a private ``/tmp/dbus-*`` bus (dbus-run-session) is dropped
    with that reason: it is not the systemd user manager's bus.
    """
    if ";" in address or not address.startswith("unix:"):
        return f"unsupported bus address {address!r} (only a single unix:path= is verified)"
    keys = {}
    for part in address[len("unix:"):].split(","):
        k, sep, v = part.partition("=")
        if not sep or k in keys:
            return f"malformed bus address {address!r}"
        keys[k] = v
    if "path" not in keys or set(keys) - {"path", "guid"}:
        return (f"unsupported bus address {address!r} (only unix:path=...[,guid=...] "
                f"is verified)")
    path = keys["path"]
    if runtime_dir is None:
        return "no verified XDG_RUNTIME_DIR to hold the bus socket"
    if os.path.dirname(os.path.normpath(path)) != os.path.normpath(runtime_dir):
        return f"bus socket {path} is not directly inside {runtime_dir}"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} does not exist (no user bus running?)"
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    if not stat.S_ISSOCK(st.st_mode):
        return f"{path} is not a socket"
    if st.st_uid != uid:
        return f"{path} is owned by uid {st.st_uid}, not {uid}"
    return None


def resolve(environ: Optional[Dict[str, str]] = None, uid: Optional[int] = None,
            run_user: str = RUN_USER) -> Tuple[Dict[str, str], List[str]]:
    """Work out XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS for ``uid``.

    Returns ``(env, notes)``: ``env`` holds only the variables that verified (0, 1 or 2 keys);
    ``notes`` explains every decision (kept, derived, or dropped and why).
    """
    environ = os.environ if environ is None else environ
    uid = os.getuid() if uid is None else uid
    env: Dict[str, str] = {}
    notes: List[str] = []

    runtime = None
    inherited = environ.get("XDG_RUNTIME_DIR")
    if inherited:
        why = _check_runtime_dir(inherited, uid)
        if why is None:
            runtime = inherited
            notes.append(f"XDG_RUNTIME_DIR={inherited} (inherited, verified)")
        else:
            notes.append(f"XDG_RUNTIME_DIR inherited but dropped: {why}")
    if runtime is None:
        derived = os.path.join(run_user, str(uid))
        why = _check_runtime_dir(derived, uid)
        if why is None:
            runtime = derived
            notes.append(f"XDG_RUNTIME_DIR={derived} (derived from uid, verified)")
        else:
            notes.append(f"XDG_RUNTIME_DIR unavailable: {why}")
    if runtime is not None:
        env["XDG_RUNTIME_DIR"] = runtime

    bus = None
    inherited = environ.get("DBUS_SESSION_BUS_ADDRESS")
    if inherited:
        why = _check_bus(inherited, runtime, uid)
        if why is None:
            bus = inherited
            notes.append(f"DBUS_SESSION_BUS_ADDRESS={inherited} (inherited, verified)")
        else:
            notes.append(f"DBUS_SESSION_BUS_ADDRESS inherited but dropped: {why}")
    if bus is None and runtime is not None:
        derived = BUS_PREFIX + os.path.join(runtime, "bus")
        why = _check_bus(derived, runtime, uid)
        if why is None:
            bus = derived
            notes.append(f"DBUS_SESSION_BUS_ADDRESS={derived} (derived, verified)")
        else:
            notes.append(f"DBUS_SESSION_BUS_ADDRESS unavailable: {why}")
    elif bus is None:
        notes.append("DBUS_SESSION_BUS_ADDRESS unavailable: no verified runtime dir")
    if bus is not None:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    return env, notes


def session_env(environ: Optional[Dict[str, str]] = None, uid: Optional[int] = None) -> Dict[str, str]:
    """Just the verified variables (see ``resolve`` for the reasons)."""
    return resolve(environ, uid)[0]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env, notes = resolve()
    if "--check" in argv:
        for n in notes:
            print(n)
        return 0 if len(env) == 2 else 1
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if key in env:
            print(f"export {key}={_shell_quote(env[key])}")
    for n in notes:
        if "unavailable" in n or "dropped" in n:
            print(f"# {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
