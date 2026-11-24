#!/usr/bin/env python3
"""armamentarium filesystem router — FastMCP server.

Presents generic filesystem tools over MCP and routes each to whichever backend
is live on the host. Run as a local stdio server::

    ARMAMENTARIUM_ROUTER_CONFIG=~/.config/armamentarium/router.json python -m armamentarium.mcp_router.server

or via the console script ``armamentarium-fs``.

The allowlist in the config file is the security boundary; with no config, every
path is denied until one is supplied (fail-closed). See config.py.

Note on destructive ops over this transport: stdio MCP has no equivalent of a
UI confirmation dialog, so `arma_delete_path` requires the caller to pass
`confirm: true` explicitly rather than defaulting to a silent delete — a text
gate, not a real approval flow. A richer human-in-the-loop confirmation (diff
preview + a blocking approval dialog) is implemented on the Open WebUI Tools
front door instead, where an interactive UI actually exists to ask a person.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

from .config import load_config
from .router import (
    AccessDeniedError,
    FilesystemRouter,
    NoBackendError,
    PathNotAllowedError,
    RouterError,
)
from .backends import DirEntry, StatResult
from .config import load_elevation, local_host
from .. import exec_policy, exec_runner

mcp = FastMCP("armamentarium_fs_mcp")

# Single router instance, built from config at import time. Kept module-level so
# every tool shares one allowlist + backend set.
_router: Optional[FilesystemRouter] = None


def get_router() -> FilesystemRouter:
    global _router
    if _router is None:
        _router = FilesystemRouter(load_config())
    return _router


# -- shared helpers --------------------------------------------------------

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _handle_error(e: Exception) -> str:
    """Map exceptions to actionable, agent-readable error strings."""
    if isinstance(e, PathNotAllowedError):
        return f"Error: {e}"
    if isinstance(e, AccessDeniedError):
        return f"Error: {e}"
    if isinstance(e, NoBackendError):
        return f"Error: {e}"
    if isinstance(e, FileNotFoundError):
        return f"Error: not found: {e}"
    if isinstance(e, FileExistsError):
        return f"Error: {e}"
    if isinstance(e, IsADirectoryError):
        return f"Error: {e}"
    if isinstance(e, NotADirectoryError):
        return f"Error: {e}"
    if isinstance(e, PermissionError):
        return f"Error: OS permission denied: {e}"
    if isinstance(e, RouterError):
        return f"Error: {e}"
    return f"Error: unexpected {type(e).__name__}: {e}"


def _entries_to_json(entries) -> list:
    return [{"name": e.name, "path": e.path, "type": e.type, "size": e.size} for e in entries]


# -- input models ----------------------------------------------------------

class ReadFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="File path to read. '~' and symlinks are resolved; must lie within an allowlisted root (e.g. '~/Datasets/Substrate/FOUNDATION/BEDROCK.md').", min_length=1)
    max_bytes: int = Field(default=200_000, description="Maximum bytes to return; longer files are truncated with a marker.", ge=1, le=5_000_000)


class WriteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    path: str = Field(..., description="File path to write. Must lie within an allowlisted root that grants 'rw' access.", min_length=1)
    content: str = Field(..., description="UTF-8 text to write. Written atomically (temp file + rename).")
    overwrite: bool = Field(default=False, description="If false (default), refuse to replace an existing file. Set true to overwrite.")
    create_dirs: bool = Field(default=False, description="If true, create missing parent directories.")


class DeletePathInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="File or directory path to delete. Must lie within an allowlisted root that grants 'rw' access.", min_length=1)
    recursive: bool = Field(default=False, description="Required to delete a non-empty directory (and everything inside it).")
    confirm: bool = Field(
        ...,
        description=(
            "Must be explicitly set to true. This transport has no interactive confirmation "
            "dialog, so this field is the only safety gate against an accidental delete — "
            "there is no default; the caller must state the intent plainly."
        ),
    )


class ListDirectoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Directory path to list. Must lie within an allowlisted root.", min_length=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' for human-readable or 'json' for machine-readable.")


class StatInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Path to stat (file or directory). Must lie within an allowlisted root.", min_length=1)


class SearchFilesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    root: str = Field(..., description="Directory to search under. Must lie within an allowlisted root.", min_length=1)
    name_glob: Optional[str] = Field(default=None, description="Optional filename glob (e.g. '*.py', 'domain1-*.md'). Matched against the file name only.")
    contains: Optional[str] = Field(default=None, description="Optional literal substring; only files whose text contains it are returned. Binary/unreadable files are skipped.")
    max_results: int = Field(default=100, description="Cap on returned paths.", ge=1, le=2000)
    max_depth: int = Field(default=8, description="Maximum directory depth to descend from root.", ge=1, le=50)


class ExecInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")
    argv: List[str] = Field(..., description="The command as a list of separate arguments, e.g. ['git', '-C', '~/Datasets/Substrate', 'status', '-s']. Never a single shell string; there is no shell, so quoting, pipes, ;, && and $() mean nothing.", min_length=1, max_length=64)
    host: Optional[str] = Field(default=None, description="Host to run on. Omit for this host. (Remote hosts are not available yet.)")


# -- tools -----------------------------------------------------------------

@mcp.tool(
    name="arma_read_file",
    annotations={"title": "Read File", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_read_file(params: ReadFileInput) -> str:
    """Read a UTF-8 text file through the router.

    Resolves the path, enforces the allowlist, and returns file contents from
    the first available backend. Use for reading any text file within an
    allowlisted root (config, source, session metadata JSON, notes).

    Args:
        params (ReadFileInput):
            - path (str): file path (allowlisted; '~'/symlinks resolved)
            - max_bytes (int): truncation cap (default 200000)

    Returns:
        str: file contents (UTF-8, decode errors replaced), truncated with a
        marker if longer than max_bytes; or "Error: <reason>".
    """
    try:
        content, resolved, backend = get_router().read_file(params.path, params.max_bytes)
        return content
    except Exception as e:  # noqa: BLE001 - mapped to actionable strings
        return _handle_error(e)


@mcp.tool(
    name="arma_write_file",
    annotations={"title": "Write File", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
async def arma_write_file(params: WriteFileInput) -> str:
    """Write a UTF-8 text file through the router (atomic temp+rename).

    Enforces the allowlist AND that the matching root grants 'rw'. Refuses to
    clobber an existing file unless overwrite=true, and refuses missing parents
    unless create_dirs=true.

    Args:
        params (WriteFileInput):
            - path (str): file path (must be under an 'rw' root)
            - content (str): UTF-8 text
            - overwrite (bool): replace existing file (default false)
            - create_dirs (bool): create missing parents (default false)

    Returns:
        str: JSON {"written": true, "path": str, "bytes": int, "backend": str}
        or "Error: <reason>".
    """
    try:
        n, resolved, backend = get_router().write_file(
            params.path, params.content, params.overwrite, params.create_dirs
        )
        return json.dumps({"written": True, "path": resolved, "bytes": n, "backend": backend}, indent=2)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_delete_path",
    annotations={"title": "Delete Path", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def arma_delete_path(params: DeletePathInput) -> str:
    """Delete a file or directory through the router.

    Enforces the allowlist AND that the matching root grants 'rw' — identical
    requirement to a write, since a delete is just as destructive. This
    transport (stdio MCP) has no interactive confirmation dialog, so `confirm`
    must be explicitly set true; there is no default that allows a silent
    delete. A non-empty directory additionally requires `recursive=true`.

    Args:
        params (DeletePathInput):
            - path (str): file or directory to delete (must be under an 'rw' root)
            - recursive (bool): required for a non-empty directory (default false)
            - confirm (bool): must be true; the only safety gate on this transport

    Returns:
        str: JSON {"deleted": true, "path": str, "backend": str} or "Error: <reason>".
    """
    if not params.confirm:
        return "Error: confirm must be set to true to delete a path — this is deliberate, not a bug."
    try:
        resolved, backend = get_router().delete_path(params.path, params.recursive)
        return json.dumps({"deleted": True, "path": resolved, "backend": backend}, indent=2)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_list_directory",
    annotations={"title": "List Directory", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_list_directory(params: ListDirectoryInput) -> str:
    """List directory entries (names, types, sizes) through the router.

    Args:
        params (ListDirectoryInput):
            - path (str): directory (allowlisted)
            - response_format ('markdown'|'json')

    Returns:
        str: markdown table or JSON list of
        {"name","path","type"("file"|"dir"|"symlink"|"other"),"size"}; or
        "Error: <reason>".
    """
    try:
        entries, resolved, backend = get_router().list_directory(params.path)
        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"path": resolved, "backend": backend, "count": len(entries), "entries": _entries_to_json(entries)}, indent=2)
        lines = [f"# {resolved}  ({len(entries)} entries, backend: {backend})", ""]
        for e in entries:
            size = f"  {e.size} B" if e.size is not None else ""
            marker = "/" if e.type == "dir" else ("@" if e.type == "symlink" else "")
            lines.append(f"- {e.name}{marker}  [{e.type}]{size}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_stat",
    annotations={"title": "Stat Path", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_stat(params: StatInput) -> str:
    """Return metadata for a path (type, size, mtime, permission bits).

    Args:
        params (StatInput):
            - path (str): file or directory (allowlisted)

    Returns:
        str: JSON {"path","type","size","modified"(epoch s),"mode"(octal),
        "backend"} or "Error: <reason>".
    """
    try:
        st, resolved, backend = get_router().stat(params.path)
        return json.dumps({"path": st.path, "type": st.type, "size": st.size, "modified": st.modified, "mode": st.mode, "backend": backend}, indent=2)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_search_files",
    annotations={"title": "Search Files", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_search_files(params: SearchFilesInput) -> str:
    """Find files under a root by name glob and/or literal content substring.

    Skips heavy dirs (.git, node_modules, __pycache__, venvs) and bounds cost
    with max_results and max_depth — important over NFS. Provide at least one of
    name_glob / contains to avoid listing everything.

    Args:
        params (SearchFilesInput):
            - root (str): search base (allowlisted)
            - name_glob (str|None): filename glob (e.g. '*.md')
            - contains (str|None): literal substring in file text
            - max_results (int): cap (default 100)
            - max_depth (int): depth cap (default 8)

    Returns:
        str: JSON {"root","backend","count","results":[path,...],
        "truncated":bool} or "Error: <reason>".
    """
    try:
        results, resolved, backend = get_router().search_files(
            params.root, params.name_glob, params.contains, params.max_results, params.max_depth
        )
        return json.dumps({"root": resolved, "backend": backend, "count": len(results), "results": results, "truncated": len(results) >= params.max_results}, indent=2)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_exec",
    annotations={"title": "Run Allowed Command", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
)
async def arma_exec(params: ExecInput) -> str:
    """Run one command, only if the operator's exec policy allows it.

    The policy (~/.config/armamentarium/exec.json, operator-owned) lists every
    allowed command shape. Anything not listed is refused with the reason, the
    nearest rule, and -- for rules that need approval -- the exact
    `arma-elevate exec <rule>` command the operator can run. Call
    arma_exec_rules first to see what is allowed. Do not retry a refused
    command in a different form to get around a rule; ask the operator.

    Args:
        params (ExecInput):
            - argv (list[str]): program and arguments, one list element each
            - host (str|None): omit for this host

    Returns:
        str: JSON {"allowed", "reason", "rule", "exit_code", "stdout", "stderr",
        "truncated", "timed_out", "duration_ms", "ran", "hint"?}. allowed=true
        with a nonzero exit_code means the command ran and failed; read stderr.
    """
    try:
        result = await asyncio.to_thread(exec_runner.execute, params.argv, params.host)
        return result.to_json()
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_exec_rules",
    annotations={"title": "List Allowed Commands", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_exec_rules() -> str:
    """List the exec policy: every allowed command shape, and active exec grants.

    Each rule shows its argv prefix (literals, a|b choices, {type} slots,
    {path:ROOT} slots), allowed options, positional args, whether it runs
    always or needs an operator grant, and which hosts it applies to.

    Returns:
        str: JSON {"policy", "host", "roots", "rules": [...], "active_exec_grants": [...]}
        or "Error: <reason>" if the policy is missing or malformed (then nothing can run).
    """
    try:
        pol = exec_policy.load_policy()
        now = time.time()
        grants = [{"rule": g["rule"], "expires_in_seconds": int(g["expires_at"] - now)}
                  for g in load_elevation().grants if g.get("kind") == "exec"]
        return json.dumps({
            "policy": pol.source, "host": local_host(),
            "roots": {n: {"patterns": r.patterns, "resolve": r.resolve} for n, r in pol.roots.items()},
            "rules": [{"name": r.name, "mode": r.mode, "argv": r.argv, "options": r.options,
                       "args": r.args, "hosts": r.hosts, "sudo": r.sudo,
                       "description": r.description} for r in pol.rules.values()],
            "active_exec_grants": grants,
        }, indent=2)
    except exec_policy.PolicyError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="arma_router_status",
    annotations={"title": "Router Status", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def arma_router_status() -> str:
    """Report the loaded config: source, allowlisted roots, and backends.

    Returns:
        str: JSON {"config_source","roots":[{path,resolved,access}],
        "backends":[{name,available}]}. If roots is empty, no path can be
        served until a config is supplied (see config.py).
    """
    try:
        return json.dumps(get_router().status(), indent=2)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
