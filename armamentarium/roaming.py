"""armamentarium.roaming — cross-machine session checkout ledger + undock.

Lets a user's machines hand sessions off safely (no live dual-open). Coordination
lives in a **shared ledger on Substrate**, reachable by every node over NFS (LAN or
tailnet) — not mDNS, which is link-local and wouldn't reach the tailnet nodes.

Layout (under the roaming store root, default ~/Datasets/Substrate/dom0/session-roaming):

    <root>/<accountUUID>/store/            canonical session copies (local_<uuid>/ + sidecar)
    <root>/<accountUUID>/checkouts.json    {session_id: {machine, at, last?}}

Verbs:
  - dock (in cli): acquires a checkout (refuses if another machine holds it live).
  - undock: flushes the live local session back to the store and releases the checkout.
  - roaming status / checkin / checkout: inspect and hand-edit the ledger.

Account-scoped; machine identity = hostname. Atomic ledger writes (temp + rename).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import dock
from .dock import UUID_RE, DockError, _read_dock_config

DEFAULT_STORE_ROOT = "~/Datasets/Substrate/dom0/session-roaming"


class RoamingError(Exception):
    pass


def machine_id() -> str:
    return socket.gethostname()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def store_root() -> Path:
    cfg = _read_dock_config()
    return Path(os.path.expanduser(cfg.get("roaming_store", DEFAULT_STORE_ROOT)))


def _resolve_account(account: Optional[str] = None) -> str:
    if account:
        return account
    if not dock.LOCAL_ROOT.exists():
        raise RoamingError(f"local session root not found: {dock.LOCAL_ROOT}")
    accs = sorted(c.name for c in dock.LOCAL_ROOT.iterdir() if c.is_dir() and UUID_RE.match(c.name))
    if len(accs) != 1:
        raise RoamingError(f"expected exactly 1 account under {LOCAL_ROOT}, found {accs}; pass --account")
    return accs[0]


def _base(account: Optional[str] = None, create: bool = False) -> Path:
    base = store_root() / _resolve_account(account)
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def _ledger_path(base: Path) -> Path:
    return base / "checkouts.json"


def _store_dir(base: Path) -> Path:
    return base / "store"


def _load_ledger(base: Path) -> dict:
    p = _ledger_path(base)
    if not p.exists():
        return {"version": "1.0", "checkouts": {}}
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"version": "1.0", "checkouts": {}}
    d.setdefault("checkouts", {})
    return d


def _save_ledger(base: Path, data: dict) -> None:
    p = _ledger_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(".checkouts.json.arma-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


# ------------------------------------------------------------------- ops

def store_sessions_dir(account: Optional[str] = None) -> Optional[Path]:
    """The store/ dir holding roamable session copies, or None if unresolved/absent."""
    try:
        sd = _store_dir(_base(account))
    except (RoamingError, DockError):
        return None
    return sd if sd.exists() else None


def holders(account: Optional[str] = None) -> dict:
    """session_id -> holding machine (or None). {} if the ledger isn't reachable."""
    try:
        led = _load_ledger(_base(account))
    except (RoamingError, DockError):
        return {}
    return {sid: e.get("machine") for sid, e in led.get("checkouts", {}).items()}


def status(account: Optional[str] = None) -> dict:
    base = _base(account)
    led = _load_ledger(base)
    return {
        "account": base.name,
        "machine": machine_id(),
        "store": str(base),
        "store_exists": base.exists(),
        "checkouts": led.get("checkouts", {}),
    }


def checkout(session_id: str, force: bool = False, account: Optional[str] = None) -> dict:
    base = _base(account, create=True)
    led = _load_ledger(base)
    me = machine_id()
    cur = led["checkouts"].get(session_id)
    if cur and cur.get("machine") and cur["machine"] != me and not force:
        raise RoamingError(
            f"{session_id} is checked out by {cur['machine']} (since {cur.get('at')}). "
            f"Pass --force to override — only if you're sure that machine isn't using it live "
            f"(no-dual-open corrupts the session)."
        )
    led["checkouts"][session_id] = {"machine": me, "at": _now()}
    _save_ledger(base, led)
    return led["checkouts"][session_id]


def checkin(session_id: str, account: Optional[str] = None) -> Optional[dict]:
    base = _base(account, create=True)
    led = _load_ledger(base)
    prev = led["checkouts"].get(session_id)
    led["checkouts"][session_id] = {"machine": None, "at": _now(), "last": (prev or {}).get("machine")}
    _save_ledger(base, led)
    return prev


def try_checkout(session_id: str, force: bool = False, account: Optional[str] = None):
    """Best-effort guard for `dock`: (ok, note). If the store root is absent/unconfigured,
    skip the ledger (ok=True) so dock still works standalone."""
    try:
        if not store_root().exists():
            return True, "roaming store not present — ledger skipped"
        entry = checkout(session_id, force=force, account=account)
        return True, f"checked out to {entry['machine']}"
    except RoamingError as e:
        return False, str(e)
    except DockError as e:
        return True, f"ledger skipped ({e})"


def undock(local_session_dir: str, remove_local: bool = False,
           account: Optional[str] = None) -> dict:
    """Flush a live local session back to the shared store and release its checkout."""
    src = Path(local_session_dir).expanduser().resolve()
    if not src.is_dir() or not src.name.startswith("local_"):
        raise RoamingError(f"not a 'local_<uuid>' session directory: {local_session_dir}")
    sid = src.name
    sidecar = src.with_name(sid + ".json")
    if not sidecar.exists():
        raise RoamingError(f"sidecar not found next to session dir: {sidecar}")

    base = _base(account, create=True)
    store = _store_dir(base)
    store.mkdir(parents=True, exist_ok=True)
    dst = store / sid
    dst_sidecar = store / (sid + ".json")

    # Flush latest live state (overwrite the store copy).
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    shutil.copy2(sidecar, dst_sidecar)

    checkin(sid, account=account)  # release — available to dock elsewhere

    removed = False
    if remove_local:
        shutil.rmtree(src)
        sidecar.unlink()
        removed = True

    return {
        "undocked": sid,
        "store": str(dst),
        "released": True,
        "removed_local": removed,
        "note": "flushed to store and released; safe to dock on another machine",
    }
