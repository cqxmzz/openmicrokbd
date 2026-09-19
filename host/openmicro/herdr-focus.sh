#!/bin/bash
# Focus Ghostty, then a herdr tab. Bound to pad rows 2 and 3a via Karabiner
# (see karabiner.json, "OpenMicro rows 2/3 -> Ghostty + herdr tab focus").
#
#     herdr-focus.sh <tab-number> [workspace-number]
#
# Every herdr call is a round trip over the et tunnel to the devserver, ~400ms
# each -- that, not process start-up, was the 2-3s key-press latency. Two
# things keep this to a single round trip:
#
#   * herdr-led.py already resolves <ws>:<tab> -> tab_id every poll and
#     publishes it to /tmp/herdr-tabmap, so no list calls are needed here, and
#     the plain-text format means no python start-up either (~100ms each).
#   * `tab focus` switches workspace on its own (verified: focused went
#     'Manual' -> '~' with no workspace.focus call), so that call is gone.
#
# The slow path -- resolving over the socket -- is kept as a fallback for when
# the daemon is not running or the map is stale.
set -u

TAB_NUMBER="${1:-1}"
WORKSPACE_NUMBER="${2:-${WORKSPACE_NUMBER:-1}}"
# HERDR_SOCKET_PATH is the one the client reads; HERDR_CLIENT_SOCKET_PATH is
# what herdr exports into panes and is ignored here.
export HERDR_SOCKET_PATH="${HERDR_SOCK:-/tmp/herdr-remote.sock}"
HERDR="${HERDR_BIN:-/opt/homebrew/bin/herdr}"
TABMAP="${TABMAP_PATH:-/tmp/herdr-tabmap}"
MAP_MAX_AGE=15          # seconds; older than this and we re-resolve
LOG=/tmp/herdr-focus.log

log() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LOG"; }

# Raise the terminal first: it is the half that always works, even with the
# devserver unreachable.
open -a Ghostty 2>/dev/null

tab_id=""
if [ -f "$TABMAP" ]; then
    age=$(( $(date +%s) - $(stat -f %m "$TABMAP" 2>/dev/null || echo 0) ))
    if [ "$age" -le "$MAP_MAX_AGE" ]; then
        tab_id=$(awk -v k="${WORKSPACE_NUMBER}:${TAB_NUMBER}" '$1==k{print $2; exit}' "$TABMAP")
    fi
fi

if [ -n "$tab_id" ]; then
    "$HERDR" tab focus "$tab_id" >/dev/null 2>&1
    log "ws$WORKSPACE_NUMBER tab$TAB_NUMBER -> $tab_id (cached)"
    exit 0
fi

# Fallback: resolve over the socket (three extra round trips).
ws_id=$("$HERDR" workspace list 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
rows = d if isinstance(d,list) else (d.get('result',d).get('workspaces') or d.get('result',d))
if not isinstance(rows,list): sys.exit(1)
n=int('$WORKSPACE_NUMBER')
m=[w for w in rows if w.get('number')==n] or rows
print(m[0]['workspace_id'] if m else '')
" 2>/dev/null)

if [ -z "$ws_id" ]; then
    log "ws$WORKSPACE_NUMBER tab$TAB_NUMBER: herdr unreachable; raised Ghostty only"
    exit 0
fi

tab_id=$("$HERDR" tab list --workspace "$ws_id" 2>/dev/null | python3 -c "
import json,sys,re
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
rows = d if isinstance(d,list) else (d.get('result',d).get('tabs') or d.get('result',d))
if not isinstance(rows,list): sys.exit(1)
n=int('$TAB_NUMBER')
# TabInfo.number is an internal id (a tab shown as '1' reports 264); the
# visible number is the integer the label starts with, e.g. '1 o st'.
m=[t for t in rows if (lambda x: x and int(x.group(1))==n)(re.match(r'\s*(\d+)', t.get('label') or ''))]
print(m[0]['tab_id'] if m else '')
" 2>/dev/null)

if [ -n "$tab_id" ]; then
    "$HERDR" tab focus "$tab_id" >/dev/null 2>&1
    log "ws$WORKSPACE_NUMBER tab$TAB_NUMBER -> $tab_id (resolved)"
else
    "$HERDR" workspace focus "$ws_id" >/dev/null 2>&1
    log "ws$WORKSPACE_NUMBER tab$TAB_NUMBER missing; focused workspace only"
fi
