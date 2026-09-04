#!/usr/bin/env bash
# armamentarium-config-watch.sh — optional automatic-capture layer.
#
# `armamentarium config capture` (config_sync.py) is deliberately a plain,
# explicit, scriptable verb — nothing in it watches anything. This script is
# the separate, optional wrapper that makes capture automatic: it watches the
# directories holding this host's tracked.txt paths and calls `armamentarium
# config capture` whenever one of them changes.
#
# WHY NOT a systemd .path unit with PathModified=: tried first, and it's
# broken for this class of file. Every writer worth trusting here (labwc,
# kanshi, this project's own atomic-write helpers) writes via temp-file +
# rename, which the kernel reports as IN_MOVE_SELF on the OLD inode, not
# IN_MODIFY — so PathModified= silently never fires. Diagnosed by a parallel
# Dom1 session, 2026-08-28, logged in domain1-arma-fleet-capture-deploy.md.
# The fix: watch the *directory* for close_write/moved_to/create and filter
# by filename, which is what this script does via inotifywait.
#
# Requires: inotify-tools (inotifywait). NOT currently in
# fleet-offline-repo's base-system.txt fragment — needs adding there
# (flagged as an open item; can't edit that fragment directly, its mount is
# read-only from every client this session has).
#
# Usage: armamentarium-config-watch.sh   (run under systemd, see the
# matching .service unit — Type=simple, Restart=always)

set -euo pipefail

TRACKED_FILE="${HOME}/Datasets/Cubicles/Continuatus/arma-fleet/config/$(hostname -s)/tracked.txt"
DEBOUNCE_SECONDS=2

if [ ! -f "$TRACKED_FILE" ]; then
  echo "no tracked.txt at $TRACKED_FILE — nothing to watch, exiting" >&2
  exit 1
fi

# Build the set of watched files (absolute) and their parent directories
# (deduped — inotify watches directories, not individual files).
mapfile -t TRACKED < <(grep -vE '^\s*(#|$)' "$TRACKED_FILE" | sed "s|^|${HOME}/.config/|")
declare -A WATCH_DIRS
for f in "${TRACKED[@]}"; do
  WATCH_DIRS["$(dirname "$f")"]=1
done

if [ "${#WATCH_DIRS[@]}" -eq 0 ]; then
  echo "tracked.txt is empty — nothing to watch, exiting" >&2
  exit 1
fi

echo "watching ${#WATCH_DIRS[@]} director(y|ies) for ${#TRACKED[@]} tracked file(s)"

last_run=0
inotifywait -m -r -e close_write -e moved_to -e create --format '%w%f' "${!WATCH_DIRS[@]}" |
while read -r changed; do
  # Only react if the changed path is actually one we track.
  match=0
  for f in "${TRACKED[@]}"; do
    [ "$changed" = "$f" ] && match=1 && break
  done
  [ "$match" -eq 1 ] || continue

  now=$(date +%s)
  if [ $((now - last_run)) -lt "$DEBOUNCE_SECONDS" ]; then
    continue  # coalesce rapid-fire writes (editors often touch a file twice)
  fi
  last_run=$now

  echo "change detected: $changed — capturing"
  armamentarium config capture || echo "capture failed, will retry on next change" >&2
done
