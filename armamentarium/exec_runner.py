"""exec_runner -- run an argv ONLY if exec_policy allows it, safely, and log every decision.

Layering:  arma_exec (MCP tool)  ->  execute()  ->  exec_policy.decide()  ->  subprocess
Everything that decides lives in exec_policy (pure, exhaustively tested). This module adds only
the things that make an ALLOWED command safe to actually run:

* argv only, never a shell; stdin is /dev/null; cwd is "/".
* the program is looked up on a FIXED system PATH, never the caller's. (~/bin and any other
  agent-reachable directory can't shadow `git` or `systemctl`.)
* a minimal environment: only what the allowed commands genuinely need is passed through.
* git hardening: repo-local config can name programs git will run. Writes under .git/ are
  already denied to agents by the router (protected_write_reason); as the second fix, every git
  call here forces those knobs off. Decided 2026-11-23: both fixes. Every git call also gets
  ``--literal-pathspecs``, so ``*`` or ``:(glob)`` in an argument is a file name, never a pattern.
* agent identity: every git call runs with GIT_AUTHOR_*/GIT_COMMITTER_* set to the agent
  identity (policy ``git_identity``, else DEFAULT_GIT_IDENTITY). Agent commits are labelled as
  agent commits in every repo, and the operator's own commits keep the operator's own config.
  (2026-11-25: one repo-local user.name had credited the operator's commits to the agent.)
* sudo rules run as ``sudo -n``: no password prompt, ever. Each command must be allowed by an
  operator-written sudoers entry; there is no blanket sudo.
* timeout kills the whole process group; output is capped; the decision, allowed or refused, is
  appended to ~/.local/state/armamentarium/exec.log (JSON lines, 0600).

Remote hosts are not built yet: a host other than this one is refused with a clear message.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from . import exec_policy, session_env
from .mcp_router.config import load_elevation, local_host

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EXEC_LOG = Path("~/.local/state/armamentarium/exec.log").expanduser()
# Root-owned directory of hooks the operator has approved for agent commits. If it is absent or
# empty, agent commits simply run no hooks -- never the repo's own (agent-reachable) hooks.
APPROVED_HOOKS = "/usr/local/share/armamentarium/git-hooks"

# XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS are NOT passed through blindly: session_env
# verifies an inherited value or derives one from the uid (the server is usually launched
# without them, which broke every `systemctl --user` rule -- smoke test #8, 2026-11-24).
PASS_ENV = ("HOME", "USER", "LOGNAME", "SSH_AUTH_SOCK")
GIT_HARDEN = [
    "-c", "core.fsmonitor=false",              # status/diff would run it
    "-c", f"core.hooksPath={APPROVED_HOOKS}",  # never the repo's own hooks
    # fetch/push would run a repo-set ssh command; ours never prompts (no tty to answer it),
    # never starts a lingering ControlMaster, and gives up on a dead route
    "-c", "core.sshCommand=ssh -o BatchMode=yes -o ControlMaster=no -o ConnectTimeout=15",
    "-c", "protocol.ext.allow=never",          # ext:: remotes run arbitrary commands
    "--no-pager",
    "--literal-pathspecs",                     # `*`, `:(glob)`, `:/` are names, not patterns
]
DEFAULT_GIT_IDENTITY = {"name": "Cognate (armamentarium)", "email": "cognate@armamentarium.invalid"}
GIT_NO_DIFF_PROGRAMS = {"diff", "show", "log"}  # add --no-ext-diff --no-textconv after these
ARG_LOG_MAX = 200


@dataclass
class ExecResult:
    allowed: bool
    reason: str
    rule: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    duration_ms: int = 0
    hint: str = ""
    ran: List[str] = field(default_factory=list)   # the argv actually executed

    def to_json(self) -> str:
        d = asdict(self)
        if not d["hint"]:
            d.pop("hint")
        return json.dumps(d, indent=2)


def harden(argv: List[str]) -> List[str]:
    """The argv as it will really run: git hardened, everything else unchanged."""
    if argv[0] != "git":
        return list(argv)
    rest = argv[1:]
    # find the subcommand: skip leading "-C <path>" pairs (the only global option rules allow)
    i = 0
    while i + 1 < len(rest) and rest[i] == "-C":
        i += 2
    out = ["git", *GIT_HARDEN, *rest[:i + 1]]
    if i < len(rest) and rest[i] in GIT_NO_DIFF_PROGRAMS:
        out += ["--no-ext-diff", "--no-textconv"]
    return out + rest[i + 1:]


def _env(git_identity: Optional[dict] = None) -> dict:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update(session_env.session_env())
    env.update({"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
                "GIT_TERMINAL_PROMPT": "0", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
    if git_identity:
        env.update({"GIT_AUTHOR_NAME": git_identity["name"],
                    "GIT_AUTHOR_EMAIL": git_identity["email"],
                    "GIT_COMMITTER_NAME": git_identity["name"],
                    "GIT_COMMITTER_EMAIL": git_identity["email"]})
    return env


def _log(entry: dict) -> None:
    """Append one JSON line. Logging failure never blocks or alters a decision."""
    try:
        EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(EXEC_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


READ_GRACE = 3.0   # seconds to let output drain after the process itself has exited


class _Reader(threading.Thread):
    """Drain one pipe, keeping at most ``cap`` bytes. Always reads to EOF (so the child can never
    block on a full pipe) but never stores more than the cap."""

    def __init__(self, pipe, cap: int):
        super().__init__(daemon=True)
        self.pipe, self.cap, self.buf, self.total = pipe, cap, bytearray(), 0

    def run(self):
        try:
            while True:
                chunk = self.pipe.read1(65536) if hasattr(self.pipe, "read1") else self.pipe.read(65536)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.cap - len(self.buf)
                if room > 0:
                    self.buf += chunk[:room]
        except (OSError, ValueError):
            pass

    def text(self):
        return bytes(self.buf).decode("utf-8", "replace"), self.total > self.cap


def _killpg(pid: int, sig=signal.SIGKILL) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def execute(argv: List[str], host: Optional[str] = None,
            policy_path: Optional[Path] = None, grants: Optional[List[dict]] = None) -> ExecResult:
    """Decide, and if allowed, run. Always returns an ExecResult; never raises for policy."""
    me = local_host()
    host = (host or me).split(".")[0].lower()
    t0 = time.time()
    entry = {"ts": round(t0, 3), "host": host,
             "argv": [a if len(a) <= ARG_LOG_MAX else a[:ARG_LOG_MAX] + "..." for a in argv]
             if isinstance(argv, list) else repr(argv)[:ARG_LOG_MAX]}

    def refuse(reason: str, rule=None, hint="") -> ExecResult:
        entry.update(allowed=False, rule=rule, reason=reason[:300])
        _log(entry)
        return ExecResult(False, reason, rule=rule, hint=hint)

    if host != me:
        return refuse(f"refused: remote exec on {host!r} is not built yet (this is {me}). "
                      f"Only local commands run for now.")
    try:
        policy = exec_policy.load_policy(policy_path)
    except exec_policy.PolicyError as exc:
        return refuse(f"refused: {exc}")
    if grants is None:
        grants = load_elevation().grants
    d = exec_policy.decide(argv, host, policy, grants)
    if not d.allowed:
        return refuse(d.reason, d.rule, d.hint)

    prog = shutil.which(argv[0], path=SAFE_PATH)
    if not prog:
        return refuse(f"refused: {argv[0]!r} is allowed by {d.rule!r} but is not installed "
                      f"on {host} (searched {SAFE_PATH})", d.rule)
    run = harden(d.run_argv or argv)   # checked paths, not the caller's spelling
    run[0] = prog
    if d.sudo:
        sudo = shutil.which("sudo", path=SAFE_PATH)
        if not sudo:
            return refuse("refused: rule needs sudo but sudo is not installed", d.rule)
        run = [sudo, "-n", "--", *run]

    entry.update(allowed=True, rule=d.rule, mode=d.mode, ran=run,
                 grant_expires_at=(d.grant or {}).get("expires_at"))
    timed_out = False
    abandoned = False
    try:
        ident = (policy.git_identity or DEFAULT_GIT_IDENTITY) if argv[0] == "git" else None
        proc = subprocess.Popen(run, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd="/", env=_env(ident),
                                start_new_session=True)
    except OSError as exc:
        entry.update(error=str(exc))
        _log(entry)
        return ExecResult(False, f"failed to start {prog}: {exc}", rule=d.rule, ran=run)
    readers = [_Reader(proc.stdout, d.max_output), _Reader(proc.stderr, d.max_output)]
    for r in readers:
        r.start()
    # Wait for the PROCESS, not for EOF on its pipes: a helper that detaches (ssh ProxyCommand,
    # a daemonising child) can hold the pipes open forever after the command itself is done.
    try:
        proc.wait(timeout=d.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.time() + READ_GRACE
    for r in readers:
        r.join(max(0.0, deadline - time.time()))
    if any(r.is_alive() for r in readers):
        # stragglers still hold the pipes: clean up our own process group, then stop waiting
        _killpg(proc.pid)
        for r in readers:
            r.join(1.0)
        abandoned = any(r.is_alive() for r in readers)
    (so, t1), (se, t2) = readers[0].text(), readers[1].text()
    if not abandoned:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
    ms = int((time.time() - t0) * 1000)
    reason = d.reason
    if timed_out:
        reason += f"; KILLED after {d.timeout}s timeout"
    if abandoned:
        reason += ("; a detached child kept the output pipes open after the command ended -- "
                   "output may be incomplete")
    if not timed_out and proc.returncode not in (0, None):
        reason += f"; exited {proc.returncode}"
        if d.sudo and "password is required" in se:
            reason += (" -- sudo has no NOPASSWD entry for this command; the operator must add "
                       "one (no blanket sudo)")
    entry.update(exit=proc.returncode, duration_ms=ms, truncated=t1 or t2, timed_out=timed_out,
                 abandoned_pipes=abandoned)
    _log(entry)
    return ExecResult(True, reason, rule=d.rule, exit_code=proc.returncode, stdout=so, stderr=se,
                      truncated=t1 or t2, timed_out=timed_out, duration_ms=ms, ran=run)
