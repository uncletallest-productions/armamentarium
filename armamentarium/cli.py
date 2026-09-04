#!/usr/bin/env python3
"""armamentarium CLI — `armamentarium dock <session-dir>`.

Dry-run by default: prints a plan and writes nothing. Add --apply to execute.

Examples:
    # preview docking an archived session onto this machine
    armamentarium dock ~/Datasets/Substrate/dom0/sisyphus-archive/sessions/local_<uuid>

    # actually do it, with an explicit extra path mapping
    armamentarium dock <session-dir> --apply \\
        --path-map /home/youruser/Scriptorium=/home/youruser/Datasets/Scriptorum
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List

from .dock import DockError, apply_plan, build_plan, load_path_map, render_plan
from . import desktop as desktop_mod
from .desktop import DesktopError


def _parse_path_maps(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--path-map expects old=new, got: {p!r}")
        old, new = p.split("=", 1)
        out[old] = new
    return out


def cmd_dock(args: argparse.Namespace) -> int:
    path_map = load_path_map(_parse_path_maps(args.path_map))
    try:
        plan = build_plan(args.session, path_map, account=args.account, device=args.device,
                          root=args.root)
    except DockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(render_plan(plan))
    print()

    if not args.apply:
        print("(dry run — nothing written. Re-run with --apply to execute.)")
        if not path_map:
            print("note: no path map loaded — configure ~/.config/armamentarium/dock.json "
                  "or pass --path-map to rewrite stale mount roots.")
        if args.restart:
            print("note: --restart has no effect without --apply.")
        return 0

    # Ledger guard: refuse if another machine holds this session live (best-effort —
    # skipped cleanly if the roaming store isn't present). Records holder=this host.
    from . import roaming
    ok, note = roaming.try_checkout(plan.session_id, force=args.force, account=args.account)
    if not ok:
        print(f"error: {note}", file=sys.stderr)
        return 2

    # With --restart: stop Desktop first so we never write under a live app,
    # then apply, then start it back up so it re-scans the session tree.
    cfg = None
    if args.restart:
        try:
            cfg = desktop_mod.load_desktop_config()
            print("stopping Claude Desktop ...")
            print("  " + json.dumps(desktop_mod.stop(cfg, force=args.force)))
        except DesktopError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    try:
        result = apply_plan(plan, path_map, force=args.force, allow_scheduled=args.allow_scheduled)
    except DockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("DOCKED.")
    print(json.dumps(result, indent=2))
    print(f"ledger: {note}")
    print(f"\nundo: {result['cleanup']}")

    if args.restart and cfg is not None:
        try:
            print("starting Claude Desktop ...")
            print("  " + json.dumps(desktop_mod.start(cfg)))
        except DesktopError as e:
            print(f"warning: dock succeeded but restart failed: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .dock import (load_source_dirs, scan_sessions, local_device_dir, local_roots_config,
                       discover_all_roots, resolve_root_group, DockError as _DockError)
    root_group = getattr(args, "root_group", None)
    if root_group:
        # --root-group only makes sense with --local: each root has its OWN docked-session
        # tree, so "list what's docked, across every root in this group" is well-defined.
        # Without --local, `list` scans root-agnostic source dirs (the roaming store /
        # source_dirs) -- there's no single shared source to scan "per group member".
        if not args.local:
            print("error: --root-group requires --local (a group is a set of local trees to "
                  "check docked-status against; there's no group-wide source dir to scan "
                  "otherwise)", file=sys.stderr)
            return 2
        try:
            members = resolve_root_group(root_group)  # [(label, path), ...]
        except _DockError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        sessions: List[dict] = []
        for label, _ in members:
            try:
                dirs = [str(local_device_dir(account=args.account, root=label))]
            except _DockError as e:
                print(f"  ⚠ {label}: {e}", file=sys.stderr)
                continue
            sessions.extend(scan_sessions(dirs, root=label))
        if args.json:
            print(json.dumps(sessions, indent=2))
            return 0
        if not sessions:
            print("(no sessions found)")
            return 0
        for s in sessions:
            kind = "task" if s["scheduled"] else "chat"
            dk = "docked" if s["docked"] else ("new" if s["docked"] is False else "?")
            print(f"[{s['root']:12}] [{kind}] {dk:6} {s['session_id']}  {s['title']!r}  {s['folders']}")
        return 0
    if getattr(args, "roots", False):
        roots = local_roots_config()
        discovered = {label: (acct, dev) for label, acct, dev, _ in discover_all_roots()}
        if args.json:
            print(json.dumps({
                label: {"path": str(p), "exists": p.exists(),
                        "account": discovered.get(label, (None, None))[0],
                        "device": discovered.get(label, (None, None))[1]}
                for label, p in roots.items()
            }, indent=2))
        else:
            for label, p in roots.items():
                acct, dev = discovered.get(label, (None, None))
                status = f"account={acct} device={dev}" if acct else ("(exists, ambiguous or empty)" if p.exists() else "(not found)")
                print(f"{label:16} {p}  {status}")
        return 0
    if args.local:
        try:
            dirs = [str(local_device_dir(account=args.account, root=args.root))]
        except DockError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        from . import roaming
        # Scan the roaming store FIRST (freshest undocked copies win dedup), then source_dirs.
        store = roaming.store_sessions_dir(account=args.account)
        base_dirs = [args.dir] if args.dir else load_source_dirs()
        dirs = ([str(store)] if store else []) + base_dirs
    if not dirs:
        print("error: no source dir given and no 'source_dirs' in dock.json", file=sys.stderr)
        return 2
    sessions = scan_sessions(dirs, root=args.root)
    if not args.local:
        # annotate ledger holder so callers can flag sessions checked out elsewhere
        from . import roaming
        hold = roaming.holders(account=args.account)
        me = roaming.machine_id()
        for s in sessions:
            h = hold.get(s["session_id"])
            s["held_by"] = h
            s["held_by_other"] = bool(h and h != me)
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("(no sessions found)")
        return 0
    for s in sessions:
        kind = "task" if s["scheduled"] else "chat"
        dk = "docked" if s["docked"] else ("new" if s["docked"] is False else "?")
        print(f"[{kind}] {dk:6} {s['session_id']}  {s['title']!r}  {s['folders']}")
    return 0


def cmd_undock(args: argparse.Namespace) -> int:
    from . import roaming
    # --restart: stop Desktop → flush+release(+remove) → start, so we snapshot a quiescent
    # session and never delete under a live app.
    cfg = None
    if args.restart:
        try:
            cfg = desktop_mod.load_desktop_config()
            print("stopping Claude Desktop ...")
            print("  " + json.dumps(desktop_mod.stop(cfg, force=args.force)))
        except DesktopError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    try:
        result = roaming.undock(args.session, remove_local=args.remove_local, account=args.account)
    except (roaming.RoamingError, DockError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("UNDOCKED.")
    print(json.dumps(result, indent=2))
    if args.restart and cfg is not None:
        try:
            print("starting Claude Desktop ...")
            print("  " + json.dumps(desktop_mod.start(cfg)))
        except DesktopError as e:
            print(f"warning: undock succeeded but restart failed: {e}", file=sys.stderr)
            return 1
    return 0


def cmd_roaming(args: argparse.Namespace) -> int:
    from . import roaming
    try:
        if args.roaming_cmd == "status":
            print(json.dumps(roaming.status(account=args.account), indent=2))
        elif args.roaming_cmd == "checkout":
            print(json.dumps(roaming.checkout(args.session_id, force=args.force, account=args.account), indent=2))
        elif args.roaming_cmd == "checkin":
            prev = roaming.checkin(args.session_id, account=args.account)
            print(json.dumps({"released": args.session_id, "was_held_by": (prev or {}).get("machine")}, indent=2))
        return 0
    except (roaming.RoamingError, DockError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_router(args: argparse.Namespace) -> int:
    # Lazy import so this command works without the mcp server deps loaded.
    from .mcp_router.config import load_config
    from .mcp_router.router import FilesystemRouter
    try:
        r = FilesystemRouter(load_config())
        print(json.dumps(r.status(), indent=2))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_fleet(args: argparse.Namespace) -> int:
    from . import fleet
    try:
        if args.fleet_cmd == "status":
            print(json.dumps(fleet.status(account=args.account), indent=2))
        elif args.fleet_cmd == "ls":
            rows = fleet.ls()
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for m in rows:
                    crown = "👑" if m["master"] else "  "
                    dot = "🟢" if m["online"] else "🔴"
                    me = " (me)" if m["me"] else ""
                    locks = f"  ⚠ stale locks: {m['stale_locks']}" if m["stale_locks"] else ""
                    seen = m["last_seen"] or "never"
                    print(f"{crown} {dot} {m['hostname']}{me}  last={seen}{locks}")
        elif args.fleet_cmd == "enroll":
            hw: dict = {}
            if args.hardware:
                for kv in args.hardware:
                    if "=" not in kv:
                        print(f"error: --hardware expects key=value, got {kv!r}", file=sys.stderr)
                        return 2
                    k, v = kv.split("=", 1)
                    # coerce numeric values
                    try:
                        hw[k] = int(v)
                    except ValueError:
                        try:
                            hw[k] = float(v)
                        except ValueError:
                            hw[k] = v
            result = fleet.enroll(
                display_name=args.display_name,
                dock_capable=not args.no_dock,
                hardware=hw or None,
                as_master=args.master,
            )
            print(json.dumps(result, indent=2))
        elif args.fleet_cmd == "set-master":
            print(json.dumps(fleet.set_master(args.hostname), indent=2))
        elif args.fleet_cmd == "heartbeat":
            print(json.dumps(fleet.heartbeat(), indent=2))
        elif args.fleet_cmd == "sessions":
            result = fleet.sessions(account=args.account, timeout=args.timeout)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                sess_list = result["sessions"]
                if not sess_list:
                    print("(no sessions found across fleet)")
                else:
                    cur_machine = None
                    for s in sess_list:
                        mach = s["machine"]
                        if mach != cur_machine:
                            cur_machine = mach
                            tag = " (this machine)" if s["is_local"] else ""
                            print(f"\n── {mach}{tag} ──")
                        kind = "task" if s.get("scheduled") else "chat"
                        status_tag = s.get("held_by_other") and f"held:{s['held_by']}" or \
                                     ("docked" if s.get("docked") else "new")
                        print(f"  [{kind}] {status_tag:12} {s['session_id']}  {(s.get('title') or '')!r}")
                if result["errors"]:
                    print()
                    for e in result["errors"]:
                        print(f"  ⚠ {e['hostname']}: {e['error']}", file=sys.stderr)
                if result["machines_skipped"]:
                    skipped = ", ".join(f"{s['hostname']} ({s['reason']})" for s in result["machines_skipped"])
                    print(f"\nskipped: {skipped}")
        return 0
    except fleet.FleetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


<<<<<<< Updated upstream
=======
def cmd_config(args: argparse.Namespace) -> int:
    """`armamentarium config capture|diff|deploy|status` — fleet dotfile/config
    sync. Deliberately separate from `armamentarium fleet` (machine presence/
    registry) to avoid colliding with the existing `fleet status` verb."""
    from . import config_sync
    try:
        if args.config_cmd == "capture":
            print(json.dumps(config_sync.capture(host=args.host), indent=2))
        elif args.config_cmd == "diff":
            print(json.dumps(config_sync.diff(args.host_a, args.host_b).as_dict(), indent=2))
        elif args.config_cmd == "deploy":
            result = config_sync.deploy(host=args.host, force=args.force)
            print(json.dumps(result, indent=2))
            if result["refused_drift"]:
                return 1
        elif args.config_cmd == "status":
            print(json.dumps(config_sync.status(host=args.host), indent=2))
        elif args.config_cmd == "untrack":
            config_sync.untrack(args.path, host=args.host)
            print(f"untracked: {args.path}")
        elif args.config_cmd == "adopt":
            result = config_sync.adopt(host=args.host)
            print(json.dumps(result, indent=2))
            if result["new"]:
                print(f"\nadded {len(result['new'])} path(s) to tracked.txt — run "
                      "`armamentarium config capture` to actually mirror them.")
            if result["no_live_source"]:
                print(f"\n{len(result['no_live_source'])} mirrored file(s) have no live "
                      "counterpart under ~/.config — left untouched, not adopted:")
                for p in result["no_live_source"]:
                    print(f"  {p}")
        return 0
    except config_sync.ConfigSyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


>>>>>>> Stashed changes
def cmd_desktop(args: argparse.Namespace) -> int:
    try:
        cfg = desktop_mod.load_desktop_config()
        action = {
            "status": lambda: desktop_mod.status(cfg),
            "start": lambda: desktop_mod.start(cfg),
            "stop": lambda: desktop_mod.stop(cfg, force=args.force),
            "restart": lambda: desktop_mod.restart(cfg, force=args.force),
        }[args.action]
        print(json.dumps(action(), indent=2))
        return 0
    except DesktopError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="armamentarium", description="Portable Claude Desktop sessions.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("dock", help="port a chat session onto this machine (dry-run by default)")
    d.add_argument("session", help="path to the source 'local_<uuid>' session directory")
    d.add_argument("--apply", action="store_true", help="actually perform the dock (default: dry-run)")
    d.add_argument("--path-map", action="append", metavar="OLD=NEW", default=[],
                   help="add/override a mount-root rewrite (repeatable)")
    d.add_argument("--account", help="account UUID override (if the local tree has more than one)")
    d.add_argument("--device", help="device UUID override (if the local tree has more than one)")
    d.add_argument("--root", metavar="LABEL",
                   help="which configured local tree to dock INTO (see 'list --roots'); "
                        "default: primary (~/.config/Claude/local-agent-mode-sessions). "
                        "Use this for e.g. claude-harness profiles -- docking a harness "
                        "session with no --root would land it in the primary Claude tree, "
                        "which is almost never what you want.")
    d.add_argument("--force", action="store_true",
                   help="overwrite an existing session id (and SIGKILL Desktop if --restart can't stop it gracefully)")
    d.add_argument("--allow-scheduled", action="store_true",
                   help="force-copy a scheduled-task session (out of scope; will likely be inert)")
    d.add_argument("--restart", action="store_true",
                   help="stop Claude Desktop before applying and start it after (needs desktop.json)")
    d.set_defaults(func=cmd_dock)

    ds = sub.add_parser("desktop", help="control the Claude Desktop process (needs desktop.json)")
    ds.add_argument("action", choices=["status", "start", "stop", "restart"])
    ds.add_argument("--force", action="store_true", help="SIGKILL if graceful SIGTERM times out (stop/restart)")
    ds.set_defaults(func=cmd_desktop)

    ud = sub.add_parser("undock", help="flush a live session back to the shared store + release it")
    ud.add_argument("session", help="path to the local 'local_<uuid>' session directory")
    ud.add_argument("--remove-local", action="store_true", help="delete the local copy after flushing")
    ud.add_argument("--restart", action="store_true", help="stop Desktop before, start after (needs desktop.json)")
    ud.add_argument("--force", action="store_true", help="SIGKILL Desktop if graceful stop times out (with --restart)")
    ud.add_argument("--account", help="account UUID override")
    ud.set_defaults(func=cmd_undock)

    rm = sub.add_parser("roaming", help="checkout ledger (cross-machine session coordination)")
    rmsub = rm.add_subparsers(dest="roaming_cmd", required=True)
    rmst = rmsub.add_parser("status", help="show the ledger (who holds what)")
    rmst.add_argument("--account", help="account UUID override")
    rmst.set_defaults(func=cmd_roaming)
    rmco = rmsub.add_parser("checkout", help="manually mark a session held by this machine")
    rmco.add_argument("session_id")
    rmco.add_argument("--force", action="store_true", help="override another machine's hold")
    rmco.add_argument("--account", help="account UUID override")
    rmco.set_defaults(func=cmd_roaming)
    rmci = rmsub.add_parser("checkin", help="release a session's checkout")
    rmci.add_argument("session_id")
    rmci.add_argument("--account", help="account UUID override")
    rmci.set_defaults(func=cmd_roaming)

    ls = sub.add_parser("list", help="list dockable sessions from source dirs")
    ls.add_argument("dir", nargs="?", help="source dir (default: source_dirs from dock.json)")
    ls.add_argument("--local", action="store_true", help="list sessions docked on THIS machine (undock candidates)")
    ls.add_argument("--roots", action="store_true",
                    help="show configured local root trees instead of sessions "
                         "(primary + any local_roots from dock.json, e.g. claude-harness profiles)")
    ls.add_argument("--root", metavar="LABEL",
                    help="which configured local tree to check against (with --local), or "
                         "to resolve 'docked' status against otherwise; default: primary")
    ls.add_argument("--root-group", metavar="GROUP",
                    help="list docked sessions across every root label in this dock.json "
                         "root_groups entry, merged into one table with a root column "
                         "(e.g. 'harness-all' spanning every claude-harness profile). "
                         "Requires --local; overrides --root.")
    ls.add_argument("--account", help="account UUID override (with --local)")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_list)

    r = sub.add_parser("router", help="filesystem router utilities")
    rsub = r.add_subparsers(dest="router_cmd", required=True)
    rst = rsub.add_parser("status", help="print router roots + backends (from router.json)")
    rst.set_defaults(func=cmd_router)

    fl = sub.add_parser("fleet", help="machine fleet registry (presence, master designation)")
    flsub = fl.add_subparsers(dest="fleet_cmd", required=True)

    fls = flsub.add_parser("status", help="full fleet view: machines, online/offline, stale locks")
    fls.add_argument("--account", help="account UUID override (for checkout ledger cross-ref)")
    fls.set_defaults(func=cmd_fleet)

    fll = flsub.add_parser("ls", help="compact machine list")
    fll.add_argument("--json", action="store_true", help="machine-readable output")
    fll.add_argument("--account", help="account UUID override")
    fll.set_defaults(func=cmd_fleet)

    fle = flsub.add_parser("enroll", help="register this machine in the shared fleet registry")
    fle.add_argument("--display-name", help="human-readable machine name")
    fle.add_argument("--no-dock", action="store_true", help="mark this machine as not dock-capable")
    fle.add_argument("--hardware", action="append", metavar="KEY=VALUE",
                     help="hardware metadata (repeatable, e.g. gpu_vram_gb=8 role=workstation)")
    fle.add_argument("--master", action="store_true",
                     help="designate this machine as the fleet master")
    fle.add_argument("--account", help="account UUID override")
    fle.set_defaults(func=cmd_fleet)

    flm = flsub.add_parser("set-master", help="change the fleet master designation")
    flm.add_argument("hostname", help="hostname to designate as master")
    flm.add_argument("--account", help="account UUID override")
    flm.set_defaults(func=cmd_fleet)

    flh = flsub.add_parser("heartbeat", help="write/refresh this machine's presence heartbeat")
    flh.add_argument("--account", help="account UUID override")
    flh.set_defaults(func=cmd_fleet)

    fls2 = flsub.add_parser("sessions", help="fleet-wide session inventory (SSH to each dock-capable machine)")
    fls2.add_argument("--json", action="store_true", help="machine-readable output")
    fls2.add_argument("--account", help="account UUID override")
    fls2.add_argument("--timeout", type=int, default=10, metavar="SECONDS",
                      help="SSH timeout per machine (default: 10)")
    fls2.set_defaults(func=cmd_fleet)

<<<<<<< Updated upstream
=======
    cfg = sub.add_parser("config", help="fleet config capture/diff/deploy (dotfiles, NOT machine presence — see 'fleet')")
    cfgsub = cfg.add_subparsers(dest="config_cmd", required=True)

    cfgc = cfgsub.add_parser("capture", help="push this host's tracked dotfiles into the shared store")
    cfgc.add_argument("--host", help="override hostname (default: this machine)")
    cfgc.set_defaults(func=cmd_config)

    cfgd = cfgsub.add_parser("diff", help="compare two hosts' captured manifests")
    cfgd.add_argument("host_a")
    cfgd.add_argument("host_b")
    cfgd.set_defaults(func=cmd_config)

    cfgdp = cfgsub.add_parser("deploy", help="restore this host's tracked files from the store (self-heal/rollback)")
    cfgdp.add_argument("--host", help="override hostname (default: this machine; another host = deliberate manual clone)")
    cfgdp.add_argument("--force", action="store_true", help="overwrite even where the live file has uncaptured edits")
    cfgdp.set_defaults(func=cmd_config)

    cfgs = cfgsub.add_parser("status", help="tracked/captured counts, last capture time, live-vs-store drift")
    cfgs.add_argument("--host", help="override hostname (default: this machine)")
    cfgs.set_defaults(func=cmd_config)

    cfgu = cfgsub.add_parser("untrack", help="drop a path from this host's manifest (mirror copy kept as history)")
    cfgu.add_argument("path", help="path relative to ~/.config")
    cfgu.add_argument("--host", help="override hostname (default: this machine)")
    cfgu.set_defaults(func=cmd_config)

    cfga = cfgsub.add_parser("adopt", help="add pre-existing mirrored files (with a live ~/.config counterpart) to tracked.txt")
    cfga.add_argument("--host", help="override hostname (default: this machine)")
    cfga.set_defaults(func=cmd_config)

>>>>>>> Stashed changes
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
