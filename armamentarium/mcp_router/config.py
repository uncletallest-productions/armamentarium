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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

DEFAULT_CONFIG_PATH = Path("~/.config/armamentarium/router.json").expanduser()

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
