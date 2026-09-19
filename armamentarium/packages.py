"""armamentarium.packages — below-decks package-state provider/consumer.

First stub of the package-management surface scoped 2026-09-18: config/mount
state already has a home (armamentarium.config_sync -> Profectionis/arma-fleet)
and dataset/ZFS state has one (substrate-replay, stoa/storage), but package
state (apt, Flatpak, Snap, AppImage) had no provider yet. Deliberately a
top-level verb (`armamentarium packages <surface> <action>`), separate from
both `fleet` (machine presence) and `config` (dotfiles) for the same reason
those two stayed separate from each other: each package surface has its own
install/update mechanism, and collapsing them into one verb would blur that.

Scoped surfaces (2026-09-18 discussion, not all implemented yet):
  - apt/Nala   - capture = manually-installed set (`apt-mark showmanual`), not
                 exact pinned versions. THIS MODULE's first real action,
                 `apt_sync_cache`, is narrower than that: it automates the
                 existing manual two-step "copy new .debs to haphaestus, then
                 reindex" workflow (a repo-population action), not yet the
                 full manifest capture/restore of what's installed.
  - flatpak    - capture = app-id + remote + version; restore = plain
                 `flatpak install`. Not yet implemented.
  - snap       - capture = name + revision + channel; restore = plain
                 `snap install`. Not yet implemented.
  - appimage   - capture = name/version + the archived binary itself, stored
                 in Horreum's binaries/ (the same mechanism already used for
                 nvidia driver entries in fleet-offline-repo/manifests/
                 snippets.txt), so restore never needs a source URL. GearLever
                 (itself an ordinary Flatpak entry) handles in-place updates
                 from that archived copy afterward. Not yet implemented.

`apt_sync_cache` replaces Jerry's manual process: copy new .debs out of a
fleet machine's local apt cache (/var/cache/apt/archives/*.deb) into
haphaestus's flat-file repo (Horreum/fleet-offline-repo/), then trigger that
host's existing /usr/local/bin/update-fleet-repo to reindex
(dpkg-scanpackages -> Packages.gz). This is the CLIENT side of that
workflow — it runs on the fleet machine that just installed something, not
on haphaestus itself.

Config: ~/.config/armamentarium/packages.json, e.g.:

    {
      "apt": {
        "host": "haphaestus",
        "remote_repo_dir": "/srv/Archivum/Horreum/fleet-offline-repo",
        "remote_reindex_cmd": "sudo update-fleet-repo",
        "cache_dir": "/var/cache/apt/archives",
        "ssh_identity": "~/.ssh/id_fleet_stoa"
      }
    }

All fields optional except "host" — the rest default to the values shown
above, which match haphaestus's current setup (fleet-repo.service serving
that same directory flat over HTTP on :8080, per systemd unit inspected
2026-09-18).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path("~/.config/armamentarium/packages.json").expanduser()

_APT_DEFAULTS = {
    "host": None,
    "remote_repo_dir": "/srv/Archivum/Horreum/fleet-offline-repo",
    "remote_reindex_cmd": "sudo update-fleet-repo",
    "cache_dir": "/var/cache/apt/archives",
    "ssh_identity": None,
}


class PackagesError(Exception):
    pass


def _config_path(config_path: Optional[Path] = None) -> Path:
    env = os.environ.get("ARMAMENTARIUM_PACKAGES_CONFIG")
    if config_path:
        return Path(config_path).expanduser()
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH


def load_apt_config(config_path: Optional[Path] = None) -> dict:
    """Load the "apt" section of packages.json, filled out with defaults.
    Raises PackagesError if no host is configured anywhere (there's no sane
    default for where a fleet machine's cache should sync to)."""
    p = _config_path(config_path)
    data = {}
    if p.is_file():
        try:
            data = json.loads(p.read_text()).get("apt", {})
        except json.JSONDecodeError as e:
            raise PackagesError(f"malformed config at {p}: {e}") from e
    cfg = {**_APT_DEFAULTS, **data}
    if not cfg["host"]:
        raise PackagesError(
            f"no apt.host configured — set it in {p} "
            '(e.g. {"apt": {"host": "haphaestus"}})'
        )
    return cfg


@dataclass
class AptSyncResult:
    host: str
    dry_run: bool
    rsync_cmd: list
    rsync_returncode: Optional[int]
    rsync_output: str
    reindex_cmd: Optional[list] = None
    reindex_returncode: Optional[int] = None
    reindex_output: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "dry_run": self.dry_run,
            "rsync_cmd": self.rsync_cmd,
            "rsync_returncode": self.rsync_returncode,
            "rsync_output": self.rsync_output,
            "reindex_cmd": self.reindex_cmd,
            "reindex_returncode": self.reindex_returncode,
            "reindex_output": self.reindex_output,
        }


def apt_sync_cache(config_path: Optional[Path] = None, dry_run: bool = False,
                    timeout: int = 300) -> AptSyncResult:
    """rsync new .debs from this machine's apt cache to the configured
    host's fleet-offline-repo, then trigger its reindex script over ssh.

    --ignore-existing means this is safe to run repeatedly / on a timer:
    only .debs not already present on the remote are copied, and the
    reindex is cheap (dpkg-scanpackages over the whole flat dir) so running
    it even when nothing new landed is harmless, just a little wasteful.

    dry_run=True runs rsync with --dry-run (so you see what WOULD copy) and
    skips the remote reindex entirely.
    """
    cfg = load_apt_config(config_path)
    host = cfg["host"]
    cache_glob = str(Path(cfg["cache_dir"]).expanduser() / "*.deb")
    remote_dest = f"{host}:{cfg['remote_repo_dir']}/"

    rsync_cmd = ["rsync", "-av", "--ignore-existing"]
    if dry_run:
        rsync_cmd.append("--dry-run")
    if cfg["ssh_identity"]:
        identity = Path(cfg["ssh_identity"]).expanduser()
        rsync_cmd += ["-e", f"ssh -i {identity}"]
    # Let the shell expand the glob against whatever .debs actually exist
    # right now; rsync itself doesn't glob.
    full_cmd = " ".join(rsync_cmd) + f" {cache_glob} {remote_dest}"

    try:
        proc = subprocess.run(full_cmd, shell=True, capture_output=True,
                               text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise PackagesError(f"rsync to {host} timed out after {timeout}s") from e

    result = AptSyncResult(
        host=host, dry_run=dry_run, rsync_cmd=rsync_cmd,
        rsync_returncode=proc.returncode,
        rsync_output=(proc.stdout or "") + (proc.stderr or ""),
    )

    if proc.returncode != 0:
        raise PackagesError(
            f"rsync to {host} failed (exit {proc.returncode}):\n{result.rsync_output}"
        )

    if dry_run:
        return result

    ssh_cmd = ["ssh"]
    if cfg["ssh_identity"]:
        ssh_cmd += ["-i", str(Path(cfg["ssh_identity"]).expanduser())]
    ssh_cmd += [host, cfg["remote_reindex_cmd"]]

    try:
        reindex = subprocess.run(ssh_cmd, capture_output=True, text=True,
                                  timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise PackagesError(
            f"reindex trigger on {host} timed out after {timeout}s "
            f"(debs were still synced — rsync succeeded)"
        ) from e

    result.reindex_cmd = ssh_cmd
    result.reindex_returncode = reindex.returncode
    result.reindex_output = (reindex.stdout or "") + (reindex.stderr or "")

    if reindex.returncode != 0:
        raise PackagesError(
            f"debs synced to {host}, but reindex failed (exit {reindex.returncode}):\n"
            f"{result.reindex_output}"
        )

    return result
