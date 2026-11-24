# armamentarium

**Give an AI agent real access to your machines, on your terms.**

armamentarium is a host-side MCP server and CLI for people who run Claude (or any MCP
client) against their own Linux machines. It provides:

- **A filesystem router** that serves only the directories you allowlist, read-only or
  read-write, with `..` and symlink escapes blocked.
- **`arma_exec`**, a command runner that executes only the command *shapes* you've
  written down. Anything that changes things waits for a time-boxed grant that only you
  can give.
- **Fleet config sync** that captures and restores dotfiles across machines, without
  trusting the shared store it syncs through.
- **Session roaming** that moves Claude Desktop chat sessions between machines.

An *armamentarium* is the complete kit one brings to a task. This one is built around a
simple rule: **the agent can ask for anything, but only the operator can grant it, and
every grant ends.**

> Status: v0.1, in daily use on a small Debian fleet. Linux only. Single operator.
> See [Status](#status) for what is and isn't done.

### The setup it grew up in

armamentarium was built for one homelab, and most of it assumes that lab's storage
shape, so it helps to know it:

- **One ZFS pool on a home server holds everything that matters,** split into datasets
  by purpose (code, agent memory, fleet config, archives, media). Each dataset is
  exported over NFS.
- **Every machine mounts the datasets it uses as leaves under `~/Datasets/<Name>`.**
  Mounts go over the LAN first, with the tailnet as a fallback. So the same path
  works on every machine, wherever the machine is.
- **Laptops carry local ZFS copies of a curated subset** for travel. They work
  offline against those, and the changes are replayed back to the server on return.
- **Shared state lives in those datasets.** The fleet-config store
  (`~/Datasets/Profectionis/fleet-config`, a git repo), the session-roaming ledger, and
  the machine registry are all on NFS. That's why the defaults below point at
  `~/Datasets/...`, and why so much of this tool is about trusting (or not trusting)
  shared storage. Every default can be overridden. Point them wherever your shared
  storage lives, or at a plain local directory.

The Latin names (*Profectionis*, *Horreum*, *Viaticus*…) follow a naming scheme
explained in [docs/NAMING.md](docs/NAMING.md). You don't need any of it to use the tool.

armamentarium is also the engine behind two hubs in **Stoa**, the author's labwc/Wayland
desktop shell: **Oraculum** (Claude Desktop sessions: docking, roaming, start/stop)
and **Aerarium** (the dataset working set: mount state and offline mode), whose mount
registry travels between machines through `arma config`. Stoa isn't required.
armamentarium is a standalone CLI and MCP server.

---

## Contents

- [Security model](#security-model)
- [Install](#install)
- [Filesystem router](#filesystem-router)
- [Running commands: `arma_exec`](#running-commands-arma_exec)
- [Grants: `arma-elevate` / `arma-revoke`](#grants-arma-elevate--arma-revoke)
- [Fleet config: `arma config`](#fleet-config-arma-config)
- [Session roaming: `arma dock` / `undock`](#session-roaming-arma-dock--undock)
- [Other commands](#other-commands)
- [Testing](#testing) · [Layout](#layout) · [Status](#status) · [License](#license)

---

## Security model

armamentarium enforces policy at the **tool boundary**. The rules:

1. **Fail closed.** With no config, the router serves no paths, and with no exec policy,
   no commands run. A malformed config refuses everything.
2. **The operator owns the policy, and agents can't reach it.** `router.json`,
   `exec.json` and the grants file live in `~/.config/armamentarium/`, which you should
   never allowlist. The loaders refuse a policy that is group/world-writable or not owned
   by you.
3. **Grants are time-boxed and only the operator can make them.** `arma-elevate` is
   installed root-owned in `/usr/local/bin` and run by you in your own shell. No tool
   writes grants. A grant enables a named rule or path for minutes (default 15 for exec,
   hard cap 4 hours), then it lapses on its own.
4. **argv, never a shell.** `arma_exec` takes a list of arguments and runs it with
   `shell=False`. Pipes, `;`, `$()` and globs mean nothing. Programs are looked up on a
   fixed system PATH, so a writable `~/bin` can't shadow `git`. The environment is minimal
   and stdin is closed.
5. **Allowlist the shape, not the string.** A rule is an argv pattern with typed slots,
   plus a list of the *only* options allowed. `git log --output=…`,
   `journalctl --vacuum-time`, and `git -c core.sshCommand=…` are refused by construction,
   because they aren't listed. No blacklist to keep current.
6. **Things git would execute are switched off.** `.git/` and hook directories are
   unwritable through the router, even with a grant. Every git call also runs with
   fsmonitor off, `hooksPath` pointed at a root-owned directory of hooks you've approved,
   `protocol.ext` disabled, and no external diff or textconv.
7. **Refusals teach.** A refusal names the rule that came closest and the token that
   failed. When a grant would help, it gives the exact `arma-elevate` command.
8. **Everything is logged.** Every decision, allowed or refused, goes to
   `~/.local/state/armamentarium/exec.log`. Every grant that ends goes to `grants.log`.
   Two logs, two questions: what was *allowed*, and what actually *ran*.
9. **Shared storage is untrusted.** Anything an agent can write, like a synced config
   store, is treated as hostile when read back. See [Fleet config](#fleet-config-arma-config).

### What it is not

- **Not a sandbox.** The server runs as **your user**. It controls what *its own tools*
  will do. It can't stop an agent that also has a different, broader tool (another MCP
  server with write access, a shell). Audit everything you connect, and treat
  armamentarium as the narrow door you want everything to go through.
- **Not multi-user.** One operator per machine. Grants are local to the host they're
  made on.
- **Not remote, yet.** `arma_exec` runs on the local host only. A `host` argument exists,
  but remote execution (a dedicated user plus a forced-command shim on each target)
  isn't built yet.

---

## Install

Requires Python ≥ 3.10 on Linux. Install into a venv. On PEP 668 systems (Debian,
Ubuntu) that's the only sane option, and it keeps the MCP dependency tree away from
apt's Python.

```bash
python3 -m venv ~/.venvs/armamentarium
~/.venvs/armamentarium/bin/pip install git+https://github.com/uncletallest-productions/armamentarium
```

Or from a clone, which you'll want anyway for `bin/arma-grant` and the example configs:

```bash
git clone https://github.com/uncletallest-productions/armamentarium ~/src/armamentarium
~/.venvs/armamentarium/bin/pip install ~/src/armamentarium
```

This installs three commands into the venv: `armamentarium-fs` (the MCP server),
`armamentarium`, and `arma` (the CLI, two names for the same thing). Link the CLI onto
your PATH if you want it there:

```bash
ln -s ~/.venvs/armamentarium/bin/arma ~/.local/bin/arma
```

**Grant tooling, root-owned.** This matters: if an agent can write the script you use to
grant access, it can grant itself access.

```bash
sudo install -o root -g root -m 0755 bin/arma-grant /usr/local/bin/arma-grant
sudo ln -sf arma-grant /usr/local/bin/arma-elevate
sudo ln -sf arma-grant /usr/local/bin/arma-revoke
```

**Approved git hooks (optional).** Agent git calls use this directory as `hooksPath`,
never the repo's own hooks. Leave it empty for no hooks, or copy in hooks you've reviewed,
for example one that refuses commits that drop an exec bit:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/share/armamentarium/git-hooks
```

**Hook it into your MCP client.** It's a local stdio server:

```json
{
  "mcpServers": {
    "armamentarium": {
      "command": "/home/youruser/.venvs/armamentarium/bin/armamentarium-fs"
    }
  }
}
```

Restart the client fully after changing its config. MCP servers keep the arguments they
were launched with.

---

## Filesystem router

The router serves nothing until `~/.config/armamentarium/router.json` grants roots:

```bash
mkdir -p ~/.config/armamentarium
cp config/router.example.json ~/.config/armamentarium/router.json
$EDITOR ~/.config/armamentarium/router.json
```

Each root has `"access": "r"` or `"rw"`. Paths are resolved (`~`, symlinks) **before** the
allowlist check, so `../` and symlink escapes can't leave a root. The config is found
through `$ARMAMENTARIUM_ROUTER_CONFIG`, then `~/.config/armamentarium/router.json`.

A path grant (`arma-elevate path <dir> [r|rw] [ttl]`) can add a root for a while, or
temporarily upgrade a read-only root to read-write. A grant never downgrades access.
Writes and deletes under `.git/`, `.githooks/` or `tests/hooks/` are always refused.

### Tools

| Tool | What it does |
|------|--------------|
| `arma_read_file` | Read a UTF-8 text file (truncation cap) |
| `arma_write_file` | Atomic write; refuses to overwrite or create parents unless asked |
| `arma_delete_path` | Delete a file or directory; requires `confirm: true` (and `recursive` for non-empty dirs) |
| `arma_list_directory` | Entries with type and size |
| `arma_stat` | Type, size, mtime, mode |
| `arma_search_files` | Find by name glob and/or literal content, depth- and result-bounded |
| `arma_exec` | Run one command allowed by `exec.json` (see below) |
| `arma_exec_rules` | List the exec rules, roots, and active exec grants |
| `arma_router_status` | Loaded roots, backends, and active grants with time left |

---

## Running commands: `arma_exec`

`arma_exec` runs a command **only** if a rule in `~/.config/armamentarium/exec.json`
matches it. Start from the example:

```bash
cp config/exec.example.json ~/.config/armamentarium/exec.json
chmod 0644 ~/.config/armamentarium/exec.json
```

### Rule format

```json
"roots": {
  "repos-read":  ["~/projects/*"],
  "repos-write": ["~/projects/*/*"]
},
"rules": {
  "git-log": {
    "mode": "always",
    "argv": ["git", "-C", "{path:repos-read}", "log"],
    "options": {"--oneline": null, "-n": "{int}", "--since": "{time}"},
    "args": ["{ref?}"]
  },
  "git-push": {
    "mode": "elevated",
    "argv": ["git", "-C", "{path:repos-write}", "push"],
    "options": {"-u": null},
    "args": ["{word?}", "{name?}"],
    "timeout": 60
  }
}
```

- **`argv`** is a fixed prefix. Each element is a literal, an `a|b` alternative, or a
  typed slot.
- **Slots:** `{int}` `{name}` `{word}` `{unit}` `{host}` `{time}` `{text}` `{ref}`
  `{dataset}`. A `?` suffix makes a slot optional. `{path:ROOT}` must resolve (realpath)
  under one of the named root's patterns, and the command runs with the resolved path,
  not the caller's spelling.
- **`options`** are the *only* flags allowed after the prefix, each with a value type, or
  `null` for a bare flag. Bundled short flags (`-sb`) aren't expanded: write `-s -b`.
- **`args`** are the positional arguments after the options.
- **`mode`**: `always` runs with no grant. `elevated` needs a live grant for that rule on
  this host. A rule without a mode is elevated.
- **`sudo: true`** runs through `sudo -n` (never prompts). It needs a NOPASSWD sudoers
  line for that exact command; there is no blanket sudo.
- **`hosts`** (default all), **`timeout`** (seconds), **`max_output`** (bytes).

A good rule of thumb: **reads are `always`, anything that changes state is `elevated`**.
Leave out reads that return secrets entirely (`env`, `git config`, unrestricted
`systemctl show`), since everything a command prints lands in the conversation.

### What a call returns

```json
{"allowed": true, "reason": "allowed by 'git-log' (always)", "rule": "git-log",
 "exit_code": 0, "stdout": "...", "stderr": "", "truncated": false,
 "timed_out": false, "duration_ms": 18, "ran": ["/usr/bin/git", "-c", "...", "log", "..."]}
```

`ran` is the exact argv that executed, hardening included. A refusal looks like this:

```json
{"allowed": false, "rule": "git-push",
 "reason": "needs rule 'git-push', which requires the operator's approval and there is no active grant for it on myhost",
 "hint": "arma-elevate exec git-push [ttl]", "ran": []}
```

### User services and the session bus

Commands like `systemctl --user` need `XDG_RUNTIME_DIR` and the user's D-Bus, and MCP
servers are often launched without them. The runner fills them in from
`armamentarium.session_env`. It accepts an inherited or derived value only if it checks
out: `/run/user/<uid>` must be a real directory you own with mode 0700, and the bus a
socket you own inside it. You can use the same module from any shell:

```bash
eval "$(~/.venvs/armamentarium/bin/python -m armamentarium.session_env)"
~/.venvs/armamentarium/bin/python -m armamentarium.session_env --check   # explains each decision
```

(If `--check` reports a bus in `/tmp`, your desktop was probably started under
`dbus-run-session`, which gives it a private bus separate from systemd's. Starting the
compositor without that wrapper is usually the fix.)

---

## Grants: `arma-elevate` / `arma-revoke`

Run these yourself, never with sudo:

```bash
arma-elevate exec <rule> [host] [ttl]     # enable a named exec rule (default 15m)
arma-elevate path <dir> [r|rw] [ttl]      # add or upgrade a filesystem root (default rw, 1h)
arma-elevate list [expired] [--since T]   # active grants, or the ledger of ended ones
arma-revoke exec <rule> [host]            # end one grant now
arma-revoke path <dir>
arma-revoke all                           # end everything
arma-revoke                               # refuses and lists, never an implicit 'all'
```

TTL is `900`, `30s`, `15m`, `2h` or `1d`. The hard cap is 4 hours; longer requests are
clamped, and it tells you so. `host` defaults to this machine. A grant names a **rule**,
not a command: it can't authorize an argv that no rule describes, so
`arma-elevate exec "rm -rf /"` has nothing to enable.

Live grants are in `~/.config/armamentarium/elevate.json` (0600; `arma-grant` is its only
writer, and the router only reads it). Ended grants move to
`~/.local/state/armamentarium/grants.log` with how they ended: `revoked`, `revoke-all` or
`expired`.

---

## Fleet config: `arma config`

Capture selected files from `~/.config` into a shared, git-backed store, compare hosts, and
restore a host from its last-known-good copy:

```bash
arma config capture              # this host's tracked files -> store (commits only this host's dir)
arma config status               # tracked vs captured, live-vs-store drift
arma config diff hostA hostB     # compare two hosts' captures
arma config deploy               # PLAN: what a restore would change, with diffs
arma config deploy --apply       # restore, interactively
arma config adopt [--apply]      # propose files already in the store for tracking
```

The store root is `$ARMA_FLEET_ROOT`, or `~/Datasets/Profectionis/fleet-config` by default.
Override it for your layout.

**Trust model.** The store is shared, so assume an agent can write to it. Several
`~/.config` files run code (login autostart, systemd user units, MCP server commands,
armamentarium's own policy), so restoring blindly from the store would be remote code
execution with extra steps. Instead:

- **The tracked list is local and operator-owned:** `~/.config/armamentarium/tracked.txt`
  on each host. The store copy is generated and informational. Capture refuses `..`,
  absolute paths, symlink escapes, and a deny list of secret-looking paths (credential
  dirs, browser profiles, key files, names containing *token*, *secret*, *password* and
  similar).
- **`deploy` is a dry run** by default and prints a unified diff for every change.
- **`deploy --apply` needs a terminal:** one `[y/N]` for the set (default No), plus the
  **file name typed** for every code-running file (`labwc/`, `kanshi/`, `systemd/`,
  `autostart/`, `environment.d/`, the Claude Desktop config, and anything with a shebang
  or exec bit).
- **`exec.json` and `router.json` are never written by deploy.** You get the diff and an
  `install` command for a reviewed snapshot outside the store. `elevate.json` is never
  synced in either direction.
- **What gets written is exactly what you reviewed:** the bytes are captured at plan time.
  A file that changed since the plan is skipped. New files get 0644, so an exec bit can't
  ride in from the store.

`config/armamentarium-config-watch.{service,sh}` is an optional user unit that runs
`capture` when tracked files change.

---

## Session roaming: `arma dock` / `undock`

Move a Claude Desktop **chat** session between machines over shared storage. Docking
syncs the session directory and sidecar into this machine's session tree, then rewrites
stale mount-root paths (and the device UUID, if it differs). Scheduled tasks are out of
scope.

```bash
cp config/dock.example.json ~/.config/armamentarium/dock.json     # mount-root map, source dirs
arma list                                   # dockable sessions
arma dock <session-dir>                     # plan only
arma dock <session-dir> --apply --restart   # stop Desktop, dock, start Desktop
arma undock <local-session-dir> --remove-local
arma roaming status                         # who holds which session
```

A checkout ledger on the shared store keeps one session live on one machine at a time.
`dock --apply` refuses if another machine holds the session (`--force` overrides). The
optional `desktop.json` lets `--restart` stop and start Claude Desktop (`arma desktop
status|start|stop|restart`).

> Claude Desktop's on-disk session layout changes between releases. If `list` comes up
> empty after an app update, check that `dock.json`'s paths still match before docking.

---

## Other commands

- `arma fleet ls|status|enroll|heartbeat|set-master|sessions`: a small machine registry
  (presence, master designation) on the shared store.
- `arma packages apt sync-cache [--apply]`: copies new `.deb`s from this machine's apt
  cache into a flat offline repo and triggers its reindex. A dry run by default.
- `arma router status`: the router's view of its config, from the CLI.

---

## Testing

```bash
cd ~/src/armamentarium
~/.venvs/armamentarium/bin/python -m unittest discover -s tests
```

Run the tests as a **non-root** user. They use real subprocesses in temp directories (git
repos, unix sockets) and play attacker against the policy, grant, and deploy code: hostile
argv, symlink and `..` escapes, planted git hooks and fsmonitor, a tampered config store,
and more. There are about 150 tests, all stdlib `unittest`; no pytest needed.

---

## Layout

```
armamentarium/
  armamentarium/
    cli.py           # `arma ...` entry point
    exec_policy.py   # exec.json loader + matcher (pure; decides, runs nothing)
    exec_runner.py   # runs an allowed argv: fixed PATH, minimal env, git hardening, logging
    session_env.py   # verified XDG_RUNTIME_DIR + session D-Bus discovery
    config_sync.py   # fleet config capture/diff/deploy/adopt/status + trust model
    dock.py          # session dock planning/applying
    roaming.py       # checkout ledger + undock
    desktop.py       # optional Claude Desktop process control
    fleet.py         # machine registry
    packages.py      # package-state sync (apt sync-cache so far)
    mcp_router/
      config.py      # router.json + elevate.json loading (security boundary)
      router.py      # path resolution, allowlist, protected paths, grants
      backends.py    # Backend interface + local filesystem backend
      server.py      # the MCP tool surface
  bin/
    arma-grant       # grant CLI (stdlib only; installed root-owned)
    arma-elevate -> arma-grant
    arma-revoke  -> arma-grant
  config/            # *.example.json + optional config-watch unit
  docs/
    NAMING.md        # the Latin naming scheme (shared across repos)
  tests/
  pyproject.toml
```

---

## Status

**v0.1.0.** Used daily on a small Debian trixie fleet (labwc/Wayland desktops, NFS-shared
storage, tailnet).

Done: the filesystem router with path grants; `arma_exec` with policy, grants, hardening
and logging; session roaming with the checkout ledger; fleet config with the untrusted-store
deploy model; verified session-bus discovery.

Not yet:
- **Remote exec:** a per-host `cognate` user, a forced-command shim re-checking the policy
  on the target, ssh transport.
- **Single-use grants:** `arma-elevate run <file>` with sha256 pinning.
- **A sudoers generator** built from `exec.json`.
- **A GUI** that explains rules, grants and history in plain language.
- **Remote router backends.**
- **Portable scheduled tasks.**
- **The rest of `packages`:** Flatpak, Snap and AppImage.

---

## License

MIT. See [LICENSE](LICENSE).
