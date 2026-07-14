"""The router: path safety, allowlist enforcement, and backend selection.

All security lives here. Every filesystem operation goes through
:meth:`FilesystemRouter.resolve`, which:

1. Expands ``~`` and resolves symlinks to a canonical absolute path
   (so ``../`` and symlink escapes can't sneak outside an allowed root).
2. Confirms the resolved path lies within an allowlisted root.
3. For writes, confirms the matching root grants ``rw``.

then hands the canonical path to the first available backend that can serve it.
Backends never see un-resolved or un-checked paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

from .backends import Backend, DirEntry, LocalFilesystemBackend, StatResult
from .config import Root, RouterConfig


class RouterError(Exception):
    """Base class for router-level failures (as opposed to backend I/O errors)."""


class PathNotAllowedError(RouterError):
    """Raised when a path resolves outside every allowlisted root."""


class AccessDeniedError(RouterError):
    """Raised when a write targets a read-only root."""


class NoBackendError(RouterError):
    """Raised when no available backend can serve a path."""


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

    def _match_root(self, resolved: str) -> Optional[Root]:
        for root in self.config.roots:
            if resolved == root.path or resolved.startswith(root.path + os.sep):
                return root
        return None

    def resolve(self, path: str, *, for_write: bool = False) -> Tuple[str, Root]:
        """Resolve ``path`` to a canonical form and enforce the allowlist.

        Returns ``(resolved_absolute_path, matched_root)``.

        Raises:
            PathNotAllowedError: path is outside every allowlisted root.
            AccessDeniedError: write requested against a read-only root.
        """
        resolved = os.path.realpath(Path(path).expanduser())
        root = self._match_root(resolved)
        if root is None:
            allowed = ", ".join(r.raw for r in self.config.roots) or "(none configured)"
            raise PathNotAllowedError(
                f"path {path!r} resolves to {resolved!r}, which is outside all "
                f"allowlisted roots. Allowed roots: {allowed}."
            )
        if for_write and not root.writable:
            raise AccessDeniedError(
                f"path {path!r} is under read-only root {root.raw!r}; writes are denied. "
                f"Grant this root \"access\": \"rw\" in the router config to allow writing."
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
        return {
            "config_source": self.config.source,
            "roots": [{"path": r.raw, "resolved": r.path, "access": r.access} for r in self.config.roots],
            "backends": [{"name": b.name, "available": b.available()} for b in self.backends],
        }
