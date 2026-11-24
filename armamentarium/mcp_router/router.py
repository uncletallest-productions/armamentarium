"""The router: path safety, allowlist enforcement, and backend selection.

All security lives here. Every filesystem operation goes through
:meth:`FilesystemRouter.resolve`, which:

1. Expands ``~`` and resolves symlinks to a canonical absolute path
   (so ``../`` and symlink escapes can't sneak outside an allowed root).
2. Confirms the resolved path lies within an allowlisted root.
3. For writes (and deletes — a delete is a write against the parent's
   contents), confirms the matching root grants ``rw``.

then hands the canonical path to the first available backend that can serve it.
Backends never see un-resolved or un-checked paths.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .backends import Backend, DirEntry, LocalFilesystemBackend, StatResult
from .config import Root, RouterConfig, load_elevation


class RouterError(Exception):
    """Base class for router-level failures (as opposed to backend I/O errors)."""


class PathNotAllowedError(RouterError):
    """Raised when a path resolves outside every allowlisted root."""


class AccessDeniedError(RouterError):
    """Raised when a write (or delete) targets a read-only root."""


class NoBackendError(RouterError):
    """Raised when no available backend can serve a path."""


# Paths an agent may NEVER write or delete, whatever the root's access or any grant says.
# git runs programs named by files here (hooks, core.fsmonitor/sshCommand in .git/config, a
# `.git` FILE pointing gitdir: elsewhere), so writing them turns any allowed git command into
# arbitrary code execution. The operator edits these by hand. Decided 2026-11-23 (Jerry: "both").
PROTECTED_DIR_NAMES = {".git", ".githooks"}
PROTECTED_DIR_PAIRS = {("tests", "hooks")}     # stoa's core.hooksPath convention


def protected_write_reason(resolved: str) -> Optional[str]:
    """Why ``resolved`` must never be agent-written, or None if it may be."""
    parts = [p for p in resolved.split(os.sep) if p]
    for i, part in enumerate(parts):
        if part in PROTECTED_DIR_NAMES:
            return f"{part!r} (git would execute what is written there)"
        if i + 1 < len(parts) and (part, parts[i + 1]) in PROTECTED_DIR_PAIRS:
            return f"'{part}/{parts[i + 1]}/' (a git hooks directory)"
    return None


# Registry of backend constructors, keyed by the name used in config "backends".
_BACKEND_FACTORIES = {
    "local": LocalFilesystemBackend,
}


class FilesystemRouter:
    def __init__(self, config: RouterConfig):
        self.config = config
        self.backends: List[Backend] = []
        for name in config.backends:
            factory = _BACKEND_FACTORIES.get(name)
            if factory is None:
                # Forward-looking names (e.g. "argus") are tolerated but skipped
                # until wired, so a shared config doesn't break the prototype.
                continue
            self.backends.append(factory())
        if not self.backends:
            # Always guarantee the local backend so the router is never inert
            # on a host where it can obviously reach the filesystem.
            self.backends.append(LocalFilesystemBackend())

    # -- path safety -------------------------------------------------------

    def _match_root(self, resolved: str, roots: Optional[List[Root]] = None) -> Optional[Root]:
        """Return the most specific (longest-path) matching root, not the
        first one listed — so entry order in router.json/elevate.json can
        never silently shadow a more specific rw grant with a broader r one."""
        best: Optional[Root] = None
        for root in roots if roots is not None else self.config.roots:
            if resolved == root.path or resolved.startswith(root.path + os.sep):
                if best is None or len(root.path) > len(best.path):
                    best = root
        return best

    def resolve(self, path: str, *, for_write: bool = False) -> Tuple[str, Root]:
        """Resolve ``path`` to a canonical form and enforce the allowlist.

        Base config first, then time-boxed elevation grants (``elevate.json``),
        consulted in exactly two cases:

        * the path matches NO base root  -> elevation can ADD a root;
        * a WRITE matches a read-only base root -> an active rw elevation that
          also matches can UPGRADE it for the duration of the grant.

        FIXED 2026-11-23: elevation used to be consulted only in the first case,
        so "r in router.json, arma-elevate rw for short bursts" could never work --
        the base r root always matched first and the rw grant was never read, while
        the refusal message told the caller to go and get exactly that grant.

        Elevation never DOWNGRADES: a read, or a write to an already-rw base root,
        never consults it. Elevation is read fresh on every call, never cached, so
        a revoke or a natural expiry takes effect on the very next operation.

        Returns ``(resolved_absolute_path, matched_root)``.

        Raises:
            PathNotAllowedError: path is outside every allowlisted root,
                including any active elevation.
            AccessDeniedError: write (or delete) requested against a
                read-only root with no active rw elevation covering it.
        """
        resolved = os.path.realpath(Path(path).expanduser())
        root = self._match_root(resolved)
        elevation = None
        if root is None or (for_write and not root.writable):
            elevation = load_elevation()
            if elevation.active:
                elevated = self._match_root(resolved, roots=elevation.roots)
                if root is None:
                    root = elevated
                elif elevated is not None and elevated.writable:
                    root = elevated          # r -> rw upgrade, only for this write
        if root is None:
            allowed = ", ".join(r.raw for r in self.config.roots) or "(none configured)"
            extra = ""
            if elevation is not None and not elevation.active and elevation.expires_at:
                extra = " An elevation grant exists but has expired; re-run arma-elevate if this access is still needed."
            raise PathNotAllowedError(
                f"path {path!r} resolves to {resolved!r}, which is outside all "
                f"allowlisted roots. Allowed roots: {allowed}.{extra}"
            )
        if for_write:
            why = protected_write_reason(resolved)
            if why:
                raise AccessDeniedError(
                    f"path {path!r} is inside {why}. Agents may never write or delete there, "
                    f"with or without a grant; the operator edits these files by hand."
                )
        if for_write and not root.writable:
            raise AccessDeniedError(
                f"path {path!r} is under read-only root {root.raw!r}; writes/deletes are denied. "
                f"Ask the operator for a time-boxed grant: arma-elevate path {root.raw} rw <ttl>"
            )
        return resolved, root

    def _pick_backend(self, resolved: str) -> Backend:
        for backend in self.backends:
            if backend.available() and backend.can_serve(resolved):
                return backend
        raise NoBackendError(
            f"no available backend can serve {resolved!r} "
            f"(tried: {', '.join(b.name for b in self.backends)})"
        )

    # -- operations --------------------------------------------------------

    def read_file(self, path: str, max_bytes: int) -> Tuple[str, str, str]:
        resolved, root = self.resolve(path)
        backend = self._pick_backend(resolved)
        return backend.read_file(resolved, max_bytes), resolved, backend.name

    def write_file(
        self, path: str, content: str, overwrite: bool, create_dirs: bool,
    ) -> Tuple[int, str, str]:
        resolved, root = self.resolve(path, for_write=True)
        backend = self._pick_backend(resolved)
        return backend.write_file(resolved, content, overwrite, create_dirs), resolved, backend.name

    def delete_path(self, path: str, recursive: bool = False) -> Tuple[str, str]:
        """Delete a file or directory. Requires an 'rw' root, same as write —
        a delete is destructive in the same way a write is, just aimed at the
        whole entry instead of its contents."""
        resolved, root = self.resolve(path, for_write=True)
        backend = self._pick_backend(resolved)
        backend.delete(resolved, recursive)
        return resolved, backend.name

    def list_directory(self, path: str) -> Tuple[List[DirEntry], str, str]:
        resolved, root = self.resolve(path)
        backend = self._pick_backend(resolved)
        return backend.list_directory(resolved), resolved, backend.name

    def stat(self, path: str) -> Tuple[StatResult, str, str]:
        resolved, root = self.resolve(path)
        backend = self._pick_backend(resolved)
        return backend.stat(resolved), resolved, backend.name

    def search_files(
        self, root_path: str, name_glob: Optional[str], contains: Optional[str],
        max_results: int, max_depth: int,
    ) -> Tuple[List[str], str, str]:
        resolved, root = self.resolve(root_path)
        backend = self._pick_backend(resolved)
        return (
            backend.search_files(resolved, name_glob, contains, max_results, max_depth),
            resolved,
            backend.name,
        )

    # -- diagnostics -------------------------------------------------------

    def status(self) -> dict:
        elevation = load_elevation()
        now = time.time()
        return {
            "config_source": self.config.source,
            "roots": [{"path": r.raw, "resolved": r.path, "access": r.access} for r in self.config.roots],
            "backends": [{"name": b.name, "available": b.available()} for b in self.backends],
            "elevation": {
                "active": elevation.active,
                "expires_at": elevation.expires_at,
                "expires_in_seconds": (
                    round(elevation.expires_at - now) if elevation.active else None
                ),
                "roots": [
                    {"path": r.raw, "resolved": r.path, "access": r.access} for r in elevation.roots
                ],
                # Every active grant (path AND exec) with its own time left -- v2 can
                # hold several, each expiring on its own clock.
                "grants": [
                    {k: g.get(k) for k in ("kind", "target", "rule", "access", "host",
                                           "granted_on", "expires_at")
                     if g.get(k) is not None}
                    | {"expires_in_seconds": round(g["expires_at"] - now)}
                    for g in elevation.grants
                ],
            },
        }
