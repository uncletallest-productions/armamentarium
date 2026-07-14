"""armamentarium.mcp_router — a backend-routing MCP filesystem server.

Presents generic filesystem tools (read_file / write_file / list_directory /
search_files / stat) over MCP and routes each call to whichever backend can
currently serve the requested path. The first (and always-available) backend is
the local host filesystem; the router is structured so remote backends (Argus
SSE, a Cowork mount bridge, etc.) can be slotted in later behind the same tools.

Why it exists: it works when Anthropic's first-party Filesystem MCP does not
(regression #584), and — because a host-side MCP process is not subject to
Cowork's folder-mount guard — it can reach paths Cowork refuses to mount, such
as the ``~/.config/Claude/local-agent-mode-sessions`` session tree that
armamentarium's session-roaming feature needs.
"""

from .config import Root, RouterConfig, load_config
from .backends import Backend, LocalFilesystemBackend
from .router import FilesystemRouter, RouterError, AccessDeniedError, PathNotAllowedError

__all__ = [
    "Root",
    "RouterConfig",
    "load_config",
    "Backend",
    "LocalFilesystemBackend",
    "FilesystemRouter",
    "RouterError",
    "AccessDeniedError",
    "PathNotAllowedError",
]
