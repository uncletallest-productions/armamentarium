# armamentarium

Portable Claude Desktop sessions across machines, plus an MCP filesystem router
that works when Anthropic's first-party Filesystem MCP does not.

An *armamentarium* is the complete kit of tools and resources one brings to bear
on a task. This tool carries a Claude Desktop session from one machine to another
over shared NFS, and marshals whatever filesystem/MCP access is available on the
host it lands on into one working kit.

Two capabilities, one tool:

1. **Session roaming** — dock/undock Claude Desktop sessions between machines
   (via shared NFS), rewriting the device-bound paths embedded in session
   metadata. *(Design stage — see `docs`/the Substrate block; must run host-side,
   see note below.)*
2. **MCP filesystem router** — a backend-routing MCP server presenting generic
   filesystem tools (`read_file` / `write_file` / `list_directory` /
   `search_files` / `stat`) and forwarding each to whichever backend is live on
   the current host. *(Shipped first — this is the `armamentarium.mcp_router`
   package.)*

## Why the router exists

- **It works when the first-party Filesystem MCP doesn't.** During the July 2026
  Filesystem-MCP regression the built-in connector stopped surfacing tools; a
  plain host-side server sidesteps that entirely.
- **It reaches paths a sandboxed client can't mount.** A host-side MCP process is
  not subject to Cowork's folder-mount guard, so it can serve directories the
  client refuses to mount — including the
  `~/.config/Claude/local-agent-mode-sessions` session tree that the roaming
  feature has to read and rewrite. That's what makes the router the access path
  for session roaming, not just a workaround.
- **Fallback chain by design.** Today only the local-filesystem backend is wired.
  The `Backend` interface lets remote backends (an SSE bridge, a Cowork host-tool
  bridge) slot in behind the same tools, so a session's abstract filesystem needs
  resolve to whatever is actually available on each host.

## Install

**Recommended: install into a venv.** On Debian/Ubuntu (and anything following
PEP 668, "externally-managed-environment"), installing into the system Python
with `pip --break-system-packages` drops these dependencies into the same
site-packages `apt` manages — which can break apt-managed Python tools like
`nala` that pin their own versions of shared libs (e.g. `anyio`). A venv keeps
armamentarium's dependency tree fully isolated, so it never fights the system:

```bash
python3 -m venv ~/.venvs/armamentarium
~/.venvs/armamentarium/bin/pip install -e /path/to/armamentarium
```

Then invoke the server by its venv-absolute path (see Run), which also avoids
any `~/.local/bin`-not-on-PATH surprises.

<details>
<summary>System install (not recommended)</summary>

```bash
pip install -e .        # from this directory; on PEP 668 systems needs --break-system-packages
```

If this leaves an apt tool complaining about `anyio` (e.g. `nala` wanting
`>=4.9`), restore it with `pip install --break-system-packages -U 'anyio>=4.9,<5'`
— mcp only requires `anyio>=4.5`, so that satisfies both. The venv route avoids
the whole problem.
</details>

## Configure (the security boundary)

The router serves **nothing** until a config grants roots — fail-closed. Copy the
example and edit:

```bash
mkdir -p ~/.config/armamentarium
cp config/router.example.json ~/.config/armamentarium/router.json
$EDITOR ~/.config/armamentarium/router.json
```

Each root has an `access` of `"r"` (read-only) or `"rw"` (read + write). Paths
resolve `~` and symlinks *before* the allowlist check, so `../` and symlink
escapes can't reach outside a root. Writes are refused against read-only roots.

Config is found via, in order: `$ARMAMENTARIUM_ROUTER_CONFIG`, then
`~/.config/armamentarium/router.json`.

## Run

```bash
# venv install (recommended) — absolute path, no PATH setup needed:
~/.venvs/armamentarium/bin/armamentarium-fs

# or, from an activated venv / system install:
armamentarium-fs
# or:
python -m armamentarium.mcp_router.server
```

It's a local **stdio** MCP server. Point your MCP client's server config at that
command (for a venv install, use the venv-absolute path above). `arma_router_status`
reports the loaded roots and backends.

Example MCP client server entry:

```json
{
  "mcpServers": {
    "armamentarium": {
      "command": "/home/youruser/.venvs/armamentarium/bin/armamentarium-fs",
      "env": { "ARMAMENTARIUM_ROUTER_CONFIG": "/home/youruser/.config/armamentarium/router.json" }
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `arma_read_file` | Read a UTF-8 text file (with truncation cap) |
| `arma_write_file` | Atomic write; `overwrite`/`create_dirs` gated off by default |
| `arma_list_directory` | List entries with type + size |
| `arma_stat` | Type, size, mtime, permission bits |
| `arma_search_files` | Find by name glob and/or content substring, depth/result-bounded |
| `arma_router_status` | Loaded config, roots, and backend availability |

## Layout

```
armamentarium/
  armamentarium/
    __init__.py
    mcp_router/
      __init__.py
      config.py      # allowlist + config loading (security boundary)
      backends.py    # Backend interface + LocalFilesystemBackend
      router.py      # path safety, allowlist enforcement, backend selection
      server.py      # FastMCP tool surface + entry point
  config/
    router.example.json
  pyproject.toml
```

## Status

Prototype (v0.1.0). Router implemented and self-tested; session-roaming is design
stage. The roaming half must run **host-side** (a script operating directly on the
real filesystem) because the session tree it targets can't be reached from inside
a sandboxed client session — which is precisely the gap this router closes.

## License

MIT.
