"""armamentarium.fleet — machine fleet registry.

Tracks which machines belong to the fleet and their live presence, layered on top
of the roaming checkout ledger.  Everything lives on **Substrate NFS** so every
node in the fleet can read and write it.

Layout (under DEFAULT_FLEET_ROOT):

    dom0/fleet/
        registry.json              # enrolled machines + master designation
        heartbeats/<hostname>.json # per-machine presence (each machine writes only its own)

Why two files?
- registry.json is written only on enroll/set-master (rare, authoritative).
- heartbeat files are written by a per-machine systemd timer (frequent, per-machine).
  Each machine writes only its own file → zero write contention.

Stale threshold: a heartbeat older than STALE_MINUTES means the machine is offline.
The `status()` call cross-references offline machines against the roaming checkout
ledger so stale locks can be surfaced and optionally force-reclaimed.

CLI surface (added by cli.py):
    armamentarium fleet status              full fleet view
    armamentarium fleet enroll [--master]   register this machine
    armamentarium fleet set-master <host>   change master
    armamentarium fleet heartbeat           write/refresh this machine's heartbeat
    armamentarium fleet ls [--json]         compact table
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .dock import DockError, _read_dock_config

# SSH command tries venv path first, then ~/bin symlink, then bare PATH.
_REMOTE_LIST_CMD = (
    "~/.venvs/armamentarium/bin/armamentarium list --local --json 2>/dev/null "
    "|| ~/bin/armamentarium list --local --json 2>/dev/null "
    "|| armamentarium list --local --json"
)

DEFAULT_FLEET_ROOT = "~/Datasets/Substrate/dom0/fleet"
STALE_MINUTES = 10


class FleetError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def machine_id() -> str:
    return socket.gethostname()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _is_stale(at: Optional[str]) -> bool:
    dt = _parse_dt(at or "")
    if dt is None:
        return True
    return (datetime.now() - dt) > timedelta(minutes=STALE_MINUTES)


def fleet_root() -> Path:
    cfg = _read_dock_config()
    return Path(os.path.expanduser(cfg.get("fleet_root", DEFAULT_FLEET_ROOT)))


def _registry_path() -> Path:
    return fleet_root() / "registry.json"


def _heartbeat_dir() -> Path:
    return fleet_root() / "heartbeats"


def _heartbeat_path(hostname: Optional[str] = None) -> Path:
    return _heartbeat_dir() / f"{hostname or machine_id()}.json"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".arma-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# ------------------------------------------------------------------ registry

def _load_registry() -> dict:
    p = _registry_path()
    if not p.exists():
        return {"version": "1.0", "master": None, "enrolled": {}}
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"version": "1.0", "master": None, "enrolled": {}}
    d.setdefault("enrolled", {})
    return d


def _save_registry(data: dict) -> None:
    _atomic_write(_registry_path(), data)


# ------------------------------------------------------------------ heartbeats

def _load_heartbeat(hostname: Optional[str] = None) -> Optional[dict]:
    p = _heartbeat_path(hostname)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _all_heartbeats() -> dict[str, dict]:
    d = _heartbeat_dir()
    if not d.exists():
        return {}
    out = {}
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            out[f.stem] = data
        except json.JSONDecodeError:
            pass
    return out


# ------------------------------------------------------------------ public API

def enroll(
    hostname: Optional[str] = None,
    *,
    display_name: Optional[str] = None,
    dock_capable: bool = True,
    hardware: Optional[dict] = None,
    as_master: bool = False,
) -> dict:
    """Register this machine (or `hostname`) in the shared registry."""
    host = hostname or machine_id()
    reg = _load_registry()
    entry: dict[str, Any] = {
        "enrolled_at": _now(),
        "dock_capable": dock_capable,
        "display_name": display_name or host,
        "hardware": hardware or {},
    }
    # Preserve original enroll date if re-enrolling.
    existing = reg["enrolled"].get(host, {})
    if "enrolled_at" in existing:
        entry["enrolled_at"] = existing["enrolled_at"]
        entry["re_enrolled_at"] = _now()
    reg["enrolled"][host] = entry
    if as_master or reg["master"] is None:
        reg["master"] = host
    _save_registry(reg)
    return {"enrolled": host, "master": reg["master"], "entry": entry}


def set_master(hostname: str) -> dict:
    """Designate `hostname` as the fleet master."""
    reg = _load_registry()
    if hostname not in reg["enrolled"]:
        raise FleetError(
            f"{hostname!r} is not enrolled. Run `armamentarium fleet enroll` on that machine first."
        )
    reg["master"] = hostname
    _save_registry(reg)
    return {"master": hostname}


def heartbeat(active_checkout: Optional[str] = None) -> dict:
    """Write/refresh this machine's heartbeat file. Call from a systemd timer."""
    host = machine_id()
    data = {
        "hostname": host,
        "at": _now(),
        "state": "online",
        "dock_available": True,
        "active_checkout": active_checkout,
    }
    _atomic_write(_heartbeat_path(host), data)
    return data


def status(account: Optional[str] = None) -> dict:
    """Full fleet view: enrolled machines, online/offline, master, stale checkouts."""
    reg = _load_registry()
    beats = _all_heartbeats()
    me = machine_id()

    # Pull checkout ledger for stale-lock detection (best-effort).
    try:
        from . import roaming
        checkouts = roaming.holders(account=account)
    except Exception:  # noqa: BLE001
        checkouts = {}

    machines = []
    for host, info in reg["enrolled"].items():
        beat = beats.get(host)
        at = beat.get("at") if beat else None
        stale = _is_stale(at)
        online = (not stale) and bool(beat)

        # Stale-lock: this machine is offline but holds a session checkout.
        held = [sid for sid, holder in checkouts.items() if holder == host]
        stale_lock = held if (not online and held) else []

        machines.append({
            "hostname": host,
            "display_name": info.get("display_name", host),
            "is_master": host == reg.get("master"),
            "is_me": host == me,
            "online": online,
            "last_seen": at,
            "dock_capable": info.get("dock_capable", True),
            "hardware": info.get("hardware", {}),
            "enrolled_at": info.get("enrolled_at"),
            "stale_locks": stale_lock,
        })

    # Include heartbeat-only machines not yet formally enrolled.
    enrolled_hosts = set(reg["enrolled"])
    for host, beat in beats.items():
        if host in enrolled_hosts:
            continue
        at = beat.get("at")
        machines.append({
            "hostname": host,
            "display_name": host,
            "is_master": host == reg.get("master"),
            "is_me": host == me,
            "online": not _is_stale(at),
            "last_seen": at,
            "dock_capable": beat.get("dock_available", False),
            "hardware": {},
            "enrolled_at": None,
            "stale_locks": [],
            "_unenrolled": True,
        })

    return {
        "master": reg.get("master"),
        "fleet_root": str(fleet_root()),
        "registry_exists": _registry_path().exists(),
        "machine_count": len(machines),
        "machines": machines,
    }


def ls() -> list[dict]:
    """Compact machine list — hostname, online flag, master flag."""
    s = status()
    return [
        {
            "hostname": m["hostname"],
            "online": m["online"],
            "master": m["is_master"],
            "me": m["is_me"],
            "last_seen": m["last_seen"],
            "stale_locks": m["stale_locks"],
        }
        for m in s["machines"]
    ]


# ------------------------------------------------------------------ sessions

def _local_sessions() -> list[dict]:
    """Run list --local on this machine directly (no SSH)."""
    from .dock import scan_sessions, local_device_dir
    try:
        d = local_device_dir()
        return scan_sessions([str(d)])
    except (DockError, Exception):  # noqa: BLE001
        return []


def _ssh_sessions(hostname: str, timeout: int = 10) -> tuple[str, list[dict], Optional[str]]:
    """SSH to `hostname`, run armamentarium list --local --json.
    Returns (hostname, sessions, error_or_None)."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", hostname, _REMOTE_LIST_CMD],
            capture_output=True, text=True, timeout=timeout,
        )
        if p.returncode != 0:
            err = (p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}")
            return hostname, [], err
        # Output may have a few lines of shell fallback noise before the JSON array.
        # Find the first '[' to isolate the JSON.
        out = p.stdout
        idx = out.find("[")
        if idx == -1:
            return hostname, [], "no JSON array in output"
        sessions = json.loads(out[idx:])
        return hostname, sessions, None
    except subprocess.TimeoutExpired:
        return hostname, [], "ssh timed out"
    except json.JSONDecodeError as e:
        return hostname, [], f"JSON parse error: {e}"
    except Exception as e:  # noqa: BLE001
        return hostname, [], str(e)


def sessions(account: Optional[str] = None, timeout: int = 10) -> dict:
    """Fleet-wide session inventory.

    Queries every enrolled dock-capable machine in parallel:
    - This machine: direct local scan (no SSH).
    - Other online machines: SSH + `armamentarium list --local --json`.
    - Offline or non-dock machines: skipped with a note.

    Returns:
        {
          "sessions": [
            { ...session fields..., "machine": "<hostname>", "is_local": bool },
            ...
          ],
          "machines_queried": [...],
          "machines_skipped": [{"hostname": ..., "reason": ...}, ...],
          "errors": [{"hostname": ..., "error": ...}, ...],
        }
    """
    fleet_status = status(account=account)
    me = machine_id()

    targets = []   # (hostname, is_me)
    skipped = []

    for m in fleet_status["machines"]:
        host = m["hostname"]
        if not m.get("dock_capable", True):
            skipped.append({"hostname": host, "reason": "not dock-capable"})
            continue
        if not m["online"] and not m["is_me"]:
            skipped.append({"hostname": host, "reason": "offline"})
            continue
        targets.append((host, m["is_me"]))

    all_sessions: list[dict] = []
    queried: list[str] = []
    errors: list[dict] = []

    def _query(host: str, is_me: bool):
        if is_me:
            s = _local_sessions()
            return host, s, None
        return _ssh_sessions(host, timeout=timeout)

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as pool:
        futures = {pool.submit(_query, h, local): h for h, local in targets}
        for future in as_completed(futures):
            host, sess, err = future.result()
            queried.append(host)
            if err:
                errors.append({"hostname": host, "error": err})
            else:
                for s in sess:
                    s["machine"] = host
                    s["is_local"] = host == me
                all_sessions.extend(sess)

    # Sort: local first, then by machine, then by title.
    all_sessions.sort(key=lambda s: (not s["is_local"], s["machine"] or "", s.get("title") or ""))

    return {
        "sessions": all_sessions,
        "machines_queried": sorted(queried),
        "machines_skipped": skipped,
        "errors": errors,
    }
