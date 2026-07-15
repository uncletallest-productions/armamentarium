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
        plan = build_plan(args.session, path_map, account=args.account, device=args.device)
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
    print(f"\nundo: {result['cleanup']}")

    if args.restart and cfg is not None:
        try:
            print("starting Claude Desktop ...")
            print("  " + json.dumps(desktop_mod.start(cfg)))
        except DesktopError as e:
            print(f"warning: dock succeeded but restart failed: {e}", file=sys.stderr)
            return 1
    return 0


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

    r = sub.add_parser("router", help="filesystem router utilities")
    rsub = r.add_subparsers(dest="router_cmd", required=True)
    rst = rsub.add_parser("status", help="print router roots + backends (from router.json)")
    rst.set_defaults(func=cmd_router)
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
