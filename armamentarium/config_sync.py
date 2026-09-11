"""armamentarium.config_sync — fleet config capture/diff/deploy/status.

Implements the design decided 2026-08-26/2026-09-02 in
domain1-arma-fleet-capture-deploy.md, and corrected 2026-09-03 after finding
that block already existed (this module replaces an earlier, duplicate
stoa-hubs/helpers/fleet_config_registry.py — retired, see
Continuatus/arma-fleet/_retired/).

Deliberately a SEPARATE verb group from `armamentarium fleet` — `fleet` already
means machine presence/registry/heartbeats (armamentarium/fleet.py), and
`fleet status` already exists there. Reusing that name for config drift would
collide. This is `armamentarium config capture|diff|deploy|status`.

Store layout (on Continuatus, NFS-shared — every node in the fleet can read
and write it):

    ~/Datasets/Cubicles/Continuatus/arma-fleet/config/<hostname>/
        tracked.txt      # operator-maintained: one path per line, relative to ~/.config
        manifest.json    # generated: {relpath: {updated, size, sha256}}
        config/<relpath> # the actual mirrored copies (mirror root IS ~/.config, not ~)

IMPORTANT: relpath is relative to ~/.config, NOT to $HOME. Fixed 2026-09-03 after a
first version treated paths as $HOME-relative and landed everything one directory too
deep (config/<host>/config/.config/... instead of config/<host>/config/...) — caught
because it didn't match the pre-existing hand-placed Claude/armamentarium/stoa dirs
already sitting directly under config/<host>/config/. If something outside ~/.config
ever needs tracking, that's a distinct future top-level category (e.g. a home/
mirror) — not a reason to make relpath ambiguous between the two roots.

Two separate mechanisms use this one shared format, per the tradeoff worked
out 2026-09-03:
  - THIS module (capture/diff/deploy/status) — a live-host tool, requires a
    full Python env + git, and is explicit/scriptable by design (nothing here
    runs itself; a systemd unit calling `capture` on file-change is a
    separate, optional layer — see config/armamentarium-config-watch.service).
  - A dependency-free consumer script vendored into
    `fleet-offline-repo/manifests/` for bare-netinst provisioning, which reads
    the same manifest.json but never imports this module or requires
    armamentarium to be installed yet — see
    Substrate core/architecture/install-node-config-restore.patch.md.

Design invariants carried over from the original decision:
  - Pure per-machine files, no shared templates/overlays. Cross-machine
    divergence is surfaced by `diff`, reconciled by hand.
  - Runtime state (e.g. ~/.config/stoa/sensor-registry.json) is explicitly
    OUT of scope — that's sensor output, not config to preserve.
  - Nothing auto-syncs from inside this module. capture/deploy are explicit
    calls; the watcher is a separate opt-in wrapper around `capture`.
  - deploy writes atomic (temp+rename) with a `.bak-<timestamp>`, never
    deletes, and refuses per-file on live/manifest hash mismatch unless
    forced (protects an uncaptured hand-edit from being silently clobbered
    by a stale manifest).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Store location. 2026-09-11 (operator decision): the store moves to
# Mechanicum/Profectionis, mounted at ~/Datasets/Profectionis, and Continuatus
# itself moves up from ~/Datasets/Cubicles/ to ~/Datasets/. Resolution order:
#   1. $ARMA_FLEET_ROOT, if set.
#   2. ~/Datasets/Profectionis/fleet-config -- whenever the host HAS a
#      ~/Datasets/Profectionis mountpoint, even if it is empty or offline. No
#      silent fallback to the legacy store once Profectionis is provisioned: a
#      capture landing in a stale store is a split brain, and a loud "no
#      tracked.txt" error is the correct failure (the next scan catches up).
#   3. Legacy Continuatus locations, only on hosts not yet given the mount.
PROFECTIONIS_ROOT = Path("~/Datasets/Profectionis/fleet-config").expanduser()
LEGACY_ROOTS = (
    Path("~/Datasets/Continuatus/arma-fleet").expanduser(),
    Path("~/Datasets/Cubicles/Continuatus/arma-fleet").expanduser(),
)


def resolve_store_root() -> Path:
    env = os.environ.get("ARMA_FLEET_ROOT")
    if env:
        return Path(env).expanduser()
    if PROFECTIONIS_ROOT.parent.is_dir():
        return PROFECTIONIS_ROOT
    for legacy in LEGACY_ROOTS:
        if (legacy / "config").is_dir():
            return legacy
    return PROFECTIONIS_ROOT


DEFAULT_ROOT = resolve_store_root()

# Paths (relative to ~/.config) that are runtime state, not config to preserve,
# per the original 2026-08-26 design decision. `adopt` never proposes these even
# when a live copy sits right there with a matching mirror entry — learned the
# hard way 2026-09-03 when `adopt` picked up stoa/sensor-registry.json on hecate
# (mechanically correct, substantively wrong: it's Hub-read sensor output, not
# something a reprovisioned machine should get handed back as configuration).
RUNTIME_STATE_EXCLUDE = {
    "stoa/sensor-registry.json",
}


class ConfigSyncError(Exception):
    pass


def machine_id() -> str:
    return socket.gethostname().split(".")[0]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_copy(src: Path, dest: Path) -> None:
    _atomic_write_bytes(dest, src.read_bytes())


def _config_home(home: Path) -> Path:
    """The live root relpaths are resolved against: ~/.config, not ~."""
    return home / ".config"


# ------------------------------------------------------------------ paths

def host_dir(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> Path:
    return root / "config" / (host or machine_id())


def tracked_path(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> Path:
    return host_dir(host, root) / "tracked.txt"


def manifest_path(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> Path:
    return host_dir(host, root) / "manifest.json"


def mirror_root(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> Path:
    return host_dir(host, root) / "config"


# ------------------------------------------------------------------ tracked.txt

def read_tracked(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> list[str]:
    p = tracked_path(host, root)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ------------------------------------------------------------------ manifest

def load_manifest(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> dict:
    p = manifest_path(host, root)
    try:
        data = json.loads(p.read_text())
        data.setdefault("files", {})
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": "1.0", "host": host or machine_id(), "files": {}}


def _save_manifest(data: dict, host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> None:
    p = manifest_path(host, root)
    _atomic_write_bytes(p, json.dumps(data, indent=2).encode())


def _git_commit_best_effort(root: Path, message: str) -> Optional[str]:
    """Best-effort git add+commit in the store. Never raises — a missing git
    binary or a store that isn't a repo yet shouldn't block a capture.

    Returns the new commit's short SHA on success, or a human-readable note
    explaining why nothing was committed. 2026-09-11: it used to return None on
    success, so a perfectly good capture reported `"commit": null` and read like
    a failure (it isn't — the commit was there all along)."""
    try:
        if not (root / ".git").is_dir():
            return "not a git repo — skipped commit"
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                        capture_output=True, timeout=15)
        result = subprocess.run(["git", "-C", str(root), "commit", "-m", message],
                                 capture_output=True, timeout=15, text=True)
        if result.returncode != 0:
            # commonly "nothing to commit" — not an error
            return result.stdout.strip() or result.stderr.strip()
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, timeout=15, text=True)
        return rev.stdout.strip() or "committed"
    except Exception as e:  # noqa: BLE001
        return f"git commit skipped: {e}"


# ------------------------------------------------------------------ capture (producer)

def capture(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
            home: Optional[Path] = None) -> dict:
    """Push every tracked path for `host` (default: this machine) into the
    shared store and update its manifest. Explicit call — nothing invokes
    this automatically; see the separate watcher for that."""
    host = host or machine_id()
    home = home or Path.home()
    tracked = read_tracked(host, root)
    manifest = load_manifest(host, root)
    synced, skipped = [], []

    for relpath in tracked:
        src = _config_home(home) / relpath
        if not src.is_file():
            skipped.append(relpath)
            continue
        dest = mirror_root(host, root) / relpath.lstrip("/")
        _atomic_copy(src, dest)
        manifest["files"][relpath] = {
            "updated": _now(),
            "size": dest.stat().st_size,
            "sha256": _sha256(dest),
        }
        synced.append(relpath)

    _save_manifest(manifest, host, root)
    commit_note = _git_commit_best_effort(
        root, f"capture: {host} ({len(synced)} file(s))"
    )
    return {
        "host": host,
        "synced": synced,
        "skipped_missing": skipped,
        "commit": commit_note,
    }


def untrack(relpath: str, host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> None:
    """Drop a path from the manifest (leaves the mirrored copy as history)."""
    host = host or machine_id()
    manifest = load_manifest(host, root)
    manifest["files"].pop(relpath, None)
    _save_manifest(manifest, host, root)


# ------------------------------------------------------------------ diff

@dataclass
class DiffResult:
    host_a: str
    host_b: str
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)
    identical: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "host_a": self.host_a, "host_b": self.host_b,
            "only_in_a": self.only_in_a, "only_in_b": self.only_in_b,
            "differing": self.differing, "identical": self.identical,
        }


def diff(host_a: str, host_b: str, root: Path = DEFAULT_ROOT) -> DiffResult:
    """Compare two hosts' captured manifests (store-vs-store, not live files —
    run `capture` on both first for a current comparison)."""
    ma = load_manifest(host_a, root)["files"]
    mb = load_manifest(host_b, root)["files"]
    result = DiffResult(host_a=host_a, host_b=host_b)
    for relpath in sorted(set(ma) | set(mb)):
        if relpath not in mb:
            result.only_in_a.append(relpath)
        elif relpath not in ma:
            result.only_in_b.append(relpath)
        elif ma[relpath]["sha256"] != mb[relpath]["sha256"]:
            result.differing.append(relpath)
        else:
            result.identical.append(relpath)
    return result


# ------------------------------------------------------------------ deploy (self-restore/rollback)

def deploy(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
           home: Optional[Path] = None, force: bool = False) -> dict:
    """Restore this host's own tracked files from the store back onto itself
    — a rollback/self-heal operation (last-known-good from the store), NOT a
    live-to-live push. `host` should normally be left as this machine;
    passing another host's name is a deliberate, manual clone of that host's
    config onto this one and is on the caller to intend.

    Refuses (per-file, not whole-run) when the live file's current hash
    doesn't match what's on record for it — that means the live file has
    uncaptured edits, and deploying over it would silently lose them. Pass
    force=True to overwrite anyway (still takes a .bak first)."""
    host = host or machine_id()
    home = home or Path.home()
    manifest = load_manifest(host, root)
    deployed, refused, restored_missing = [], [], []

    for relpath, meta in manifest["files"].items():
        src = mirror_root(host, root) / relpath.lstrip("/")
        if not src.is_file():
            continue  # manifest says tracked, mirror missing — nothing to deploy for this one
        dest = _config_home(home) / relpath

        if dest.exists() and not force:
            live_hash = _sha256(dest)
            if live_hash != meta.get("sha256"):
                refused.append(relpath)
                continue

        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = dest.with_name(dest.name + f".bak-{stamp}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            restored_missing.append(relpath)

        _atomic_copy(src, dest)
        deployed.append(relpath)

    return {
        "host": host,
        "deployed": deployed,
        "restored_missing": restored_missing,
        "refused_drift": refused,
        "note": ("refused files had live edits not yet captured — run `capture` "
                 "first, or re-run with force=True to discard them") if refused else None,
    }


# ------------------------------------------------------------------ adopt (onboard pre-existing mirror content)

def discover_untracked(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
                       home: Optional[Path] = None) -> dict:
    """Find files already sitting in the mirror tree that aren't in
    tracked.txt yet — e.g. config hand-placed there before this tool existed
    (the original arma-fleet-capture-deploy incident captured
    armamentarium/Claude/stoa config by hand on 2026-08-26, well before
    `capture` existed to track it).

    Splits results: 'new' has a live counterpart under ~/.config and is safe
    to adopt; 'no_live_source' is mirror-only (moved, renamed, or from a
    machine that's since changed) and is surfaced for a human to look at,
    never silently adopted or discarded."""
    host = host or machine_id()
    home = home or Path.home()
    tracked = set(read_tracked(host, root))
    mroot = mirror_root(host, root)
    new, no_live = [], []
    if mroot.is_dir():
        for p in mroot.rglob("*"):
            if not p.is_file():
                continue
            relpath = str(p.relative_to(mroot))
            if relpath in tracked:
                continue
            if relpath in RUNTIME_STATE_EXCLUDE:
                continue
            live = _config_home(home) / relpath
            (new if live.is_file() else no_live).append(relpath)
    return {"new": sorted(new), "no_live_source": sorted(no_live)}


def adopt(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
          home: Optional[Path] = None) -> dict:
    """Add every discovered untracked-but-mirrored file with a live source to
    tracked.txt (append-only — never rewrites or reorders existing lines).
    Does NOT run `capture` itself and does NOT touch mirror-only files with
    no live source; both are left for the caller to act on deliberately."""
    host = host or machine_id()
    found = discover_untracked(host, root, home)
    if found["new"]:
        p = tracked_path(host, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text() if p.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        header = f"# adopted {_now()} (pre-existing mirror content)\n"
        p.write_text(existing + header + "\n".join(found["new"]) + "\n")
    return found


# ------------------------------------------------------------------ status

def status(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
            home: Optional[Path] = None) -> dict:
    """Summary for one host: tracked count, captured count, last capture
    time, and — only when checking THIS machine — live-vs-store drift for
    each tracked file."""
    host = host or machine_id()
    home = home or Path.home()
    tracked = read_tracked(host, root)
    manifest = load_manifest(host, root)
    files = manifest["files"]

    last_capture = max((v["updated"] for v in files.values()), default=None)
    never_captured = [p for p in tracked if p not in files]

    drift = None
    if host == machine_id():
        drift = []
        for relpath in tracked:
            if relpath not in files:
                continue
            live = _config_home(home) / relpath
            if not live.is_file():
                drift.append({"path": relpath, "state": "missing_locally"})
                continue
            if _sha256(live) != files[relpath]["sha256"]:
                drift.append({"path": relpath, "state": "uncaptured_local_edit"})

    return {
        "host": host,
        "tracked_count": len(tracked),
        "captured_count": len(files),
        "never_captured": never_captured,
        "last_capture": last_capture,
        "drift": drift,
    }
