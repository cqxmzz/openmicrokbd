#!/usr/bin/env python3
"""Say which slot a physical key is, and what it is currently bound to.

The row/slot numbering used so far came from reading the app's source, not
from the hardware. This reads the firmware's live input stream instead, so
pressing a key tells you its slot number for certain.

    launchctl bootout gui/$(id -u)/com.qimche.herdr-led    # free the pad
    ~/.config/openmicro/.venv/bin/python ~/.config/openmicro/padwatch.py
    # press keys; Ctrl-C when done
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.qimche.herdr-led.plist

Event reports are [0x80, src, a, b]: src 0 = key (a = position, b = pressed),
1 = encoder rotate (a=1 CW), 2 = encoder button, 3 = joystick, 4 = touch tap.
"""
import json
import os
import sys

import hid

VID, PID = 0x1209, 0x0001
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
EVENT_REPORT = 0x80

CFG = os.path.expanduser("~/Library/Application Support/OpenMicro/config.json")
ROW = {0: 1, 1: 1, 2: 2, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 9: 3, 10: 4, 11: 4, 12: 4}
KEYNAME = {40: "Enter", 42: "Backspace", 41: "Escape", 43: "Tab"}
MODNAME = {0: "", 1: "Ctrl+", 2: "Shift+", 4: "Alt+", 8: "Cmd+"}
SRC = {1: "encoder rotate", 2: "encoder press", 3: "joystick", 4: "touch tap"}


def binding(slot):
    try:
        d = json.load(open(CFG))
        inp = d["profiles"][d["active_profile"]]["inputs"][slot]
    except Exception:
        return "?"
    e = inp.get("emitted", {})
    if e.get("kind") == "keyboard":
        c = e.get("code", 0)
        name = f"F{c - 91}" if 104 <= c <= 111 else KEYNAME.get(c, hex(c))
        return f"{inp.get('label','')} = {MODNAME.get(e.get('mods', 0), '?')}{name}"
    if e.get("kind") == "consumer":
        return f"{inp.get('label','')} = consumer 0x{e.get('code', 0):02X}"
    return f"{inp.get('label','')} = none"


def main():
    path = next((d["path"] for d in hid.enumerate(VID, PID)
                 if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE), None)
    if path is None:
        sys.exit("no OpenMicro raw interface found")
    dev = hid.device()
    try:
        dev.open_path(path)
    except OSError as e:
        sys.exit(f"cannot open the pad ({e}).\n"
                 "Stop the LED daemon and quit the OpenMicro app -- both hold it:\n"
                 "  launchctl bootout gui/$(id -u)/com.qimche.herdr-led")
    print("press keys on the pad (Ctrl-C to stop)\n")
    print(f"  {'slot':<6} {'row':<5} {'position in row':<16} binding")
    try:
        while True:
            r = dev.read(32, timeout_ms=1000)
            if not r or r[0] != EVENT_REPORT:
                continue
            src, a, b = r[1], r[2], r[3]
            if src == 0:
                if not b:          # report on press only
                    continue
                row = ROW.get(a, "?")
                pos = sum(1 for s, rr in ROW.items() if rr == row and s <= a)
                print(f"  p{a:<5} {row:<5} {pos:<16} {binding(a)}")
            elif b or src == 1:
                print(f"  {'-':<6} {'-':<5} {'-':<16} {SRC.get(src, src)} (a={a})")
    except KeyboardInterrupt:
        print()
    finally:
        dev.close()


if __name__ == "__main__":
    main()
