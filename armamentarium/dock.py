"""armamentarium dock — port a Claude Desktop *chat* session onto this machine.

Productizes the manual, proven procedure (2026-07-14 rewrite-test):

    dock(chat session) = sync `local_{uuid}/` + `local_{uuid}.json` sidecar into the
    local device tree, then rewrite the `userSelectedFolders` mount root(s).

Two transforms, in priority order of how often they matter on a cloned-config fleet:

1. **Path-map rewrite** — replace stale mount roots in the sidecar
   (e.g. `/home/youruser/Substrate` -> `/home/youruser/Datasets/Substrate`). This is the
   one that actually mattered in testing. Map comes from
   `~/.config/armamentarium/dock.json` and/or `--path-map old=new`.
2. **deviceUUID rewrite** — only when the source session's deviceUUID differs from this
   machine's (the fresh-install case; a NO-OP on a cloned-config fleet where they match).
   Rewrites `cwd` and renames the `.claude/projects/<slug>` dir accordingly.

Scheduled tasks are deliberately OUT OF SCOPE — they live in `scheduled-tasks.json` with a
server-side `scheduledTaskId`, a separate mechanism (see the caddy block).

Safety: dry-run by default (prints a plan, writes nothing). Additive only — refuses to
overwrite an existing session unless `--force`. Cleanup is deleting the two items it added.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOCAL_ROOT = Path("~/.config/Claude/local-agent-mode-sessions").expanduser()
DOCK_CONFIG = Path("~/.config/armamentarium/dock.json").expanduser()
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class DockError(Exception):
    pass


# ----------------------------------------------------------------------------- config

def load_path_map(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Load the mount-root path map from config, overlaid with CLI --path-map entries."""
    path_map: Dict[str, str] = {}
    if DOCK_CONFIG.exists():
        try:
            data = json.loads(DOCK_CONFIG.read_text())
        except json.JSONDecodeError as e:
            raise DockError(f"{DOCK_CONFIG} is not valid JSON: {e}") from e
        path_map.update(data.get("path_map", {}))
    if extra:
        path_map.update(extra)
    return path_map


# ------------------------------------------------------------------- local tree probe

def _uuid_children(d: Path) -> List[str]:
    return sorted(c.name for c in d.iterdir() if c.is_dir() and UUID_RE.match(c.name))


def discover_local(account: Optional[str], device: Optional[str]) -> Tuple[str, str]:
    """Find this machine's account + device UUID dirs under LOCAL_ROOT.

    Uses the single UUID-shaped dir at each level; requires an explicit override only when
    the choice is ambiguous.
    """
    if not LOCAL_ROOT.exists():
        raise DockError(f"local session root not found: {LOCAL_ROOT}")
    accounts = _uuid_children(LOCAL_ROOT)
    if account is None:
        if len(accounts) != 1:
            raise DockError(f"expected exactly 1 account UUID under {LOCAL_ROOT}, found {accounts}; pass --account")
        account = accounts[0]
    elif account not in accounts:
        raise DockError(f"account {account} not found under {LOCAL_ROOT} (have {accounts})")

    acct_dir = LOCAL_ROOT / account
    devices = _uuid_children(acct_dir)
    if device is None:
        if len(devices) != 1:
            raise DockError(f"expected exactly 1 device UUID under {acct_dir}, found {devices}; pass --device")
        device = devices[0]
    elif device not in devices:
        raise DockError(f"device {device} not found under {acct_dir} (have {devices})")
    return account, device


# ------------------------------------------------------------------------------- plan

@dataclass
class Rewrite:
    kind: str        # "path-map" | "device"
    old: str
    new: str
    count: int       # occurrences in the sidecar text


@dataclass
class DockPlan:
    session_id: str
    title: Optional[str]
    src_dir: Path
    src_sidecar: Path
    account: str
    local_device: str
    src_device: Optional[str]
    dst_dir: Path
    dst_sidecar: Path
    collision: bool
    usf_before: List[str]
    usf_after: List[str]
    rewrites: List[Rewrite]
    is_scheduled: bool = False               # sessionType "scheduled" / has scheduledTaskId
    scheduled_task_id: Optional[str] = None
    unmapped_stale: List[str] = field(default_factory=list)  # paths still not on disk after rewrite


def _apply_map(text: str, path_map: Dict[str, str]) -> str:
    # Longest keys first so nested prefixes don't get half-rewritten.
    for old in sorted(path_map, key=len, reverse=True):
        text = text.replace(old, path_map[old])
    return text


def build_plan(
    session_path: str,
    path_map: Dict[str, str],
    account: Optional[str] = None,
    device: Optional[str] = None,
) -> DockPlan:
    src_dir = Path(session_path).expanduser().resolve()
    if not src_dir.is_dir() or not src_dir.name.startswith("local_"):
        raise DockError(f"source must be a 'local_<uuid>' session directory: {session_path}")
    session_id = src_dir.name
    src_sidecar = src_dir.with_name(session_id + ".json")
    if not src_sidecar.exists():
        raise DockError(f"sidecar not found next to session dir: {src_sidecar}")

    sidecar_text = src_sidecar.read_text()
    sidecar = json.loads(sidecar_text)
    title = sidecar.get("title")
    usf_before = list(sidecar.get("userSelectedFolders") or [])
    scheduled_task_id = sidecar.get("scheduledTaskId")
    is_scheduled = sidecar.get("sessionType") == "scheduled" or bool(scheduled_task_id)

    # source deviceUUID from the sidecar cwd (segment after the account UUID)
    src_device = None
    cwd = sidecar.get("cwd", "")
    m = re.search(r"local-agent-mode-sessions/[0-9a-f-]{36}/([0-9a-f-]{36})/", cwd)
    if m:
        src_device = m.group(1)

    acct, local_device = discover_local(account, device)

    rewrites: List[Rewrite] = []
    for old in sorted(path_map, key=len, reverse=True):
        c = sidecar_text.count(old)
        if c:
            rewrites.append(Rewrite("path-map", old, path_map[old], c))
    if src_device and src_device != local_device:
        rewrites.append(Rewrite("device", src_device, local_device,
                                sidecar_text.count(src_device)))

    usf_after = [_apply_map(p, path_map) for p in usf_before]

    dst_dir = LOCAL_ROOT / acct / local_device / session_id
    dst_sidecar = dst_dir.with_name(session_id + ".json")

    # After rewrite, warn about any selected folder that still won't exist on this host.
    unmapped = [p for p in usf_after if not Path(p).exists()]

    return DockPlan(
        session_id=session_id, title=title, src_dir=src_dir, src_sidecar=src_sidecar,
        account=acct, local_device=local_device, src_device=src_device,
        dst_dir=dst_dir, dst_sidecar=dst_sidecar, collision=dst_dir.exists() or dst_sidecar.exists(),
        usf_before=usf_before, usf_after=usf_after, rewrites=rewrites,
        is_scheduled=is_scheduled, scheduled_task_id=scheduled_task_id, unmapped_stale=unmapped,
    )


def render_plan(p: DockPlan) -> str:
    L = []
    L.append(f"dock plan: {p.session_id}" + (f"  ({p.title!r})" if p.title else ""))
    L.append(f"  source     : {p.src_dir}")
    L.append(f"  dest       : {p.dst_dir}")
    L.append(f"  account    : {p.account}")
    dev = "same (no rewrite)" if p.src_device == p.local_device else f"{p.src_device} -> {p.local_device}"
    L.append(f"  deviceUUID : {dev}")
    L.append(f"  collision  : {'YES — will refuse without --force' if p.collision else 'no'}")
    if p.is_scheduled:
        L.append(f"  SCHEDULED  : this is a SCHEDULED TASK (scheduledTaskId={p.scheduled_task_id}) —")
        L.append("               OUT OF SCOPE for dock. It carries server-side task state that")
        L.append("               won't roam by copying files. Refused unless --allow-scheduled.")
    if p.rewrites:
        L.append("  rewrites   :")
        for r in p.rewrites:
            L.append(f"    [{r.kind}] {r.old}  ->  {r.new}   ({r.count} occ.)")
    else:
        L.append("  rewrites   : none (no path-map matches, device identical)")
    if p.usf_before != p.usf_after:
        L.append(f"  folders    : {p.usf_before}  ->  {p.usf_after}")
    else:
        L.append(f"  folders    : {p.usf_before}  (unchanged)")
    if p.unmapped_stale:
        L.append(f"  WARNING    : selected folder(s) still missing on this host after rewrite: {p.unmapped_stale}")
        L.append("               (add a --path-map entry or the folder will show disconnected)")
    return "\n".join(L)


# ------------------------------------------------------------------------------ apply

def apply_plan(p: DockPlan, path_map: Dict[str, str], force: bool = False,
               allow_scheduled: bool = False) -> dict:
    if p.is_scheduled and not allow_scheduled:
        raise DockError(
            f"{p.session_id} is a scheduled task (scheduledTaskId={p.scheduled_task_id}); "
            f"dock is for chat sessions only. Scheduled-task portability is a separate, "
            f"unsolved problem (server-side registration). Pass --allow-scheduled to force "
            f"the file copy anyway (the task will likely be inert)."
        )
    if p.collision and not force:
        raise DockError(f"session already present at {p.dst_dir} — pass --force to overwrite")
    if p.collision and force:
        if p.dst_dir.exists():
            shutil.rmtree(p.dst_dir)
        if p.dst_sidecar.exists():
            p.dst_sidecar.unlink()

    # 1. sync the session directory
    shutil.copytree(p.src_dir, p.dst_dir)

    # 2. deviceUUID: rename the .claude/projects/<slug> dir + rewrite text within the copy
    if p.src_device and p.src_device != p.local_device:
        _rewrite_device_in_tree(p.dst_dir, p.src_device, p.local_device, path_map)

    # 3. sidecar: string-level rewrite (preserves formatting; matches the proven sed test)
    text = p.src_sidecar.read_text()
    text = _apply_map(text, path_map)
    if p.src_device and p.src_device != p.local_device:
        text = text.replace(p.src_device, p.local_device)
    p.dst_sidecar.write_text(text)

    return {
        "docked": True,
        "session_id": p.session_id,
        "dst_dir": str(p.dst_dir),
        "dst_sidecar": str(p.dst_sidecar),
        "cleanup": f'rm -rf "{p.dst_dir}" "{p.dst_sidecar}"',
    }


def _rewrite_device_in_tree(root: Path, old_dev: str, new_dev: str, path_map: Dict[str, str]) -> None:
    """General-case (fresh-install) support: fix the projects-slug dir + path refs in the copy."""
    projects = root / ".claude" / "projects"
    if projects.is_dir():
        for slug in list(projects.iterdir()):
            if old_dev in slug.name:
                slug.rename(slug.with_name(slug.name.replace(old_dev, new_dev)))
    # light text rewrite of small json/text files inside .claude (skip big audit.jsonl)
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".md") and f.stat().st_size < 512_000:
            try:
                t = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            nt = _apply_map(t.replace(old_dev, new_dev), path_map)
            if nt != t:
                f.write_text(nt)
