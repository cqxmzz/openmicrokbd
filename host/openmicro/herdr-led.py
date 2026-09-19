#!/usr/bin/env python3
"""Drive the OpenMicro's second-row key LEDs from herdr agent status.

Pad slots mirror herdr agent status (see SLOT_MAP):

    row 2      slots 2,3,4,5  ->  workspace 1, tabs 1-4 (by position)
    row 3 a,b  slots 6,7      ->  workspace 2, tabs 1-2 (by position)

    no agent / idle / unknown  ->  off
    working                    ->  blue
    blocked                    ->  red   (the agent is waiting on you)
    done                       ->  green

Only pure red/green/blue are used: the per-key LED has three dies a millimetre
apart under the cap, so a mixed colour shows as separate dots rather than one
light.

herdr runs on the devserver, so its socket has to be tunnelled to this Mac --
see README-herdr-led.md. We shell out to the herdr CLI rather than speaking
its socket protocol directly: the CLI is herdr's own client, so the framing
and the schema can never drift out from under us.

The pad is written with the firmware's per-key override command,
`[0x0F, index, enable, r, g, b]`, on the raw HID interface. "Off" is an
explicit black override rather than releasing the override, because releasing
would hand the key back to the idle pattern (white).

    ./herdr-led.py                 poll the default socket path
    HERDR_SOCK=/tmp/x ./herdr-led.py
    POLL_MS=500 ./herdr-led.py
"""
import json
import os
import re
import subprocess
import sys
import time

import hid

VID, PID = 0x1209, 0x0001
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
CMD_SET_KEY_LED_OVERRIDE = 0x0F

# (workspace number, tab number) -> pad slot. Rows are 0-1 / 2-5 / 6-9 / 10-12
# in the app's slot_name, so row 2 is slots 2..5 and the first two of row 3 are
# slots 6,7.
SLOT_MAP = {
    (1, 1): 2, (1, 2): 3, (1, 3): 4, (1, 4): 5,   # row 2  -> workspace 1
    (2, 1): 6, (2, 2): 7,                          # row 3a -> workspace 2
}
ALL_SLOTS = sorted(SLOT_MAP.values())

OFF = (0, 0, 0)
# SK6812MINI-E has three dies ~1mm apart under the keycap, so only a single-die
# colour reads as one light. Anything mixed (yellow was red+green) shows as two
# separate dots. That leaves red/green/blue, and idle is the state that gives
# up its colour -- an idle agent looks the same as an empty tab.
COLOURS = {
    "idle": OFF,
    "working": (0, 0, 255),     # blue
    "blocked": (255, 0, 0),     # red -- needs you
    "done": (0, 255, 0),        # green
    "unknown": OFF,
}

HERDR_SOCK = os.environ.get("HERDR_SOCK") or "/tmp/herdr-remote.sock"
HERDR_BIN = os.environ.get("HERDR_BIN") or "/opt/homebrew/bin/herdr"
TABMAP_PATH = os.environ.get("TABMAP_PATH") or "/tmp/herdr-tabmap"
# One `herdr` invocation per workspace per poll plus two shared ones, so
# keep the cadence modest now that two workspaces are tracked.
POLL = float(os.environ.get("POLL_MS") or 1000) / 1000.0
# Must be comfortably under the firmware's KEY_LED_OVERRIDE_TTL_MS (5s).
HEARTBEAT = float(os.environ.get("HEARTBEAT_MS") or 2000) / 1000.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def herdr(*args):
    """Run a herdr CLI command against the tunnelled socket. None on failure.

    HERDR_SOCKET_PATH is the one the client reads. (HERDR_CLIENT_SOCKET_PATH
    also exists in the binary but is what herdr *exports into panes*; setting
    it is silently ignored and the CLI goes to the default socket.)
    """
    env = dict(os.environ, HERDR_SOCKET_PATH=HERDR_SOCK)
    try:
        p = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True,
                           timeout=4, env=env)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(out, dict) and "error" in out:
        return None
    return out


def rows(out, *keys):
    """herdr returns either a bare list or {result:{<key>:[...]}}; accept both."""
    if out is None:
        return None
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        node = out.get("result", out)
        if isinstance(node, list):
            return node
        for k in keys:
            if isinstance(node.get(k), list):
                return node[k]
    return None


def by_position(tabs):
    """Tabs keyed 1..n by position within the workspace.

    Position, not the number in the label. herdr's Cmd+N switches tabs by
    position inside the current workspace, so the pad has to agree with that
    or a key would light one tab and jump to another. The labels are no guide:
    workspace 2's only tab is labelled "9".
    """
    return {i: t for i, t in enumerate(tabs, start=1)}


def desired_colours():
    """Colour per pad slot, or None when herdr is unreachable."""
    ws = rows(herdr("workspace", "list"), "workspaces")
    if ws is None:
        return None
    by_ws_number = {w.get("number"): w for w in ws}

    # A tab with no agent must stay dark, so confirm presence rather than
    # trusting a tab's aggregated status (a plain shell still has one).
    agents = rows(herdr("agent", "list"), "agents") or []
    tabs_with_agents = {a.get("tab_id") for a in agents}

    out = {slot: OFF for slot in ALL_SLOTS}
    tabs_cache = {}
    resolved = {}
    for (ws_number, tab_number), slot in SLOT_MAP.items():
        workspace = by_ws_number.get(ws_number)
        if workspace is None:
            continue
        wid = workspace.get("workspace_id")
        if wid not in tabs_cache:
            found = rows(herdr("tab", "list", "--workspace", wid), "tabs") or []
            tabs_cache[wid] = by_position(found)
        tab = tabs_cache[wid].get(tab_number)
        if tab is not None:
            resolved[(ws_number, tab_number)] = tab.get("tab_id")
        if tab is not None and tab.get("tab_id") in tabs_with_agents:
            out[slot] = COLOURS.get(tab.get("agent_status"), OFF)
    write_tabmap(resolved)
    return out


def write_tabmap(resolved):
    """Publish <ws>:<tab> -> tab_id for herdr-focus.sh.

    Resolving a tab id costs two round trips over the tunnel (~400ms each),
    which dominated key-press latency. We already have the answer here every
    poll, so hand it over as plain lines -- awk-parseable, so the focus path
    needs no python start-up either.
    """
    try:
        body = "".join(f"{ws}:{tab} {tid}\n" for (ws, tab), tid in sorted(resolved.items()))
        tmp = TABMAP_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, TABMAP_PATH)
    except Exception:
        pass


def screen_locked():
    """True when the login window is up.

    Nothing is blanked explicitly for this: we simply stop refreshing and let
    the firmware's override TTL expire, which is the same path a sleeping or
    unplugged host takes. One mechanism, not three.
    """
    try:
        p = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1",
                            "-k", "CGSSessionScreenIsLocked"],
                           capture_output=True, text=True, timeout=2)
        return "CGSSessionScreenIsLocked\" = Yes" in p.stdout
    except Exception:
        return False


def open_pad():
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE:
            dev = hid.device()
            dev.open_path(d["path"])
            return dev
    return None


def paint(dev, slot, rgb):
    r, g, b = rgb
    report = bytes([CMD_SET_KEY_LED_OVERRIDE, slot, 1, r, g, b])
    dev.write(b"\x00" + report + b"\x00" * (64 - len(report)))


def main():
    log(f"socket={HERDR_SOCK} poll={POLL*1000:.0f}ms map={sorted(SLOT_MAP.items())}")
    dev = None
    shown = {}
    fails = 0
    opening = False
    last_beat = 0.0
    while True:
        try:
            if dev is None:
                opening = True
                dev = open_pad()
                if dev is None:
                    time.sleep(2)
                    continue
                opening = False
                log("pad opened")
                shown = {}
                fails = 0

            if screen_locked():
                # Stop refreshing; the firmware TTL takes the row dark.
                shown = {}
                time.sleep(POLL)
                continue

            want = desired_colours()
            # Unreachable herdr (tunnel down, devserver asleep) must not leave
            # a stale status lit -- go dark rather than lie.
            if want is None:
                want = {slot: OFF for slot in ALL_SLOTS}

            # Re-assert well inside the firmware's override TTL even when
            # nothing changed: the refresh IS the liveness signal, so a host
            # that stops (asleep, unplugged, crashed) expires into darkness.
            now = time.monotonic()
            due = now - last_beat >= HEARTBEAT
            for slot, rgb in want.items():
                if due or shown.get(slot) != rgb:
                    paint(dev, slot, rgb)
                    shown[slot] = rgb
            if due:
                last_beat = now
        except OSError as e:
            fails += 1
            what = "open" if opening else "write"
            log(f"HID {what} failed ({e}); reopening [{fails}]"
                + ("  -- is the OpenMicro app running? it seizes the pad" if opening else ""))
            try:
                dev.close()
            except Exception:
                pass
            dev = None
            if fails >= 5:
                time.sleep(3)
        except Exception as e:  # never let one bad poll kill the daemon
            log(f"error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
