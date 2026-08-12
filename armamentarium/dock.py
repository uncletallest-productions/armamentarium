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

--- 2026-08-12: multi-root support -----------------------------------------------
Originally this module only ever looked at LOCAL_ROOT (the primary Claude Desktop
tree, ~/.config/Claude/local-agent-mode-sessions). It turns out at least one other
tree exists on this fleet with the IDENTICAL local_<uuid>.json + local_<uuid>/
shape: ~/.config/claude-harness/<profile>/session-data/local-agent-mode-sessions/,
produced by the Erebos Harness Configurator (~/bin/HarnessConfigurator/) for
multi-account testing (profiles: pro=dom1, free1=dom2, free2=dom3, free3=dom0).
Verified byte-for-byte compatible against a real harness session before writing
this: same sidecar keys, same audit.jsonl event shape (type/message/role/content
blocks), same account+device UUID nesting. The only wrinkle is the harness signs
each audit line with an HMAC (a sibling .audit-key file) — that's tamper-evidence,
it doesn't change how the file reads.

Every function below defaults to the exact old behavior (LOCAL_ROOT only, no
`root` argument) so nothing that already calls this module needs to change.
Multi-root is opt-in via `root=<label>` or the new discover_all_roots()/
local_roots_config() for callers that want to see everything at once.

Also added the same day, per Jerry's follow-up ("a mechanism to actually
combine roots into a single group is a viable choice"): `root_groups` in
dock.json, a named set of root labels (see root_groups_config()/
resolve_root_group() below) so e.g. `list --local --root-group harness-all`
can show what's docked across every claude-harness profile in one call
instead of naming each one. This is pure config sugar over local_roots --
it doesn't introduce a new storage concept, just a name for a set of
existing ones.

NOT covered by this pass: roaming.py (undock / checkout ledger) is a separate,
unread module — cross-machine roaming of harness sessions is a deliberate
stopping point here, not solved. dock (this file) is the only thing made
multi-root-aware.
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

PRIMARY_ROOT_LABEL = "primary"


class DockError(Exception):
    pass


# ----------------------------------------------------------------------------- config

def _read_dock_config() -> dict:
    if not DOCK_CONFIG.exists():
        return {}
    try:
        return json.loads(DOCK_CONFIG.read_text())
    except json.JSONDecodeError as e:
        raise DockError(f"{DOCK_CONFIG} is not valid JSON: {e}") from e


def load_path_map(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Load the mount-root path map from config, overlaid with CLI --path-map entries."""
    path_map: Dict[str, str] = dict(_read_dock_config().get("path_map", {}))
    if extra:
        path_map.update(extra)
    return path_map


def load_source_dirs(extra: Optional[List[str]] = None) -> List[str]:
    """Directories to scan for dockable sessions (`source_dirs` in dock.json + CLI extras)."""
    dirs = list(_read_dock_config().get("source_dirs", []))
    if extra:
        dirs += extra
    return [os.path.expanduser(d) for d in dirs]


def local_roots_config() -> Dict[str, Path]:
    """All configured local session-tree roots, keyed by label.

    Always includes "primary" -> LOCAL_ROOT. Additional roots come from a
    `local_roots` object in dock.json, e.g.:

        {
          "local_roots": {
            "harness-pro":    "~/.config/claude-harness/pro/session-data/local-agent-mode-sessions",
            "harness-free1":  "~/.config/claude-harness/free1/session-data/local-agent-mode-sessions",
            "harness-free2":  "~/.config/claude-harness/free2/session-data/local-agent-mode-sessions",
            "harness-free3":  "~/.config/claude-harness/free3/session-data/local-agent-mode-sessions"
          }
        }

    A label of "primary" in config would override LOCAL_ROOT itself -- not
    expected, but not blocked either.
    """
    roots: Dict[str, Path] = {PRIMARY_ROOT_LABEL: LOCAL_ROOT}
    for label, p in _read_dock_config().get("local_roots", {}).items():
        roots[label] = Path(p).expanduser()
    return roots


def resolve_root(root: Optional[str]) -> Path:
    """Label -> Path, defaulting to the primary root. Raises on an unknown label."""
    roots = local_roots_config()
    label = root or PRIMARY_ROOT_LABEL
    if label not in roots:
        raise DockError(f"unknown root label {label!r}; configured roots: {sorted(roots)}")
    return roots[label]


def root_groups_config() -> Dict[str, List[str]]:
    """Named groups of root labels, added 2026-08-12 alongside `local_roots` --
    e.g. a dock.json with:

        {
          "local_roots": { "harness-pro": "...", "harness-free1": "...",
                           "harness-free2": "...", "harness-free3": "..." },
          "root_groups": { "harness-all": ["harness-pro", "harness-free1",
                                           "harness-free2", "harness-free3"] }
        }

    lets a caller say --root-group harness-all once instead of naming every
    profile, e.g. for `list --local --root-group harness-all` to show what's
    docked across all four harness profiles in one merged table. This is a
    pure config-level grouping over local_roots_config() -- it doesn't add a
    new kind of root, just a name for a set of existing ones. Purely additive:
    no root_groups key means an empty dict, nothing else changes."""
    return _read_dock_config().get("root_groups", {})


def resolve_root_group(group: str) -> List[Tuple[str, Path]]:
    """Group label -> [(root_label, path), ...], each validated against
    local_roots_config(). Raises if the group itself is unknown, or if it
    references a root label that isn't actually configured (config drift --
    e.g. a root_groups entry surviving after its local_roots entry was
    renamed/removed)."""
    groups = root_groups_config()
    if group not in groups:
        raise DockError(f"unknown root group {group!r}; configured groups: {sorted(groups)}")
    roots = local_roots_config()
    out: List[Tuple[str, Path]] = []
    for label in groups[group]:
        if label not in roots:
            raise DockError(f"root group {group!r} references unknown root label {label!r} "
                            f"(configured roots: {sorted(roots)})")
        out.append((label, roots[label]))
    return out


def scan_sessions(dirs: List[str], account: Optional[str] = None,
                  device: Optional[str] = None, root: Optional[str] = None) -> List[dict]:
    """Enumerate `local_<uuid>` sessions under `dirs` with dock-relevant metadata.

    Each entry: session_id, title, path, folders, scheduled (bool), docked
    (True/False, or None if the local device tree couldn't be resolved).

    `root` (a label from local_roots_config(), default "primary") controls
    which local tree "docked" is checked against -- a harness-originated
    session should be checked against its own harness root, not the primary
    Claude tree, or "docked" will always read False/None even when it's
    already there.
    """
    root_path = resolve_root(root)
    local_dev = acct = None
    try:
        acct, local_dev = discover_local(account, device, root=root)
    except DockError:
        pass  # docked-status stays unknown

    out: List[dict] = []
    seen = set()
    for d in dirs:
        base = Path(d).expanduser()
        if not base.is_dir():
            continue
        for sc in sorted(base.glob("local_*.json")):
            sid = sc.name[:-len(".json")]
            sess_dir = base / sid
            if not sess_dir.is_dir() or sid in seen:
                continue
            seen.add(sid)
            try:
                data = json.loads(sc.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            docked = None
            if local_dev:
                docked = (root_path / acct / local_dev / sid).exists()
            out.append({
                "session_id": sid,
                "title": data.get("title"),
                "path": str(sess_dir),
                "folders": data.get("userSelectedFolders") or [],
                "scheduled": data.get("sessionType") == "scheduled" or bool(data.get("scheduledTaskId")),
                "docked": docked,
                "root": root or PRIMARY_ROOT_LABEL,
            })
    return out


# ------------------------------------------------------------------- local tree probe

def _uuid_children(d: Path) -> List[str]:
    return sorted(c.name for c in d.iterdir() if c.is_dir() and UUID_RE.match(c.name))


def discover_local(account: Optional[str], device: Optional[str],
                   root: Optional[str] = None) -> Tuple[str, str]:
    """Find this machine's account + device UUID dirs under a local root.

    Uses the single UUID-shaped dir at each level; requires an explicit override only when
    the choice is ambiguous. `root` is a label from local_roots_config() -- default
    "primary" (LOCAL_ROOT), so existing callers see zero behavior change.
    """
    root_path = resolve_root(root)
    if not root_path.exists():
        raise DockError(f"local session root not found: {root_path}")
    accounts = _uuid_children(root_path)
    if account is None:
        if len(accounts) != 1:
            raise DockError(f"expected exactly 1 account UUID under {root_path}, found {accounts}; pass --account")
        account = accounts[0]
    elif account not in accounts:
        raise DockError(f"account {account} not found under {root_path} (have {accounts})")

    acct_dir = root_path / account
    devices = _uuid_children(acct_dir)
    if device is None:
        if len(devices) != 1:
            raise DockError(f"expected exactly 1 device UUID under {acct_dir}, found {devices}; pass --device")
        device = devices[0]
    elif device not in devices:
        raise DockError(f"device {device} not found under {acct_dir} (have {devices})")
    return account, device


def discover_all_roots(account: Optional[str] = None, device: Optional[str] = None
                       ) -> List[Tuple[str, str, str, Path]]:
    """Best-effort discover_local() across EVERY configured root.

    Returns one (root_label, account, device, root_path) tuple per root that
    resolves cleanly; roots that don't exist or are ambiguous (multiple
    accounts/devices without an override) are silently skipped -- this is a
    "show me what's here" helper, not a strict one, so one messy root
    shouldn't blank out the rest.
    """
    out = []
    for label, root_path in local_roots_config().items():
        try:
            acct, dev = discover_local(account, device, root=label)
        except DockError:
            continue
        out.append((label, acct, dev, root_path))
    return out


def local_device_dir(account: Optional[str] = None, device: Optional[str] = None,
                     root: Optional[str] = None) -> Path:
    """The live device session dir on this machine — where docked sessions live
    (i.e. the undock candidates). `root` picks which configured tree, default primary."""
    acct, dev = discover_local(account, device, root=root)
    return resolve_root(root) / acct / dev


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
    root_label: str = PRIMARY_ROOT_LABEL     # which configured local tree this docks into


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
    root: Optional[str] = None,
) -> DockPlan:
    """Build a dock plan. `root` (label from local_roots_config(), default "primary")
    picks which local tree this session docks INTO. A harness-originated session
    should generally dock back into its own harness root (e.g. root="harness-pro"),
    not the primary Claude tree -- docking it into "primary" would put a
    harness-only session where the real Claude Desktop app looks, which is
    almost certainly not what you want. Nothing infers this automatically;
    the caller (or --root on the CLI) has to say which tree."""
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

    acct, local_device = discover_local(account, device, root=root)
    root_label = root or PRIMARY_ROOT_LABEL
    root_path = resolve_root(root)

    rewrites: List[Rewrite] = []
    for old in sorted(path_map, key=len, reverse=True):
        c = sidecar_text.count(old)
        if c:
            rewrites.append(Rewrite("path-map", old, path_map[old], c))
    if src_device and src_device != local_device:
        rewrites.append(Rewrite("device", src_device, local_device,
                                sidecar_text.count(src_device)))

    usf_after = [_apply_map(p, path_map) for p in usf_before]

    dst_dir = root_path / acct / local_device / session_id
    dst_sidecar = dst_dir.with_name(session_id + ".json")

    # After rewrite, warn about any selected folder that still won't exist on this host.
    unmapped = [p for p in usf_after if not Path(p).exists()]

    return DockPlan(
        session_id=session_id, title=title, src_dir=src_dir, src_sidecar=src_sidecar,
        account=acct, local_device=local_device, src_device=src_device,
        dst_dir=dst_dir, dst_sidecar=dst_sidecar, collision=dst_dir.exists() or dst_sidecar.exists(),
        usf_before=usf_before, usf_after=usf_after, rewrites=rewrites,
        is_scheduled=is_scheduled, scheduled_task_id=scheduled_task_id, unmapped_stale=unmapped,
        root_label=root_label,
    )


def render_plan(p: DockPlan) -> str:
    L = []
    L.append(f"dock plan: {p.session_id}" + (f"  ({p.title!r})" if p.title else ""))
    L.append(f"  source     : {p.src_dir}")
    L.append(f"  dest root  : {p.root_label}")
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
    p.dst_dir.parent.mkdir(parents=True, exist_ok=True)
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
        "root": p.root_label,
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
