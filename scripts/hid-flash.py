#!/usr/bin/env python3
"""Upload an application image through the resident bootloader's HID protocol.

The companion app only flashes its own bundled image or a GitHub catalog
release, so a locally built firmware needs this. It speaks the protocol in
boot/README.md against the bootloader at 1209:0002 -- the resumable path, which
never rewrites the bootloader itself. An interrupted run leaves the pad in
update mode with the old image still refused; just run it again.

    ./hid-flash.py dist/openmicro-fw-0.10.1.bin

Quit the OpenMicro app first: it watches for the bootloader appearing and will
race this for the device.
"""
import struct
import sys
import time

import hid

APP_VID, APP_PID = 0x1209, 0x0001
BOOT_VID, BOOT_PID = 0x1209, 0x0002
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61
FLASH_BASE = 0x08000000

OP_ENTER_BOOT = 0x12
OP_BOOT_INFO = 0x20
OP_UPDATE_BEGIN = 0x21
OP_UPDATE_DATA = 0x22
OP_UPDATE_END = 0x23
OP_BOOT_RUN = 0x24
ENTER_BOOT_KEY = b"BOOT"

# Header words the bootloader cross-checks against UPDATE_BEGIN.
HDR_LENGTH_OFF, HDR_CRC_OFF = 0xC4, 0xC8

STATUS = {
    0: "ok",
    1: "bad length",
    2: "flash error",
    3: "out of order",
    4: "header invalid / mismatch",
    5: "no BEGIN",
    6: "CRC mismatch",
    0xFF: "unknown opcode",
}


def find(vid, pid):
    for d in hid.enumerate(vid, pid):
        if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE:
            return d["path"]
    return None


def xfer(dev, payload, timeout=2000):
    """One 64-byte report out, one in. Report id 0 is prepended on write."""
    report = bytes(payload) + b"\x00" * (64 - len(payload))
    dev.write(b"\x00" + report)
    reply = dev.read(64, timeout_ms=timeout)
    if not reply:
        raise RuntimeError(f"no reply to opcode 0x{payload[0]:02X} within {timeout} ms")
    if reply[0] != payload[0]:
        raise RuntimeError(f"reply opcode 0x{reply[0]:02X} != request 0x{payload[0]:02X}")
    return bytes(reply)


def enter_bootloader():
    path = find(BOOT_VID, BOOT_PID)
    if path:
        print("pad is already in bootloader mode")
        return path

    path = find(APP_VID, APP_PID)
    if not path:
        raise SystemExit("no OpenMicro found (neither application nor bootloader)")

    print("pad is in application mode; sending ENTER_BOOT")
    dev = hid.device()
    dev.open_path(path)
    try:
        dev.write(b"\x00" + bytes([OP_ENTER_BOOT]) + ENTER_BOOT_KEY + b"\x00" * 59)
    finally:
        dev.close()

    for _ in range(60):  # up to ~15 s for re-enumeration
        time.sleep(0.25)
        path = find(BOOT_VID, BOOT_PID)
        if path:
            print("bootloader enumerated")
            return path
    raise SystemExit("pad did not come back as 1209:0002 -- unplug, hold the encoder, replug")


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    image = open(sys.argv[1], "rb").read()
    print(f"combined image: {len(image)} bytes")

    path = enter_bootloader()
    dev = hid.device()
    dev.open_path(path)
    try:
        info = xfer(dev, [OP_BOOT_INFO], timeout=1000)
        if info[1] != 0:
            raise SystemExit(f"BOOT_INFO status {info[1]}")
        proto = info[2]
        boot_ver = f"{info[3]}.{info[4]}.{info[5]}"
        app_base, app_size = struct.unpack_from("<II", info, 6)
        app_valid = info[14]
        page, max_chunk = struct.unpack_from("<HB", info, 31)
        print(f"bootloader {boot_ver} proto {proto} | slot 0x{app_base:08X} "
              f"size {app_size} | app_valid {app_valid} | page {page} chunk {max_chunk}")

        off = app_base - FLASH_BASE
        if not 0 < off < len(image):
            raise SystemExit(f"app base 0x{app_base:08X} outside the image")
        app = image[off:]
        length, crc = struct.unpack_from("<II", app, HDR_LENGTH_OFF)
        print(f"application slice: {len(app)} bytes at +0x{off:04X}; "
              f"header length {length} crc 0x{crc:08X}")
        if length != len(app):
            raise SystemExit(f"header length {length} != slice {len(app)}")
        if crc == 0:
            raise SystemExit("unstamped image (crc 0) -- build with scripts/build-firmware.sh")
        if length > app_size:
            raise SystemExit(f"image {length} exceeds the {app_size}-byte slot")

        print("erasing...")
        r = xfer(dev, bytes([OP_UPDATE_BEGIN]) + struct.pack("<II", length, crc), timeout=8000)
        if r[1] != 0:
            raise SystemExit(f"UPDATE_BEGIN: {STATUS.get(r[1], r[1])}")

        sent = 0
        while sent < length:
            n = min(max_chunk, length - sent)
            r = xfer(dev, bytes([OP_UPDATE_DATA]) + struct.pack("<IB", sent, n) + app[sent:sent + n])
            if r[1] == 3:  # out of order -- resume where it wants
                sent = struct.unpack_from("<I", r, 2)[0]
                continue
            if r[1] != 0:
                raise SystemExit(f"UPDATE_DATA at {sent}: {STATUS.get(r[1], r[1])}")
            sent = struct.unpack_from("<I", r, 2)[0]
            pct = 100 * sent // length
            print(f"\r  {pct:3d}%  {sent}/{length}", end="", flush=True)
        print()

        r = xfer(dev, [OP_UPDATE_END])
        if r[1] != 0:
            raise SystemExit(f"UPDATE_END: {STATUS.get(r[1], r[1])}")
        print("image verified in flash")

        xfer(dev, [OP_BOOT_RUN], timeout=1000)
        print("booting application")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
