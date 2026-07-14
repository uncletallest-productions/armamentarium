"""Storage backends the router can dispatch to.

A :class:`Backend` is anything that can satisfy a filesystem operation on a
resolved absolute path. The router owns the allowlist and path safety; a backend
only has to (a) say whether it can currently serve a given path, and (b) perform
the primitive operations. This keeps the security boundary in one place (the
router) and lets backends stay dumb.

Only :class:`LocalFilesystemBackend` is wired in this prototype. Future backends
(Argus SSE, a Cowork host-tool bridge) implement the same interface so the MCP
tool surface never changes.
"""

from __future__ import annotations

import abc
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class DirEntry:
    name: str
    path: str
    type: str          # "file" | "dir" | "symlink" | "other"
    size: Optional[int]  # bytes, None for non-regular


@dataclass
class StatResult:
    path: str
    type: str
    size: int
    modified: float    # epoch seconds
    mode: str          # octal permission string, e.g. "0644"


class Backend(abc.ABC):
    """Abstract storage backend operating on already-resolved absolute paths.

    The router guarantees that every ``path`` passed here has already been
    resolved and checked against the allowlist. Backends must NOT re-interpret
    ``~`` or relative paths.
    """

    name: str = "backend"

    @abc.abstractmethod
    def available(self) -> bool:
        """True if this backend is usable right now (e.g. mount is live)."""

    @abc.abstractmethod
    def can_serve(self, path: str) -> bool:
        """True if this backend can serve operations for ``path``."""

    @abc.abstractmethod
    def read_file(self, path: str, max_bytes: int) -> str: ...

    @abc.abstractmethod
    def write_file(self, path: str, content: str, overwrite: bool, create_dirs: bool) -> int: ...

    @abc.abstractmethod
    def list_directory(self, path: str) -> List[DirEntry]: ...

    @abc.abstractmethod
    def stat(self, path: str) -> StatResult: ...

    @abc.abstractmethod
    def search_files(
        self, root: str, name_glob: Optional[str], contains: Optional[str],
        max_results: int, max_depth: int,
    ) -> List[str]: ...


def _entry_type(p: Path) -> str:
    if p.is_symlink():
        return "symlink"
    if p.is_dir():
        return "dir"
    if p.is_file():
        return "file"
    return "other"


# Directories that are almost never worth walking and blow up search cost.
_SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"}


class LocalFilesystemBackend(Backend):
    """Direct host filesystem access via the standard library.

    Always available and can serve any path (the router's allowlist is what
    actually constrains it). This is the backend that makes the router useful
    the moment it runs on a host — no Filesystem MCP, no Cowork mount required.
    """

    name = "local"

    def available(self) -> bool:
        return True

    def can_serve(self, path: str) -> bool:
        return True

    def read_file(self, path: str, max_bytes: int) -> str:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"no such file: {path}")
        if p.is_dir():
            raise IsADirectoryError(f"path is a directory, use list_directory: {path}")
        data = p.read_bytes()[: max_bytes + 1]
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[...truncated at {max_bytes} bytes...]"
        return text

    def write_file(self, path: str, content: str, overwrite: bool, create_dirs: bool) -> int:
        p = Path(path)
        if p.exists() and not overwrite:
            raise FileExistsError(
                f"file exists and overwrite=False: {path}. Pass overwrite=true to replace it."
            )
        if p.exists() and p.is_dir():
            raise IsADirectoryError(f"path is a directory: {path}")
        if create_dirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        elif not p.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {p.parent}. Pass create_dirs=true to create it."
            )
        # Atomic write: temp in same dir + rename, so a crash never leaves a
        # half-written file (matches the atomic-JSON pattern used elsewhere).
        tmp = p.with_name(f".{p.name}.arma-tmp")
        encoded = content.encode("utf-8")
        tmp.write_bytes(encoded)
        os.replace(tmp, p)
        return len(encoded)

    def list_directory(self, path: str) -> List[DirEntry]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"no such directory: {path}")
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {path}")
        entries: List[DirEntry] = []
        for child in sorted(p.iterdir(), key=lambda c: c.name):
            t = _entry_type(child)
            try:
                size = child.stat().st_size if t == "file" else None
            except OSError:
                size = None
            entries.append(DirEntry(name=child.name, path=str(child), type=t, size=size))
        return entries

    def stat(self, path: str) -> StatResult:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"no such path: {path}")
        st = p.stat()
        return StatResult(
            path=str(p),
            type=_entry_type(p),
            size=st.st_size,
            modified=st.st_mtime,
            mode=oct(st.st_mode & 0o777)[2:].zfill(4),
        )

    def search_files(
        self, root: str, name_glob: Optional[str], contains: Optional[str],
        max_results: int, max_depth: int,
    ) -> List[str]:
        base = Path(root)
        if not base.exists():
            raise FileNotFoundError(f"no such directory: {root}")
        if not base.is_dir():
            raise NotADirectoryError(f"search root is not a directory: {root}")
        base_depth = len(base.parts)
        results: List[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS]
            for fn in filenames:
                if name_glob and not fnmatch.fnmatch(fn, name_glob):
                    continue
                full = os.path.join(dirpath, fn)
                if contains:
                    try:
                        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                            if contains not in fh.read():
                                continue
                    except OSError:
                        continue
                results.append(full)
                if len(results) >= max_results:
                    return results
        return results
