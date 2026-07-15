"""armamentarium.desktop — optional, config-driven control of the Claude Desktop process.

Claude Desktop only re-scans the session tree at startup, so docking a session needs a real
**quit + relaunch** (not just a focus of the running window — the app is single-instance and
re-launching only raises the existing window). This helper makes that one graceful op, and
lets `dock --restart` do stop -> apply -> start so files are never written under a live app.

It is deliberately thin and OPT-IN. armamentarium's core (router + dock) works with zero
process management; this only runs when you configure it. The launch command and process
match are NOT hardcoded — they live in `~/.config/armamentarium/desktop.json`, because the
binary/packaging differs per machine (claude-desktop-unofficial, flatpak, npm, ...).

Config (`~/.config/armamentarium/desktop.json`)::

    {
      "command": ["/usr/bin/claude-desktop-unofficial"],
      "env": {"CLAUDE_USE_WAYLAND": "1"},
      "process_match": "claude-desktop-unofficial",
      "term_timeout_s": 8
    }
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

DESKTOP_CONFIG = Path("~/.config/armamentarium/desktop.json").expanduser()


class DesktopError(Exception):
    pass


class DesktopConfig:
    def __init__(self, data: dict):
        self.command: List[str] = data.get("command") or []
        self.env: Dict[str, str] = data.get("env") or {}
        self.process_match: str = data.get("process_match") or ""
        # Electron apps are multi-process (main + renderers + GPU + utility + zygote);
        # graceful teardown of all of them can take a while, so default generously.
        self.term_timeout_s: float = float(data.get("term_timeout_s", 15))
        # Settle window after the last process exits, before relaunch — lets the
        # single-instance lock/socket (shared ~/.config/Claude) fully release.
        self.restart_settle_s: float = float(data.get("restart_settle_s", 2.5))
        if not self.command:
            raise DesktopError("desktop config missing 'command' (list, e.g. [\"/usr/bin/claude-desktop-unofficial\"])")
        if not self.process_match:
            raise DesktopError("desktop config missing 'process_match' (substring to find the process, e.g. \"claude-desktop-unofficial\")")


def load_desktop_config() -> DesktopConfig:
    if not DESKTOP_CONFIG.exists():
        raise DesktopError(
            f"no desktop config at {DESKTOP_CONFIG}. Copy config/desktop.example.json there "
            f"and set your launch command + process match."
        )
    try:
        return DesktopConfig(json.loads(DESKTOP_CONFIG.read_text()))
    except json.JSONDecodeError as e:
        raise DesktopError(f"{DESKTOP_CONFIG} is not valid JSON: {e}") from e


# ------------------------------------------------------------------ process helpers

def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def find_pids(match: str) -> List[int]:
    """PIDs whose executable (argv[0]) contains `match` (excluding this process).

    Matches argv[0] ONLY — not the full argument list — so a shell or editor that merely
    *mentions* the process name in its args is never matched (that false positive would let
    `stop()` SIGTERM the wrong process). `match` should therefore be the launched binary's
    name/path, e.g. "claude-desktop-unofficial".
    """
    if not match:
        return []
    me = os.getpid()
    pids: List[int] = []
    proc = Path("/proc")
    if not proc.exists():
        raise DesktopError("/proc not available — process control is Linux-only in this prototype")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue  # kernel threads have empty cmdline
        argv0 = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if argv0 and match in argv0:
            pids.append(pid)
    return sorted(pids)


# --------------------------------------------------------------------------- actions

def status(cfg: DesktopConfig) -> dict:
    pids = find_pids(cfg.process_match)
    return {"running": bool(pids), "pids": pids, "process_match": cfg.process_match}


def start(cfg: DesktopConfig, settle_s: float = 1.0) -> dict:
    existing = find_pids(cfg.process_match)
    if existing:
        return {"action": "start", "already_running": True, "pids": existing}
    env = {**os.environ, **cfg.env}
    subprocess.Popen(
        cfg.command, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach so it outlives the CLI
    )
    time.sleep(settle_s)
    return {"action": "start", "launched": cfg.command, "env": cfg.env, "pids": find_pids(cfg.process_match)}


def stop(cfg: DesktopConfig, force: bool = False) -> dict:
    pids = find_pids(cfg.process_match)
    if not pids:
        return {"action": "stop", "was_running": False}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + cfg.term_timeout_s
    while time.time() < deadline:
        if not any(_alive(p) for p in pids):
            break
        time.sleep(0.25)
    remaining = [p for p in pids if _alive(p)]
    killed = []
    if remaining and force:
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                pass
        time.sleep(0.5)
        remaining = [p for p in pids if _alive(p)]
    return {
        "action": "stop", "was_running": True, "sigterm": pids,
        "sigkill": killed, "still_alive": remaining,
        "clean": not remaining,
    }


def restart(cfg: DesktopConfig, force: bool = False) -> dict:
    s = stop(cfg, force=force)
    if s.get("still_alive"):
        raise DesktopError(
            f"processes did not exit within {cfg.term_timeout_s}s: {s['still_alive']}. "
            f"Re-run with --force to SIGKILL, or close Claude Desktop manually."
        )
    # Settle: even after the processes are gone, the single-instance lock/socket may
    # linger a moment. Wait, then double-check nothing respawned before relaunching.
    settled = 0.0
    if s.get("was_running"):
        time.sleep(cfg.restart_settle_s)
        settled = cfg.restart_settle_s
        lingering = find_pids(cfg.process_match)
        while lingering and settled < cfg.term_timeout_s:
            time.sleep(0.5)
            settled += 0.5
            lingering = find_pids(cfg.process_match)
        if lingering:
            raise DesktopError(
                f"processes reappeared after stop and did not clear: {lingering}. "
                f"Not relaunching to avoid a double instance."
            )
    return {"action": "restart", "stopped": s, "settled_s": settled, "started": start(cfg)}
