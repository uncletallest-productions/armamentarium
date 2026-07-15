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
        return 0

    try:
        result = apply_plan(plan, path_map, force=args.force, allow_scheduled=args.allow_scheduled)
    except DockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("DOCKED.")
    print(json.dumps(result, indent=2))
    print(f"\nundo: {result['cleanup']}")
    return 0


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
    d.add_argument("--force", action="store_true", help="overwrite an existing session of the same id")
    d.add_argument("--allow-scheduled", action="store_true",
                   help="force-copy a scheduled-task session (out of scope; will likely be inert)")
    d.set_defaults(func=cmd_dock)
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
