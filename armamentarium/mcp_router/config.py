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
import socket
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


def local_host() -> str:
    """This machine's short hostname, lowercased.

    Same normalisation as armamentarium's fleet.normalize_host() and the
    arma-elevate script: grants are keyed on it, so all three must agree. A v2
    grant whose ``host`` is not this host is ignored by this router.
    """
    return socket.gethostname().split(".")[0].lower()


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
    """Result of checking the elevation sentinel.

    ``roots`` are the ACTIVE path grants for this host. ``active`` is True when
    there is at least one. ``expires_at`` is the latest expiry among active path
    grants (or, when none are active, of the expired ones -- kept so the router
    can say "a grant exists but has expired"). ``grants`` carries every active
    grant of any kind as a plain dict, for status reporting; exec grants are
    read here but only consumed once arma_exec exists.
    """

    roots: List[Root] = field(default_factory=list)
    expires_at: Optional[float] = None  # epoch seconds
    active: bool = False
    grants: List[dict] = field(default_factory=list)


def _v1_grants(data: dict) -> List[dict]:
    """Translate a v1 sentinel ({roots, expires_at}) into v2-shaped grants.

    v1 had one expiry for the whole file and no host field (it was always this
    host). Kept readable so an operator mid-upgrade, or an old arma-elevate still
    on PATH somewhere, never silently loses a grant.
    """
    exp = data.get("expires_at")
    if not isinstance(exp, (int, float)):
        return []
    out = []
    for r in data.get("roots", []) or []:
        if isinstance(r, dict) and "path" in r:
            out.append({"kind": "path", "target": r["path"],
                        "access": r.get("access", "r"), "host": local_host(),
                        "expires_at": exp, "v1": True})
    return out


def load_elevation() -> ElevationState:
    """Load time-boxed elevation grants, if any are present and unexpired.

    ``elevate.json`` is written by the OPERATOR, from their own local shell,
    via the ``arma-elevate`` helper script (``bin/arma-grant``; installed
    root-owned to /usr/local/bin) — never by this router, the MCP server, or
    any agent reachable through it. That's the point: an agent with only
    ``arma_read_file``/``arma_write_file`` cannot grant itself more roots by
    calling a tool; only a human touching the filesystem directly can.

    Accepts both formats:
      v1  {"roots": [{"path","access"}], "expires_at": N}
      v2  {"version": 2, "grants": [{"kind": "path"|"exec", "target"|"rule",
           "access", "host", "granted_on", "granted_at", "expires_at"}]}

    Re-read fresh on every call (no in-process caching) so a revoke or a natural
    expiry takes effect immediately. This function NEVER writes the file:
    pruning expired grants into the ledger is arma-elevate's job.

    Fails closed per grant: a malformed entry is skipped, it never widens access.
    Only grants whose ``host`` is this host count.
    """
    if not ELEVATION_PATH.exists():
        return ElevationState()
    try:
        data = json.loads(ELEVATION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return ElevationState()
    if not isinstance(data, dict):
        return ElevationState()

    grants = data.get("grants") if data.get("version") == 2 else _v1_grants(data)
    if not isinstance(grants, list):
        return ElevationState()

    now = time.time()
    me = local_host()
    roots: List[Root] = []
    live: List[dict] = []
    latest_active: Optional[float] = None
    latest_any: Optional[float] = None
    for g in grants:
        if not isinstance(g, dict):
            continue
        exp = g.get("expires_at")
        if not isinstance(exp, (int, float)):
            continue
        if str(g.get("host", "")).lower() != me:
            continue
        kind = g.get("kind")
        if kind == "path":
            latest_any = exp if latest_any is None else max(latest_any, exp)
        if now >= exp:
            continue
        if kind == "path":
            try:
                roots.append(_resolve_root({"path": g.get("target", ""),
                                            "access": g.get("access", "r")}))
            except ValueError:
                continue
            latest_active = exp if latest_active is None else max(latest_active, exp)
            live.append(g)
        elif kind == "exec" and isinstance(g.get("rule"), str):
            live.append(g)

    return ElevationState(
        roots=roots,
        expires_at=latest_active if roots else latest_any,
        active=bool(roots),
        grants=live,
    )
