#!/usr/bin/env python3
"""Push config.json to the pad's flash over raw HID, without the OpenMicro app.

The app turned out not to write the keymap on launch, so edits to config.json
looked correct on disk and changed nothing on the hardware. It also seizes the
pad exclusively, locking out the LED daemon. This does the write directly and
keeps the app out of the loop entirely.

    launchctl bootout gui/$(id -u)/com.qimche.herdr-led    # free the pad
    ~/.config/openmicro/.venv/bin/python ~/.config/openmicro/writemap.py
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.qimche.herdr-led.plist

    --dry-run   show what would be written, touch nothing

Wire format (fw/src/main.rs): slots are 4 bytes [kind, mods, code_lo, code_hi]
with kind 0 none / 1 keyboard / 2 consumer, sent 7 per page via SET_KEYMAP,
then SAVE commits to flash.
"""
import json
import os
import sys

import hid

VID, PID = 0x1209, 0x0001
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61

CMD_SET_KEYMAP = 0x04
CMD_SAVE = 0x05
CMD_SET_ANALOG = 0x08
CMD_SET_JOYMODE = 0x0A
CMD_SET_LED = 0x0C
CMD_SET_LEDPATTERN = 0x0E
SAVE_KEY = b"SAVE"

PAGE_SLOTS = 7
SLOT_COUNT = 24
KIND = {"none": 0, "keyboard": 1, "consumer": 2}
JOY_MODE = {"keys": 0, "mouse": 1, "grade": 2}
LED_MODE = {"rainbow": 0, "solid": 1, "white": 1}

CFG = os.path.expanduser("~/Library/Application Support/OpenMicro/config.json")
DRY = "--dry-run" in sys.argv


def slot_bytes(inp):
    e = (inp or {}).get("emitted") or {}
    kind = KIND.get(e.get("kind"), 0)
    code = int(e.get("code", 0)) if kind else 0
    mods = int(e.get("mods", 0)) if kind else 0
    return bytes([kind, mods, code & 0xFF, (code >> 8) & 0xFF])


def led_bytes(pattern):
    p = pattern or {}
    mode = LED_MODE.get(p.get("mode"), 0)
    if p.get("mode") == "white":
        return bytes([mode, 255, 255, 255])
    return bytes([mode, p.get("r", 0), p.get("g", 0), p.get("b", 0)])


def send(dev, payload, label):
    if DRY:
        print(f"  would send {label}: {payload[:10].hex(' ')}")
        return
    dev.write(b"\x00" + bytes(payload) + b"\x00" * (32 - len(payload)))
    reply = dev.read(32, timeout_ms=2000)
    ok = bool(reply) and reply[0] == payload[0] and reply[1] != 0
    print(f"  {label:<28} {'ok' if ok else 'FAILED ' + str(reply[:4] if reply else 'no reply')}")
    return ok


def main():
    d = json.load(open(CFG))
    prof = d["profiles"][d["active_profile"]]
    inputs = prof["inputs"]
    analog = prof.get("analog", {})
    print(f"profile {prof.get('name')!r}: {len(inputs)} slots")

    path = next((x["path"] for x in hid.enumerate(VID, PID)
                 if x["usage_page"] == RAW_USAGE_PAGE and x["usage"] == RAW_USAGE), None)
    if path is None:
        sys.exit("no OpenMicro raw interface found")
    dev = None
    if not DRY:
        dev = hid.device()
        try:
            dev.open_path(path)
        except OSError as e:
            sys.exit(f"cannot open the pad ({e}).\n"
                     "Quit the OpenMicro app and stop the LED daemon -- both seize it:\n"
                     "  launchctl bootout gui/$(id -u)/com.qimche.herdr-led")

    for page in range((SLOT_COUNT + PAGE_SLOTS - 1) // PAGE_SLOTS):
        start = page * PAGE_SLOTS
        count = min(PAGE_SLOTS, SLOT_COUNT - start)
        data = b"".join(slot_bytes(inputs[start + i] if start + i < len(inputs) else None)
                        for i in range(count))
        send(dev, bytes([CMD_SET_KEYMAP, page, count]) + data,
             f"keymap page {page} (slots {start}-{start + count - 1})")

    thr = int(analog.get("joy_threshold", 1024))
    send(dev, bytes([CMD_SET_ANALOG, thr & 0xFF, (thr >> 8) & 0xFF]), f"joy threshold {thr}")
    send(dev, bytes([CMD_SET_JOYMODE, JOY_MODE.get(analog.get("joy_mode"), 0),
                     int(analog.get("joy_mouse_speed", 5))]), f"joy mode {analog.get('joy_mode')}")
    send(dev, bytes([CMD_SET_LED, int(d.get("led_brightness", 255))]),
         f"brightness {d.get('led_brightness')}")
    send(dev, bytes([CMD_SET_LEDPATTERN]) + led_bytes(d.get("led_key_pattern"))
         + led_bytes(d.get("led_ambient_pattern")), "led patterns")

    send(dev, bytes([CMD_SAVE]) + SAVE_KEY, "SAVE to flash")
    if dev:
        dev.close()
    print("\ndone -- unplug/replug to confirm it persisted")


if __name__ == "__main__":
    main()
