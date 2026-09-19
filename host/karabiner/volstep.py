#!/usr/bin/env python3
"""Nudge the default output device's volume by one step.

    volstep.py  1    louder
    volstep.py -1    quieter

Steps in decibels when the device reports a trustworthy dB control, otherwise
in proportional scalar steps of the same size.

SAFETY: some devices (e.g. Bluetooth headsets) report garbage for
kAudioDevicePropertyVolumeDecibels -- a WH-1000XM2 here returns 2.66e20.
Clamping that into a valid range yields the range MAXIMUM, which slams the
volume to full. So every reading is sanity-checked, and there is a hard
invariant below: a decrement can never raise the level, and an increment can
never raise it by more than one step. If nothing sane can be read, do nothing.
"""
import ctypes, ctypes.util, struct, sys, datetime, fcntl, time, os

# Karabiner spawns one of these per detent and they overlap: a fast knob spin
# had two processes read the same level, compute the same next value and write
# it, so several notches produced one step. LOCK makes the read-modify-write
# atomic; STATE fixes the other half, because a Bluetooth sink acknowledges a
# write asynchronously (~28ms measured) and even serialized presses would keep
# reading the pre-write value. Inside a burst we chain off our own last
# intended value; once it goes stale we resync to the device, so changing the
# volume by any other means still works.
LOCK_PATH = '/tmp/volstep.lock'
STATE_PATH = '/tmp/volstep.state'
STATE_TTL = 1.0                       # seconds


def cached(dev, kind, live):
    try:
        t, d, k, v = open(STATE_PATH).read().split()
        if time.time() - float(t) < STATE_TTL and int(d) == dev and k == kind:
            return float(v)
    except Exception:
        pass
    return live


def remember(dev, kind, value):
    try:
        with open(STATE_PATH, 'w') as f:
            f.write('%f %d %s %.9f' % (time.time(), dev, kind, value))
    except Exception:
        pass


def forget():
    try:
        os.unlink(STATE_PATH)
    except Exception:
        pass

STEP_DB = 2.0
FACTOR  = 10 ** (STEP_DB / 20.0)      # same step size, multiplicative
# Scalar below this counts as silence. 1/127 is AVRCP absolute volume's
# smallest representable step: a Bluetooth sink (WH-1000XM2 here) snaps
# anything quieter straight back up to it, so a multiplicative decrement below
# 1/127 is a fixed point -- it writes 0.006255, the headset returns 0.007874,
# and the level never moves however many times you press. Terminating the
# ladder here makes one press reach real silence, and one press back out of it
# land on the quietest level the device can actually produce.
FLOOR   = 1.0 / 127

ca = ctypes.CDLL(ctypes.util.find_library('CoreAudio'))
def fcc(s): return struct.unpack('>I', s.encode())[0]
class Addr(ctypes.Structure):
    _fields_ = [('sel', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('elem', ctypes.c_uint32)]
SYS = 1
GLOB, OUT = fcc('glob'), fcc('outp')
DOUT, VOLD, VOLM, VDBR, MUTED = fcc('dOut'), fcc('vold'), fcc('volm'), fcc('vdb#'), fcc('mute')

def sane(x, lo, hi):
    return x is not None and x == x and -1e30 < x < 1e30 and lo <= x <= hi

def getf(obj, sel, elem):
    a = Addr(sel, OUT, elem); sz = ctypes.c_uint32(4); v = ctypes.c_float()
    r = ca.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(a), 0, None,
                                      ctypes.byref(sz), ctypes.byref(v))
    return None if r else v.value

def setf(obj, sel, elem, val):
    a = Addr(sel, OUT, elem); v = ctypes.c_float(val)
    return ca.AudioObjectSetPropertyData(ctypes.c_uint32(obj), ctypes.byref(a), 0, None,
                                         ctypes.c_uint32(4), ctypes.byref(v))

def get_mute(dev):
    """None when the device has no master mute (then we just clamp as before)."""
    a = Addr(MUTED, OUT, 0); sz = ctypes.c_uint32(4); v = ctypes.c_uint32()
    r = ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                      ctypes.byref(sz), ctypes.byref(v))
    return None if r else bool(v.value)


def set_mute(dev, on):
    a = Addr(MUTED, OUT, 0); v = ctypes.c_uint32(1 if on else 0)
    return ca.AudioObjectSetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                         ctypes.c_uint32(4), ctypes.byref(v))


def db_range(dev, elem):
    a = Addr(VDBR, OUT, elem); sz = ctypes.c_uint32(16); b = (ctypes.c_char * 16)()
    if ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev), ctypes.byref(a), 0, None,
                                     ctypes.byref(sz), b) or sz.value != 16:
        return None
    lo, hi = struct.unpack('<dd', bytes(b))
    if not (sane(lo, -200, 40) and sane(hi, -200, 40) and lo < hi):
        return None
    return lo, hi

def default_output():
    a = Addr(DOUT, GLOB, 0); sz = ctypes.c_uint32(4); d = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyData(ctypes.c_uint32(SYS), ctypes.byref(a), 0, None,
                                     ctypes.byref(sz), ctypes.byref(d)):
        return None
    return d.value

def usable(dev, sel, lo, hi):
    """Elements whose current value is readable AND sane."""
    els = [e for e in (1, 2) if sane(getf(dev, sel, e), lo, hi)]
    if els:
        return els
    return [0] if sane(getf(dev, sel, 0), lo, hi) else []

def run(sign):
    dev = default_output()
    if dev is None:
        return 'no default output device'

    # Silence is the mute property, not a volume of zero. A Bluetooth sink
    # snaps scalar 0 back to its smallest step within ~30ms (measured on the
    # WH-1000XM2: 0.0 -> 0.007874 after 28ms), so the bottom of the ladder
    # mutes instead, and a press upward lifts mute before anything else.
    muted = get_mute(dev)
    if muted is not None:
        if sign > 0 and muted:
            set_mute(dev, False)
            return 'dev%d unmuted' % dev
        if sign < 0 and muted:
            return 'dev%d already muted' % dev

    els = usable(dev, VOLD, -200, 40)
    rng = db_range(dev, els[0]) if els else None
    if els and rng:
        lo, hi = rng
        cur = cached(dev, 'db', getf(dev, VOLD, els[0]))
        if not sane(cur, lo, hi):
            cur = getf(dev, VOLD, els[0])
        if sign < 0 and muted is not None and cur <= lo + 1e-6:
            set_mute(dev, True)
            forget()
            return 'dev%d muted at the %.0f dB floor' % (dev, lo)
        new = min(hi, max(lo, cur + STEP_DB * sign))
        new = min(new, cur) if sign < 0 else min(new, cur + STEP_DB)   # hard invariant
        for e in els:
            setf(dev, VOLD, e, new)
        remember(dev, 'db', new)
        return 'dev%d dB %.1f -> %.1f (range %.0f..%.0f)' % (dev, cur, new, lo, hi)

    els = usable(dev, VOLM, 0.0, 1.0)
    if els:
        cur = cached(dev, 'scalar', getf(dev, VOLM, els[0]))
        if not sane(cur, 0.0, 1.0):
            cur = getf(dev, VOLM, els[0])
        if sign < 0:
            nxt = cur / FACTOR
            if nxt < FLOOR and cur > FLOOR * 1.0001:
                # Land on the floor before muting. Without this the last step
                # overshoots silence and the quietest audible level -- the one
                # you actually want when something is playing quietly -- can
                # never be selected on the way down.
                nxt = FLOOR
            if nxt < FLOOR:
                if muted is not None:
                    # Mute only -- do NOT write the volume too. A Bluetooth
                    # sink answers a volume write asynchronously (~28ms) and
                    # that late reply clears the mute we just set. Leaving the
                    # level alone also means unmute returns where you were.
                    set_mute(dev, True)
                    forget()
                    return 'dev%d muted (next %.6f below floor %.6f)' % (dev, nxt, FLOOR)
                new = 0.0
            else:
                new = nxt
            new = min(new, cur)                                        # hard invariant
        else:
            new = max(cur * FACTOR, FLOOR)
            new = min(new, cur * FACTOR + 1e-6, 1.0)                   # hard invariant
        new = min(1.0, max(0.0, new))
        for e in els:
            setf(dev, VOLM, e, new)
        remember(dev, 'scalar', new)
        return 'dev%d scalar %.6f -> %.6f' % (dev, cur, new)

    return 'dev%d REFUSED: no sane volume reading (dB=%s scalar=%s)' % (
        dev, getf(dev, VOLD, 1), getf(dev, VOLM, 1))

sign = -1.0 if str(sys.argv[1] if len(sys.argv) > 1 else '1').startswith('-') else 1.0

# Serialise the read-modify-write. Python start-up (~60ms) stays parallel
# across the burst -- only the CoreAudio critical section queues, which is a
# couple of ms -- so a fast spin applies every notch instead of collapsing.
_lock = open(LOCK_PATH, 'w')
fcntl.flock(_lock, fcntl.LOCK_EX)
try:
    msg = run(sign)
finally:
    fcntl.flock(_lock, fcntl.LOCK_UN)
    _lock.close()

with open('/tmp/volstep.log', 'a') as f:
    f.write('%s  %-3s %s\n' % (datetime.datetime.now().strftime('%H:%M:%S'),
                               sys.argv[1] if len(sys.argv) > 1 else '1', msg))
