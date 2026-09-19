// volstep-daemon — resident fine-grained volume stepper.
//
// Replaces the per-press `volstep.py` on the hot path. Karabiner's
// `shell_command` cannot keep up with a rotary encoder: measured ~5-6/s before
// it starts dropping, while a fast knob spin is 15-20 notches/s. Proven by
// bypassing Karabiner entirely (ignore=true), where every notch landed. Same
// reasoning as swptt-daemon: a resident process pays the start-up cost once.
//
// Karabiner no longer runs a shell_command for volume. It maps the media keys
// to two keycodes nothing on a US layout can produce, and this daemon watches
// them with a passive tap:
//
//     volume_increment -> international3 (JIS Yen,        kVK 93) -> louder
//     volume_decrement -> international1 (JIS underscore, kVK 94) -> quieter
//
// The stepping rules are volstep.py's, unchanged: 2 dB additive where the
// device reports a trustworthy dB control, the same size multiplicatively
// otherwise, and the bottom of the ladder mutes rather than writing a zero a
// Bluetooth sink would bounce back.
//
// Build: swiftc -O volstep-daemon.swift -o volstep-daemon
//        codesign -s - --identifier com.qimche.volstep-daemon --force volstep-daemon
//
// Needs Accessibility (installs an event tap).

import Foundation
import CoreAudio
import CoreGraphics

let KEY_UP: CGKeyCode = 93     // international3
let KEY_DOWN: CGKeyCode = 94   // international1

let STEP_DB = 2.0
let FACTOR = pow(10.0, STEP_DB / 20.0)
// 1/127 is AVRCP absolute volume's smallest representable step. A Bluetooth
// sink snaps anything quieter back up to it, so below this we mute instead.
let FLOOR = 1.0 / 127.0
let LOGPATH = "/Users/qimche/.config/karabiner/volstep-daemon.log"
// Within a burst, chain off our own last intended value: a BT sink
// acknowledges a write asynchronously (~28ms measured) so the readback lags
// and consecutive notches would otherwise all read the same level.
let STATE_TTL = 1.0

let stamp: DateFormatter = {
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss.SSS"; return f
}()

func log(_ msg: String) {
    let line = "[\(stamp.string(from: Date()))] \(msg)\n"
    if let fh = FileHandle(forWritingAtPath: LOGPATH) {
        fh.seekToEndOfFile(); fh.write(Data(line.utf8)); try? fh.close()
    } else {
        try? line.write(toFile: LOGPATH, atomically: true, encoding: .utf8)
    }
}

// --- CoreAudio ---------------------------------------------------------------

func addr(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope,
          _ elem: AudioObjectPropertyElement) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: elem)
}

func defaultOutput() -> AudioObjectID? {
    var a = addr(kAudioHardwarePropertyDefaultOutputDevice,
                 kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    var dev = AudioObjectID(0)
    var sz = UInt32(MemoryLayout<AudioObjectID>.size)
    let r = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                       &a, 0, nil, &sz, &dev)
    return r == noErr ? dev : nil
}

func getFloat(_ dev: AudioObjectID, _ sel: AudioObjectPropertySelector,
              _ elem: AudioObjectPropertyElement) -> Float32? {
    var a = addr(sel, kAudioDevicePropertyScopeOutput, elem)
    var v: Float32 = 0
    var sz = UInt32(MemoryLayout<Float32>.size)
    return AudioObjectGetPropertyData(dev, &a, 0, nil, &sz, &v) == noErr ? v : nil
}

@discardableResult
func setFloat(_ dev: AudioObjectID, _ sel: AudioObjectPropertySelector,
              _ elem: AudioObjectPropertyElement, _ value: Float32) -> OSStatus {
    var a = addr(sel, kAudioDevicePropertyScopeOutput, elem)
    var v = value
    return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &v)
}

func getMute(_ dev: AudioObjectID) -> Bool? {
    var a = addr(kAudioDevicePropertyMute, kAudioDevicePropertyScopeOutput, 0)
    var v: UInt32 = 0
    var sz = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(dev, &a, 0, nil, &sz, &v) == noErr ? (v != 0) : nil
}

@discardableResult
func setMute(_ dev: AudioObjectID, _ on: Bool) -> OSStatus {
    var a = addr(kAudioDevicePropertyMute, kAudioDevicePropertyScopeOutput, 0)
    var v: UInt32 = on ? 1 : 0
    return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<UInt32>.size), &v)
}

func sane(_ x: Float32?, _ lo: Double, _ hi: Double) -> Bool {
    guard let x = x else { return false }
    let d = Double(x)
    return d.isFinite && d >= lo && d <= hi
}

/// Elements whose current value is readable AND sane (volstep.py's rule: a
/// WH-1000XM2 reports garbage dB, so every reading is checked before use).
func usable(_ dev: AudioObjectID, _ sel: AudioObjectPropertySelector,
            _ lo: Double, _ hi: Double) -> [AudioObjectPropertyElement] {
    let els: [AudioObjectPropertyElement] = [1, 2].filter { sane(getFloat(dev, sel, $0), lo, hi) }
    if !els.isEmpty { return els }
    return sane(getFloat(dev, sel, 0), lo, hi) ? [0] : []
}

func dbRange(_ dev: AudioObjectID, _ elem: AudioObjectPropertyElement) -> (Double, Double)? {
    var a = addr(kAudioDevicePropertyVolumeRangeDecibels, kAudioDevicePropertyScopeOutput, elem)
    var r = AudioValueRange()
    var sz = UInt32(MemoryLayout<AudioValueRange>.size)
    guard AudioObjectGetPropertyData(dev, &a, 0, nil, &sz, &r) == noErr else { return nil }
    let lo = r.mMinimum, hi = r.mMaximum
    guard lo.isFinite, hi.isFinite, lo >= -200, hi <= 40, lo < hi else { return nil }
    return (lo, hi)
}

// --- stepping ----------------------------------------------------------------

var cacheDev: AudioObjectID = 0
var cacheKind = ""
var cacheValue = 0.0
var cacheAt = 0.0

func cached(_ dev: AudioObjectID, _ kind: String, _ live: Double) -> Double {
    let now = ProcessInfo.processInfo.systemUptime
    if cacheDev == dev, cacheKind == kind, now - cacheAt < STATE_TTL { return cacheValue }
    return live
}

func remember(_ dev: AudioObjectID, _ kind: String, _ value: Double) {
    cacheDev = dev; cacheKind = kind; cacheValue = value
    cacheAt = ProcessInfo.processInfo.systemUptime
}

func forget() { cacheDev = 0; cacheKind = "" }

func step(_ sign: Double) {
    guard let dev = defaultOutput() else { log("no default output device"); return }

    // Silence is the mute property, not a volume of zero.
    if let muted = getMute(dev) {
        if sign > 0 && muted { setMute(dev, false); forget(); log("unmuted"); return }
        if sign < 0 && muted { return }
    }
    let canMute = getMute(dev) != nil

    // dB path, when the device reports a trustworthy dB control.
    let dbEls = usable(dev, kAudioDevicePropertyVolumeDecibels, -200, 40)
    if let first = dbEls.first, let (lo, hi) = dbRange(dev, first) {
        let live = Double(getFloat(dev, kAudioDevicePropertyVolumeDecibels, first) ?? 0)
        var cur = cached(dev, "db", live)
        if !(cur.isFinite && cur >= lo && cur <= hi) { cur = live }
        if sign < 0 && canMute && cur <= lo + 1e-6 {
            setMute(dev, true); forget(); log(String(format: "muted at the %.0f dB floor", lo))
            return
        }
        var new = min(hi, max(lo, cur + STEP_DB * sign))
        new = sign < 0 ? min(new, cur) : min(new, cur + STEP_DB)   // hard invariant
        for e in dbEls { setFloat(dev, kAudioDevicePropertyVolumeDecibels, e, Float32(new)) }
        remember(dev, "db", new)
        log(String(format: "dB %.1f -> %.1f", cur, new))
        return
    }

    // Scalar fallback.
    let els = usable(dev, kAudioDevicePropertyVolumeScalar, 0.0, 1.0)
    guard let first = els.first else { log("no sane volume reading"); return }
    let live = Double(getFloat(dev, kAudioDevicePropertyVolumeScalar, first) ?? 0)
    var cur = cached(dev, "scalar", live)
    if !(cur.isFinite && cur >= 0 && cur <= 1) { cur = live }

    var new: Double
    if sign < 0 {
        var nxt = cur / FACTOR
        // Land on the floor before muting, so the quietest audible level stays
        // selectable on the way down.
        if nxt < FLOOR && cur > FLOOR * 1.0001 { nxt = FLOOR }
        if nxt < FLOOR {
            if canMute {
                // Mute only. Writing the level too would have its async reply
                // clear the mute we just set.
                setMute(dev, true); forget()
                log(String(format: "muted (next %.6f below floor %.6f)", nxt, FLOOR))
                return
            }
            nxt = 0.0
        }
        new = min(nxt, cur)                                        // hard invariant
    } else {
        new = max(cur * FACTOR, FLOOR)
        new = min(new, cur * FACTOR + 1e-6, 1.0)                   // hard invariant
    }
    new = min(1.0, max(0.0, new))
    for e in els { setFloat(dev, kAudioDevicePropertyVolumeScalar, e, Float32(new)) }
    remember(dev, "scalar", new)
    log(String(format: "scalar %.6f -> %.6f", cur, new))
}

// --- event tap ---------------------------------------------------------------

var gTap: CFMachPort?

let callback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let t = gTap { CGEvent.tapEnable(tap: t, enable: true); log("tap was disabled — re-enabled") }
        return nil
    }
    if type == .keyDown {
        let kc = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
        if kc == KEY_UP { step(1) } else if kc == KEY_DOWN { step(-1) }
    }
    return Unmanaged.passUnretained(event)
}

func installTap() -> Bool {
    let mask = (1 << CGEventType.keyDown.rawValue)
    guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                      place: .headInsertEventTap,
                                      options: .listenOnly,      // passive: never swallows a key
                                      eventsOfInterest: CGEventMask(mask),
                                      callback: callback,
                                      userInfo: nil) else { return false }
    gTap = tap
    let rls = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetMain(), rls, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
    return true
}

_ = defaultOutput()   // warm CoreAudio so the first notch pays nothing

log("--- daemon start (pid \(getpid()), up=\(KEY_UP) down=\(KEY_DOWN))")

if !installTap() {
    log("event tap refused — grant Accessibility to volstep-daemon; retrying every 5s")
    var timer: DispatchSourceTimer? = DispatchSource.makeTimerSource(queue: .main)
    timer?.schedule(deadline: .now() + 5, repeating: 5)
    timer?.setEventHandler {
        if installTap() { log("event tap installed"); timer?.cancel(); timer = nil }
    }
    timer?.resume()
} else {
    log("event tap installed")
}

CFRunLoopRun()
