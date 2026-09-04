"""Configuration + the allowlist that is the router's security boundary.

The router serves nothing unless a config explicitly grants roots (fail-closed).
Config resolution order:

1. Path in ``$ARMAMENTARIUM_ROUTER_CONFIG`` (JSON).
2. ``~/.config/armamentarium/router.json`` if present.
3. Otherwise: empty config → the server starts but every path is denied until
   a config is supplied.

Config schema (JSON)::

    {
      "roots": [
        {"path": "~/Datasets/Substrate", "access": "rw"},
        {"path": "~/.config/Claude/local-agent-mode-sessions", "access": "rw"},
        {"path": "~/Datasets/Gastronomicum", "access": "r"}
      ],
      "backends": ["local"]
    }

``access`` is ``"r"`` (read-only) or ``"rw"`` (read + write). ``backends`` is
forward-looking; only ``"local"`` is wired in this prototype.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG_PATH = Path("~/.config/armamentarium/router.json").expanduser()

# The elevation sentinel: a SEPARATE file from router.json, written only by the
# operator's own shell (via the arma-elevate helper — never by the router, the
# server, or an agent). Its presence grants extra roots on top of the base
# allowlist until expires_at; absence or expiry means it grants nothing. See
# load_elevation() below and router.py's resolve() for how it's consulted.
ELEVATION_PATH = Path("~/.config/armamentarium/elevate.json").expanduser()

VALID_ACCESS = ("r", "rw")


@dataclass(frozen=True)
class Root:
    """A single allowlisted root and its access level.

    ``path`` is stored fully resolved (``~`` and symlinks expanded) so that
    membership checks in the router are a simple prefix test against an
    already-canonical path.
    """

    path: str          # canonical, resolved absolute path
    access: str        # "r" or "rw"
    raw: str           # the path exactly as written in config (for messages)

    @property
    def writable(self) -> bool:
        return self.access == "rw"


@dataclass
class RouterConfig:
    roots: List[Root] = field(default_factory=list)
    backends: List[str] = field(default_factory=lambda: ["local"])
    source: Optional[str] = None  # where the config was loaded from

    def is_empty(self) -> bool:
        return not self.roots


def _resolve_root(entry: dict) -> Root:
    if "path" not in entry:
        raise ValueError(f"root entry missing 'path': {entry!r}")
    raw = entry["path"]
    access = entry.get("access", "r")
    if access not in VALID_ACCESS:
        raise ValueError(
            f"root {raw!r} has invalid access {access!r}; expected one of {VALID_ACCESS}"
        )
    resolved = os.path.realpath(Path(raw).expanduser())
    return Root(path=resolved, access=access, raw=raw)


def load_config(config_path: Optional[str] = None) -> RouterConfig:
    """Load and validate router config, resolving the allowlist.

    Args:
        config_path: explicit path override. If ``None``, falls back to
            ``$ARMAMENTARIUM_ROUTER_CONFIG`` then ``~/.config/armamentarium/router.json``.

    Returns:
        RouterConfig. If no config file is found, returns an empty (deny-all)
        config rather than raising — the server should start and report the
        situation, not crash.

    Raises:
        ValueError: if a config file exists but is malformed.
    """
    path: Optional[Path] = None
    if config_path:
        path = Path(config_path).expanduser()
    elif os.environ.get("ARMAMENTARIUM_ROUTER_CONFIG"):
        path = Path(os.environ["ARMAMENTARIUM_ROUTER_CONFIG"]).expanduser()
    elif DEFAULT_CONFIG_PATH.exists():
        path = DEFAULT_CONFIG_PATH

    if path is None:
        return RouterConfig(roots=[], backends=["local"], source=None)

    if not path.exists():
        raise ValueError(f"config path does not exist: {path}")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"config {path} is not valid JSON: {e}") from e

    roots = [_resolve_root(entry) for entry in data.get("roots", [])]
    backends = data.get("backends", ["local"]) or ["local"]
    return RouterConfig(roots=roots, backends=backends, source=str(path))


@dataclass
class ElevationState:
    """Result of checking the elevation sentinel: extra roots, and whether
    they're currently active (present, well-formed, and unexpired)."""

    roots: List[Root] = field(default_factory=list)
    expires_at: Optional[float] = None  # epoch seconds
    active: bool = False


def load_elevation() -> ElevationState:
    """Load a time-boxed elevation grant, if one is present and unexpired.

    ``elevate.json`` is written by the OPERATOR, from their own local shell,
    via the ``arma-elevate`` helper script (see ``bin/arma-elevate`` /
    ``bin/arma-revoke``) — never by this router, the MCP server, or any agent
    reachable through it. That's the point: an agent with only
    ``arma_read_file``/``arma_write_file`` cannot grant itself more roots by
    calling a tool; only a human touching the filesystem directly can.

    Re-read fresh on every call (no in-process caching) so a manual revoke
    (``rm ~/.config/armamentarium/elevate.json``) or a natural expiry takes
    effect immediately, without restarting the long-lived router process.

    Returns:
        ElevationState. ``active`` is False (roots empty) if the file is
        missing, malformed, or its ``expires_at`` has passed — expiry is
        checked, not just presence, so a stale grant silently stops working
        rather than needing to be remembered and revoked.
    """
    if not ELEVATION_PATH.exists():
        return ElevationState()
    try:
        data = json.loads(ELEVATION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return ElevationState()
    expires_at = data.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return ElevationState()
    if time.time() >= expires_at:
        return ElevationState(expires_at=expires_at, active=False)
    try:
        roots = [_resolve_root(entry) for entry in data.get("roots", [])]
    except ValueError:
        return ElevationState(expires_at=expires_at, active=False)
    return ElevationState(roots=roots, expires_at=expires_at, active=True)
