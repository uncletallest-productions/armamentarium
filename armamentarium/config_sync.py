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
  - 2026-11-24: the store is AGENT-WRITABLE, so deploy is a dry-run plan by default;
    writing needs --apply at a real terminal + y/N, code-running files need their name
    typed, and armamentarium/{exec,router}.json are never written (see POLICY_FILES).
  - 2026-11-24: the AUTHORITATIVE tracked list is ~/.config/armamentarium/tracked.txt on
    each host (no agent root reaches it). The store's config/<host>/tracked.txt is a
    generated copy, rewritten by capture, read only for diff/other-host views. A
    secret deny list (SECRET_*) is enforced by capture and adopt as a backstop.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

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

# ---- deploy trust model (2026-11-24, domain1-arma-exec "deploy trust hole") ----------------
# The store (tracked.txt, manifest.json, the mirrored copies) lives under ~/Datasets, which
# agents can WRITE. Deploy installs store content into ~/.config, where several files run code
# (login autostart, user units, MCP server commands, arma's own policy). So deploy must never
# trust the store: it is a dry-run plan by default, --apply needs a real terminal and a y/N,
# code-running files additionally need their file name typed, and the two policy files are
# never written at all (diff + a reviewed-snapshot install command instead).

# Never written by deploy, even with --apply: they decide what agents may do.
POLICY_FILES = {"armamentarium/exec.json", "armamentarium/router.json"}
# Never captured or deployed: runtime grants, host-local by design.
NEVER_SYNC = {"armamentarium/elevate.json"}
# Files that run commands (at login, on activation, or as configured programs). Best effort:
# every change is diffed in the plan anyway; these also need the file name typed to apply.
CODE_RUNNER_EXACT = {"Claude/claude_desktop_config.json"}
CODE_RUNNER_PREFIXES = ("labwc/", "kanshi/", "systemd/", "autostart/", "environment.d/")
# Reviewed snapshots for policy files live where no agent root reaches.
REVIEW_DIR = Path("~/.local/state/armamentarium/deploy-review").expanduser()
DIFF_MAX_LINES = 400

# Authoritative per-host tracked list, relative to ~ (see local_tracked_path()).
LOCAL_TRACKED_REL = ".config/armamentarium/tracked.txt"
GENERATED_HEADER = ("# GENERATED by `armamentarium config capture` from "
                    "~/.config/armamentarium/tracked.txt on {host} at {ts}.\n"
                    "# Edits here are IGNORED. Edit the local file on that host.\n")
# Never captured (backstop: the tracked list should never name these anyway).
SECRET_PREFIXES = ("gh/", "hub/", "rclone/", "gcloud/", "gnupg/", "keepassxc/", "Bitwarden",
                   "1Password/", "op/", "google-chrome/", "chromium/", "BraveSoftware/",
                   "vivaldi/", "Signal/", "discord/", "Slack/", "git/credentials",
                   "tailscale/", "syncthing/", "armamentarium/elevate.json")
SECRET_ALLOW_EXACT = {"Claude/claude_desktop_config.json"}   # the only Claude/ file allowed
SECRET_NAME_PARTS = ("token", "secret", "credential", "password", "passwd", "cookie",
                     "session", "auth")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".gpg", ".asc")


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


def _within(path: Path, base: Path) -> bool:
    """True if ``path`` (after resolving symlinks in it) is ``base`` or below it."""
    p, b = os.path.realpath(path), os.path.realpath(base)
    return p == b or p.startswith(b.rstrip(os.sep) + os.sep)


def unsafe_relpath(relpath: str) -> Optional[str]:
    """Why ``relpath`` may not be synced, or None. tracked.txt and manifest.json are
    agent-writable, so a line like '../.ssh/id_ed25519' must not escape ~/.config."""
    if not relpath or relpath != relpath.strip():
        return "empty or padded path"
    p = PurePosixPath(relpath)
    if p.is_absolute():
        return "absolute path"
    if ".." in p.parts or "." in p.parts:
        return "path contains '..' or '.'"
    if relpath in NEVER_SYNC:
        return "runtime grants file, never synced"
    return None


def secret_reason(relpath: str) -> Optional[str]:
    """Why ``relpath`` looks like a secret and must not be captured, or None."""
    if relpath in SECRET_ALLOW_EXACT:
        return None
    if relpath.startswith("Claude/"):
        return "Claude/ holds app state and tokens; only claude_desktop_config.json is allowed"
    if relpath.startswith(SECRET_PREFIXES):
        return "under a known credential/profile directory"
    name = PurePosixPath(relpath).name.lower()
    if name.startswith("id_") or name.endswith(SECRET_SUFFIXES):
        return "looks like a key file"
    for part in SECRET_NAME_PARTS:
        if part in name:
            return f"file name contains '{part}'"
    return None


def is_code_runner(relpath: str, data: Optional[bytes] = None, mode: Optional[int] = None) -> bool:
    if relpath in CODE_RUNNER_EXACT or relpath.startswith(CODE_RUNNER_PREFIXES):
        return True
    if data is not None and data[:2] == b"#!":
        return True
    return bool(mode is not None and mode & 0o111)


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

def _parse_tracked(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def read_tracked(host: Optional[str] = None, root: Path = DEFAULT_ROOT) -> list[str]:
    """The STORE copy of a host's list. Informational only (agent-writable): used for
    diff and other-host views, never to decide what this host captures or deploys."""
    p = tracked_path(host, root)
    if not p.is_file():
        return []
    return _parse_tracked(p.read_text())


def local_tracked_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / LOCAL_TRACKED_REL


def read_authoritative_tracked(home: Optional[Path] = None, host: Optional[str] = None,
                               root: Path = DEFAULT_ROOT) -> list[str]:
    """This host's real tracked list: ~/.config/armamentarium/tracked.txt, which no agent root
    reaches. Refuses (fail-closed) if it is missing, a symlink, not owned by this user, or
    group/world-writable -- the same rules as exec.json."""
    p = local_tracked_path(home)
    store_copy = tracked_path(host, root)
    if not p.exists() and not p.is_symlink():
        raise ConfigSyncError(
            f"no local tracked list at {p}. It is the authoritative list now (the store copy is "
            f"agent-writable). Read the store copy, then install it if it is right:\n"
            f"    less {store_copy}\n"
            f"    install -D -m 0644 {store_copy} {p}")
    st = os.lstat(p)
    if os.path.islink(p) or not os.path.isfile(p):
        raise ConfigSyncError(f"{p} must be a regular file, not a symlink")
    if st.st_uid != os.getuid():
        raise ConfigSyncError(f"{p} is owned by uid {st.st_uid}, not you ({os.getuid()})")
    if st.st_mode & 0o022:
        raise ConfigSyncError(f"{p} is group/world-writable; chmod 0644 it")
    return _parse_tracked(p.read_text())


def _write_generated_store_copy(tracked: list[str], host: str, root: Path) -> None:
    header = GENERATED_HEADER.format(host=host, ts=_now())
    _atomic_write_bytes(tracked_path(host, root), (header + "\n".join(tracked) + "\n").encode())


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


def _git_commit_best_effort(root: Path, message: str,
                            paths: Optional[list[str]] = None) -> Optional[str]:
    """Best-effort git add+commit in the store. Never raises — a missing git
    binary or a store that isn't a repo yet shouldn't block a capture.

    Returns the new commit's short SHA on success, or a human-readable note
    explaining why nothing was committed. 2026-09-11: it used to return None on
    success, so a perfectly good capture reported `"commit": null` and read like
    a failure (it isn't — the commit was there all along).

    2026-11-24: ``paths`` (store-relative) limits BOTH staging and the commit to those paths.
    It used to `add -A` the whole store, so an unrelated uncommitted edit landed in a
    commit labelled "capture: <host>" (seen: sisyphus/tracked.txt inside defcaba)."""
    try:
        if not (root / ".git").is_dir():
            return "not a git repo — skipped commit"
        scope = ["--", *paths] if paths else []
        subprocess.run(["git", "-C", str(root), "add", "-A", *scope], check=True,
                        capture_output=True, timeout=15)
        result = subprocess.run(["git", "-C", str(root), "commit", "-m", message, *scope],
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
    tracked = read_authoritative_tracked(home, host, root)
    manifest = load_manifest(host, root)
    synced, skipped, refused = [], [], []

    for relpath in tracked:
        why = unsafe_relpath(relpath) or secret_reason(relpath)
        src = _config_home(home) / relpath
        if why is None and not _within(src, _config_home(home)):
            why = "resolves outside ~/.config (symlink)"
        if why:
            refused.append({"path": relpath, "reason": why})
            continue
        if not src.is_file():
            skipped.append(relpath)
            continue
        dest = mirror_root(host, root) / relpath
        if not _within(dest.parent, mirror_root(host, root)):   # before mkdir: no dirs via a link
            refused.append({"path": relpath, "reason": "mirror dir resolves outside the store"})
            continue
        _atomic_copy(src, dest)
        manifest["files"][relpath] = {
            "updated": _now(),
            "size": dest.stat().st_size,
            "sha256": _sha256(dest),
        }
        synced.append(relpath)

    _save_manifest(manifest, host, root)
    _write_generated_store_copy(tracked, host, root)
    commit_note = _git_commit_best_effort(
        root, f"capture: {host} ({len(synced)} file(s))",
        paths=[str(host_dir(host, root).relative_to(root))],
    )
    return {
        "host": host,
        "synced": synced,
        "skipped_missing": skipped,
        "refused_unsafe": refused,
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

@dataclass
class PlanEntry:
    path: str
    action: str            # unchanged | create | update | policy | refused | missing_mirror
    reason: str = ""
    code_runner: bool = False
    diff: str = ""
    review_cmd: str = ""   # policy files: the install command for the reviewed snapshot
    data: Optional[bytes] = field(default=None, repr=False)   # EXACT bytes that would be written
    live_sha: Optional[str] = None                             # live hash at plan time

    def as_dict(self) -> dict:
        d = {"path": self.path, "action": self.action, "code_runner": self.code_runner}
        for k in ("reason", "diff", "review_cmd"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


@dataclass
class DeployPlan:
    host: str
    home: Path
    entries: list[PlanEntry] = field(default_factory=list)

    def by_action(self, *actions: str) -> list[PlanEntry]:
        return [e for e in self.entries if e.action in actions]

    def as_dict(self) -> dict:
        return {"host": self.host, "entries": [e.as_dict() for e in self.entries]}


def _diff_text(old: Optional[bytes], new: bytes, relpath: str) -> str:
    if (old and b"\0" in old) or b"\0" in new:
        return f"(binary: {len(old or b'')} -> {len(new)} bytes)"
    a = (old or b"").decode("utf-8", "replace").splitlines(keepends=True)
    b = new.decode("utf-8", "replace").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"live/{relpath}", f"store/{relpath}"))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... ({len(lines) - DIFF_MAX_LINES} more diff lines)\n"]
    return "".join(lines)


def plan_deploy(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
                home: Optional[Path] = None, force: bool = False) -> DeployPlan:
    """Work out what deploy WOULD do. Writes nothing (except reviewed snapshots of
    changed policy files under REVIEW_DIR, which is outside every agent root).

    The store is agent-writable, so nothing in it is trusted: every entry must be in BOTH
    the manifest and tracked.txt, have a safe relpath that stays inside ~/.config, and come
    from a regular (non-symlink) file inside the mirror. The bytes read here are the bytes
    apply_deploy writes, so the store can't change between review and write.

    Drift rule kept from v1: a live file whose hash differs from the manifest has uncaptured
    edits and is refused unless force=True. NB that check does NOT detect a tampered
    store (an attacker leaves the manifest alone); the diff and the confirmations do."""
    host = host or machine_id()
    home = home or Path.home()
    cfg_home = _config_home(home)
    mroot = mirror_root(host, root)
    tracked = set(read_authoritative_tracked(home, host, root))
    plan = DeployPlan(host=host, home=home)

    for relpath, meta in sorted(load_manifest(host, root)["files"].items()):
        e = PlanEntry(path=relpath, action="refused", code_runner=is_code_runner(relpath))
        plan.entries.append(e)
        why = unsafe_relpath(relpath)
        if why:
            e.reason = why
            continue
        if relpath not in tracked:
            e.reason = "in manifest.json but not in ~/.config/armamentarium/tracked.txt"
            continue
        src, dest = mroot / relpath, cfg_home / relpath
        if not _within(dest.parent, cfg_home):
            e.reason = "destination resolves outside ~/.config (symlink)"
            continue
        if not src.exists() and not src.is_symlink():
            e.action, e.reason = "missing_mirror", "no mirrored copy in the store"
            continue
        if src.is_symlink() or not src.is_file() or not _within(src, mroot):
            e.reason = "store copy is a symlink, not a regular file, or outside the store"
            continue
        data = src.read_bytes()
        e.data = data
        e.code_runner = is_code_runner(relpath, data, src.stat().st_mode)
        live = dest.read_bytes() if dest.is_file() else None
        e.live_sha = hashlib.sha256(live).hexdigest() if live is not None else None

        if live is not None and not force and e.live_sha != meta.get("sha256"):
            e.reason = ("live file has uncaptured edits (hash differs from manifest): "
                        "run `capture` first, or --force to discard them")
            e.diff = _diff_text(live, data, relpath)
            continue
        if live == data:
            e.action = "unchanged"
            continue
        e.diff = _diff_text(live, data, relpath)
        if relpath in POLICY_FILES:
            e.action = "policy"
            sha = hashlib.sha256(data).hexdigest()
            snap = REVIEW_DIR / f"{relpath.replace('/', '__')}.{sha[:12]}"
            _atomic_write_bytes(snap, data)
            os.chmod(snap, 0o600)
            e.reason = ("policy file: deploy never writes it. Read the diff; if it is right, "
                        "install the reviewed snapshot (same bytes as the diff):")
            e.review_cmd = f"install -m 0644 {snap} {dest}"
            continue
        e.action = "update" if live is not None else "create"
    return plan


def render_plan(plan: DeployPlan, show_diffs: bool = True) -> str:
    out = [f"deploy plan for {plan.host} -> {_config_home(plan.home)}"]
    marks = {"unchanged": "=", "create": "+", "update": "~", "policy": "!",
             "refused": "x", "missing_mirror": "?"}
    for e in plan.entries:
        tag = " [runs code: type its name to apply]" if e.code_runner and e.action in (
            "create", "update") else ""
        out.append(f"  {marks.get(e.action, ' ')} {e.action:14} {e.path}{tag}")
        if e.reason:
            out.append(f"      {e.reason}")
        if e.review_cmd:
            out.append(f"      {e.review_cmd}")
    if show_diffs:
        for e in plan.entries:
            if e.diff and e.action != "unchanged":
                out.append("")
                out.append(e.diff.rstrip("\n"))
    pending = plan.by_action("create", "update")
    out.append("")
    out.append(f"{len(pending)} file(s) would be written; "
               f"{len(plan.by_action('policy'))} policy file(s) need a manual install; "
               f"{len(plan.by_action('refused'))} refused.")
    return "\n".join(out)


def _default_isatty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def apply_deploy(plan: DeployPlan, prompt: Callable[[str], str] = input,
                 out: Callable[[str], None] = print,
                 isatty: Callable[[], bool] = _default_isatty) -> dict:
    """Write a plan's create/update entries, only with a human at a terminal.

    One y/N (default No) for the whole set, then for each code-running file its diff again
    and the file NAME typed back. Policy and refused entries are never written. Each write:
    live file re-hashed (skip if it changed since the plan), .bak-<ts> taken, atomic
    replace with the PLANNED bytes, existing mode kept (new files 0644, never the store's
    mode, so an exec bit can't ride in from the store)."""
    if not isatty():
        raise ConfigSyncError(
            "refusing to apply: no terminal. deploy --apply needs a human to confirm "
            "(stdin and stdout must be a tty); run it yourself in a shell.")
    pending = plan.by_action("create", "update")
    result = {"host": plan.host, "applied": [], "skipped_by_operator": [],
              "skipped_changed_since_plan": [], "aborted": False}
    if not pending:
        out("nothing to apply.")
        return result
    answer = prompt(f"Write {len(pending)} file(s) into {_config_home(plan.home)}? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        result["aborted"] = True
        out("aborted, nothing written.")
        return result
    cfg_home = _config_home(plan.home)
    for e in pending:
        if e.code_runner:
            name = PurePosixPath(e.path).name
            out("")
            out(e.diff.rstrip("\n") or f"(new file {e.path})")
            typed = prompt(f"{e.path} can run code. Type '{name}' to write it "
                           f"(anything else skips): ")
            if typed.strip() != name:
                result["skipped_by_operator"].append(e.path)
                out(f"  skipped {e.path}")
                continue
        dest = cfg_home / e.path
        now = dest.read_bytes() if dest.is_file() else None
        now_sha = hashlib.sha256(now).hexdigest() if now is not None else None
        if now_sha != e.live_sha or not _within(dest.parent, cfg_home):
            result["skipped_changed_since_plan"].append(e.path)
            out(f"  skipped {e.path}: changed since the plan was made; re-run deploy")
            continue
        mode = 0o644
        if now is not None:
            mode = dest.stat().st_mode & 0o7777
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(dest, dest.with_name(dest.name + f".bak-{stamp}"))
        _atomic_write_bytes(dest, e.data)
        os.chmod(dest, mode)
        result["applied"].append(e.path)
        out(f"  wrote {e.path}")
    return result


def deploy(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
           home: Optional[Path] = None, force: bool = False) -> dict:
    """Compatibility wrapper: returns the PLAN only (dry run), never writes ~/.config.
    Writing goes through apply_deploy(), which needs a terminal. v1 wrote immediately."""
    return plan_deploy(host, root, home, force).as_dict()


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
    tracked = set(read_authoritative_tracked(home, host, root))
    mroot = mirror_root(host, root)
    new, no_live, secret = [], [], []
    if mroot.is_dir():
        for p in mroot.rglob("*"):
            if not p.is_file():
                continue
            relpath = str(p.relative_to(mroot))
            if relpath in tracked:
                continue
            if relpath in RUNTIME_STATE_EXCLUDE:
                continue
            why = unsafe_relpath(relpath) or secret_reason(relpath)
            if why:
                secret.append({"path": relpath, "reason": why})
                continue
            live = _config_home(home) / relpath
            (new if live.is_file() else no_live).append(relpath)
    return {"new": sorted(new), "no_live_source": sorted(no_live),
            "refused": sorted(secret, key=lambda x: x["path"])}


def adopt(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
          home: Optional[Path] = None, apply: bool = False) -> dict:
    """Propose (default) or, with apply=True, append every discovered untracked-but-
    mirrored file with a live source to the LOCAL tracked list (append-only).

    Dry-run by default since 2026-11-24: the mirror is agent-writable, so a file planted
    there next to a live secret would otherwise be adopted -- and then captured -- without
    the operator ever reading its name. Secret-looking and unsafe paths are never proposed.
    Does NOT run `capture` and does NOT touch mirror-only files with no live source."""
    host = host or machine_id()
    home = home or Path.home()
    found = discover_untracked(host, root, home)
    found["applied"] = False
    if apply and found["new"]:
        p = local_tracked_path(home)
        existing = p.read_text()   # discover_untracked already validated it
        if existing and not existing.endswith("\n"):
            existing += "\n"
        header = f"# adopted {_now()} (pre-existing mirror content)\n"
        _atomic_write_bytes(p, (existing + header + "\n".join(found["new"]) + "\n").encode())
        os.chmod(p, 0o644)
        found["applied"] = True
    return found


# ------------------------------------------------------------------ status

def status(host: Optional[str] = None, root: Path = DEFAULT_ROOT,
            home: Optional[Path] = None) -> dict:
    """Summary for one host: tracked count, captured count, last capture
    time, and — only when checking THIS machine — live-vs-store drift for
    each tracked file."""
    host = host or machine_id()
    home = home or Path.home()
    local = host == machine_id()
    # this host: the authoritative local list; another host: its (informational) store copy
    tracked = read_authoritative_tracked(home, host, root) if local else read_tracked(host, root)
    manifest = load_manifest(host, root)
    files = manifest["files"]

    last_capture = max((v["updated"] for v in files.values()), default=None)
    never_captured = [p for p in tracked if p not in files]

    drift = None
    if local:
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
