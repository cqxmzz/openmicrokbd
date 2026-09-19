#!/usr/bin/env python3
"""Watch the default output device's volume and print a line on every change.

    volwatch.py            poll the default output, log changes
    volwatch.py --all      also log when the default device itself changes
    VOLWATCH_HZ=50 ...     poll rate (default 40 Hz)

Prints old -> new with the absolute delta and the ratio, so a multiplicative
step (volstep's 2 dB = x1.2589) is distinguishable from an additive one at a
glance, and a change counter for counting events per encoder notch.

Reads the same CoreAudio properties volstep.py writes, so what you see here is
what volstep sees. Ctrl-C to stop.
"""
import ctypes, ctypes.util, struct, sys, os, time, datetime

HZ = float(os.environ.get('VOLWATCH_HZ') or 40)
SHOW_DEV = '--all' in sys.argv

ca = ctypes.CDLL(ctypes.util.find_library('CoreAudio'))
cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
cf.CFStringGetCStringPtr.restype = ctypes.c_char_p


def fcc(s): return struct.unpack('>I', s.encode())[0]


class Addr(ctypes.Structure):
    _fields_ = [('sel', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('elem', ctypes.c_uint32)]


SYS = 1
GLOB, OUT = fcc('glob'), fcc('outp')
DOUT, VOLD, VOLM, VDBR, NAME, MUTED = (fcc('dOut'), fcc('vold'), fcc('volm'),
                                       fcc('vdb#'), fcc('lnam'), fcc('mute'))


def get_mute(dev):
    a = Addr(MUTED, OUT, 0); sz = ctypes.c_uint32(4); v = ctypes.c_uint32()
    r = ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                      ctypes.byref(sz), ctypes.byref(v))
    return None if r else bool(v.value)


def sane(x, lo, hi):
    return x is not None and x == x and -1e30 < x < 1e30 and lo <= x <= hi


def default_output():
    a = Addr(DOUT, GLOB, 0); sz = ctypes.c_uint32(4); d = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyData(ctypes.c_uint32(SYS), ctypes.byref(a), 0, None,
                                     ctypes.byref(sz), ctypes.byref(d)):
        return None
    return d.value


def name_of(dev):
    a = Addr(NAME, GLOB, 0); sz = ctypes.c_uint32(8); s = ctypes.c_void_p()
    if ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                     ctypes.byref(sz), ctypes.byref(s)):
        return '?'
    p = cf.CFStringGetCStringPtr(s, 0x08000100)
    return p.decode() if p else '?'


def getf(dev, sel, elem):
    a = Addr(sel, OUT, elem); sz = ctypes.c_uint32(4); v = ctypes.c_float()
    r = ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                      ctypes.byref(sz), ctypes.byref(v))
    return None if r else v.value


def read(dev):
    """Scalar plus dB when the device reports a trustworthy one (volstep's rule)."""
    for e in (1, 2, 0):
        s = getf(dev, VOLM, e)
        if sane(s, 0.0, 1.0):
            d = getf(dev, VOLD, e)
            return s, (d if sane(d, -200, 40) else None)
    return None, None


def bar(x, w=24):
    n = int(round(x * w))
    return '#' * n + '.' * (w - n)


def main():
    dev = default_output()
    if dev is None:
        sys.exit('no default output device')
    last_dev = dev
    last, lastdb = read(dev)
    lastmute = get_mute(dev)
    print(f"watching {name_of(dev)} (dev {dev}) at {HZ:.0f} Hz -- Ctrl-C to stop")
    print(f"  start  scalar {last:.6f}  [{bar(last)}] {last*100:5.1f}%"
          + (f"  {lastdb:+.1f} dB" if lastdb is not None else "  (no sane dB -> scalar path)"))
    n = 0
    t0 = None
    while True:
        time.sleep(1.0 / HZ)
        dev = default_output()
        if dev is None:
            continue
        if dev != last_dev:
            if SHOW_DEV:
                print(f"-- default output changed to {name_of(dev)} (dev {dev})")
            last_dev = dev
            last, lastdb = read(dev)
            lastmute = get_mute(dev)
            continue
        cur, curdb = read(dev)
        mute = get_mute(dev)
        if mute != lastmute:
            n += 1
            stamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
            print(f"[{stamp}] #{n:<4d} {'MUTED' if mute else 'UNMUTED'}"
                  f"  (scalar {cur:.6f} unchanged)")
            lastmute = mute
        if cur is None or last is None or abs(cur - last) < 1e-9:
            continue
        n += 1
        now = time.time()
        gap = '' if t0 is None else f"  +{(now - t0) * 1000:6.0f}ms"
        t0 = now
        ratio = (cur / last) if last > 0 else float('inf')
        stamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
        db = f"  {curdb:+.1f} dB" if curdb is not None else ""
        print(f"[{stamp}] #{n:<4d} {last:.6f} -> {cur:.6f}  "
              f"d={cur-last:+.6f}  x{ratio:.4f}  [{bar(cur)}] {cur*100:5.1f}%{db}{gap}")
        last, lastdb = cur, curdb


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print()
