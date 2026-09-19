# armamentarium

Portable Claude Desktop sessions across machines, plus an MCP filesystem router
that works when Anthropic's first-party Filesystem MCP does not.

An *armamentarium* is the complete kit of tools and resources one brings to bear
on a task. This tool carries a Claude Desktop session from one machine to another
over shared NFS, and marshals whatever filesystem/MCP access is available on the
host it lands on into one working kit.

Two capabilities, one tool:

1. **Session roaming** — dock Claude Desktop *chat* sessions onto a machine,
   rewriting the mount-root paths (and, for fresh installs, the deviceUUID) embedded
   in session metadata. *(Shipped — `armamentarium dock`; see "Session roaming" below.
   Runs host-side. Scheduled tasks are out of scope — separate mechanism.)*
2. **MCP filesystem router** — a backend-routing MCP server presenting generic
   filesystem tools (`read_file` / `write_file` / `list_directory` /
   `search_files` / `stat`) and forwarding each to whichever backend is live on
   the current host. *(Shipped first — this is the `armamentarium.mcp_router`
   package.)*

Also shipped, below decks (see their own sections): `armamentarium fleet` (machine
presence/registry), `armamentarium config` (fleet dotfile capture/diff/deploy), and
the first stub of `armamentarium packages` (package-state sync — currently just the
`apt sync-cache` action).

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

## Session roaming (`armamentarium dock`)

Dock a Claude Desktop **chat** session onto the current machine. Docking is:

> sync the `local_<uuid>/` dir + its `local_<uuid>.json` sidecar into the local device
> tree, then rewrite the stale **mount-root** paths in the sidecar (and, only if the
> source session's **deviceUUID** differs from this machine's, rewrite that too and
> rename the `.claude/projects/<slug>` dir).

On a fleet where `~/.config/Claude` was cloned across machines the deviceUUID already
matches, so docking reduces to the mount-root rewrite. Scheduled tasks are **out of
scope** (they live in `scheduled-tasks.json` with a server-side id — a separate problem).

Configure the mount-root map once:

```bash
mkdir -p ~/.config/armamentarium
cp config/dock.example.json ~/.config/armamentarium/dock.json   # edit for your migrations
```

List what's dockable (from `source_dirs` in dock.json, or a given dir):

```bash
armamentarium list                     # source_dirs from dock.json
armamentarium list <dir> --json        # scan a dir, machine-readable
```

Dry-run by default (prints a plan, writes nothing):

```bash
armamentarium dock ~/Datasets/Substrate/dom0/sisyphus-archive/sessions/local_<uuid>
```

Apply it (quit Claude Desktop first so it re-scans on next launch):

```bash
armamentarium dock <session-dir> --apply
# extra one-off mapping, e.g. the old Scriptorium name:
armamentarium dock <session-dir> --apply --path-map /home/youruser/Scriptorium=/home/youruser/Datasets/Scriptorum
```

It refuses to overwrite an existing session id unless `--force`, warns if a rewritten
folder still doesn't exist on this host, and prints the exact `rm` to undo the dock.

### One-shot with restart

Claude Desktop only re-scans the session tree at **startup** — a running instance won't
show a freshly docked session until it's restarted. `--apply --restart` makes dock atomic:
**stop Desktop → apply → start Desktop** (so files are never written under a live app):

```bash
cp config/desktop.example.json ~/.config/armamentarium/desktop.json   # once; edit for your install
armamentarium dock <session-dir> --apply --restart
```

Stop is a graceful `SIGTERM` with a timeout; add `--force` to `SIGKILL` if it won't exit.

## Roaming between machines (`undock` + checkout ledger)

To move a session to another machine *properly* — without the corruption risk of having
it open in two places — armamentarium coordinates through a **shared checkout ledger on
Substrate** (reachable by every node over NFS, LAN or tailnet; not mDNS, which is
link-local). Configure `roaming_store` in dock.json (defaults to
`~/Datasets/Substrate/dom0/session-roaming`).

The loop:

```bash
# On machine A, hand the session off:
armamentarium undock <local-session-dir> --remove-local   # flush to store, release, clear local

# On machine B, take it:
armamentarium dock <store>/<account>/store/local_<uuid> --apply --restart
```

`dock --apply` **acquires** a checkout and refuses if another machine holds the session
live (`--force` to override — only if you're sure it isn't in use). `undock` **releases**
it. Inspect or hand-edit the ledger:

```bash
armamentarium roaming status                 # who holds what, where
armamentarium roaming checkin  local_<uuid>  # force-release (e.g. a crashed machine's hold)
armamentarium roaming checkout local_<uuid>  # manually claim
```

The ledger is best-effort in `dock`: if the store isn't present, it's skipped so `dock`
still works standalone. On a cloned-config fleet (shared deviceUUID + identical mount
paths) undock↔dock is mostly a file sync — the ledger is the only added machinery.

## Claude Desktop control (`armamentarium desktop`)

Optional, config-driven, and thin — armamentarium's core needs none of it. Configure
`~/.config/armamentarium/desktop.json` (launch `command`, extra `env`, `process_match`,
`term_timeout_s`), then:

```bash
armamentarium desktop status     # running? which pids?
armamentarium desktop start      # launch (with configured env, e.g. CLAUDE_USE_WAYLAND=1)
armamentarium desktop stop       # graceful SIGTERM (add --force for SIGKILL on timeout)
armamentarium desktop restart    # stop then start
```

`process_match` matches the process **executable (argv[0])**, not arbitrary arguments, so a
shell or editor that merely mentions the name is never targeted. Run `desktop status` once
to confirm your install is detected before relying on `stop`/`restart`.

## Package sync (`armamentarium packages`)

First stub (2026-09-18) of a below-decks package-state provider/consumer, scoped
across apt/Nala, Flatpak, Snap, and AppImage/GearLever. Only one action is
implemented so far — everything else is tracked, unimplemented, in
`armamentarium/packages.py`'s module docstring.

`packages apt sync-cache` automates what used to be a manual two-step: copy new
`.deb`s out of this machine's apt cache into the fleet's flat-file offline repo,
then trigger that host's reindex script.

```bash
mkdir -p ~/.config/armamentarium
cp config/packages.example.json ~/.config/armamentarium/packages.json   # edit host/paths
armamentarium packages apt sync-cache            # rsync --ignore-existing, then remote reindex
armamentarium packages apt sync-cache --dry-run  # show what would sync, touch nothing remote
```

`--ignore-existing` makes it safe to run repeatedly (only new `.deb`s copy) or on a
timer. The remote reindex trigger is a plain `ssh <host> <remote_reindex_cmd>` —
passwordless key auth (`ssh_identity` in packages.json) and, if the reindex command
itself needs `sudo`, a NOPASSWD entry for that one command on the remote host are
both on you to have set up; this module doesn't manage either.

## Layout

```
armamentarium/
  armamentarium/
    __init__.py
    cli.py           # `armamentarium dock/undock/roaming/desktop/list/router/fleet/config/packages ...` entry
    dock.py          # session dock: discover local device, plan, rewrite, apply; session scan
    roaming.py       # checkout ledger + undock (cross-machine coordination on Substrate)
    desktop.py       # optional config-driven Claude Desktop process control
    fleet.py         # machine fleet registry (presence, master designation)
    config_sync.py   # fleet dotfile capture/diff/deploy/status (Profectionis/arma-fleet)
    packages.py      # below-decks package-state provider/consumer (apt/Flatpak/Snap/AppImage)
    mcp_router/
      __init__.py
      config.py      # allowlist + config loading (security boundary)
      backends.py    # Backend interface + LocalFilesystemBackend
      router.py      # path safety, allowlist enforcement, backend selection
      server.py      # FastMCP tool surface + entry point
  config/
    router.example.json
    dock.example.json
    desktop.example.json
    packages.example.json
  pyproject.toml
```

## Status

Prototype (v0.1.0). Router implemented and self-tested. Chat-session roaming
(`armamentarium dock`) implemented and verified end-to-end: a real archived session
docked onto another machine opens in Claude Desktop with full history and its folder
reconnected. Runs host-side (the session tree it targets can't be reached from inside a
sandboxed client — the gap the router closes). Cross-machine roaming — `undock` +
checkout ledger — implemented and self-tested (checkout/hold-refusal/release/flush).
`packages apt sync-cache` is new (2026-09-18) and not yet run against the real fleet —
verify against haphaestus before relying on it. Remaining: scheduled-task portability
(separate mechanism), Avahi presence + auto stale-lock detection, remote router
backends, the rest of the `packages` surface (apt manifest capture/restore, Flatpak,
Snap, AppImage/GearLever).

## License

MIT.
