# Host tooling for this pad

Everything here is host-side glue built around one OpenMicroKbd, driving it
from [herdr](https://herdr.dev) agent status and replacing the companion app.
The firmware changes that go with it are in `fw/src/main.rs`; see the repo root
for upstream's own docs.

The short version: **the OpenMicro companion app is not used.** It seizes the
pad's raw HID interface exclusively, so nothing else can drive the LEDs while
it runs, and it turned out not to write the keymap to flash on launch anyway.
`writemap.py` replaces it.

## Layout

```
        +-------------+-------------+-------------+-------------+
row 1   |  [ KNOB ]   | p0          | p1          |  [JOYSTICK] |
        |   volume    |  prev track |  next track |   disabled  |
        +-------------+-------------+-------------+-------------+
row 2   | p2          | p3          | p4          | p5          |
        |  ws1 tab1   |  ws1 tab2   |  ws1 tab3   |  ws1 tab4   |
        +-------------+-------------+-------------+-------------+
row 3   | p6          | p7          | p8          | p9          |
        |  ws2 tab1   |  ws2 tab2   |  delete word|  confirm    |
        +-------------+-------------+-------------+-------------+
row 4   |  [ TOUCH ]  | p10         | p11         | p12         |
        |   disabled  |  cmd+shift+b|  cmd+b      |  voice (PTT)|
        +-------------+-------------+-------------+-------------+
```

Rows come from the app's `slot_name` (`app/src/gpui_app.rs`); slot numbers were
confirmed against hardware with `padwatch.py`, not inferred.

## Firmware changes (`fw/src/main.rs`)

| change | why |
|---|---|
| `ENC_COUNTS_PER_DETENT` 4 -> 2 | this unit's EC11 is a half-cycle part producing 2 counts per detent; with 4, every other notch was swallowed in the accumulator |
| `KEY_LED_OFF_MASK` (new) | idle lighting is per-chain in flash, one pattern for all 13 keys, so individual positions can only be blanked in the paint loop |
| `KEY_LED_OVERRIDE_TTL_MS` (new) | per-key overrides never expired, so a host that vanished left its last status lit forever; they now lapse after 5s unless refreshed |

Only the TTL is unambiguously an upstream bug. The detent constant is
hardware-specific and the LED mask is a preference; both belong in runtime
config alongside the joystick threshold if they ever go upstream.

## Tools

### `openmicro/writemap.py`
Writes `config.json` to the pad's flash over raw HID — all 24 slots, joystick
threshold and mode, LED brightness and patterns, then `SAVE`. This exists
because the companion app does not write the keymap on launch: edits appeared
correct on disk and changed nothing on the hardware.

### `openmicro/herdr-led.py`
Resident daemon. Polls herdr and paints per-key LEDs with the firmware's
override command (`[0x0F, index, enable, r, g, b]`).

```
no agent / idle / unknown  ->  off
working                    ->  blue
blocked                    ->  red     (the agent is waiting on you)
done                       ->  green
```

Only pure red/green/blue are used: the SK6812MINI-E has three dies about a
millimetre apart under the cap, so any mixed colour reads as separate dots
rather than one light. That costs `idle` its colour — an idle agent looks the
same as an empty tab — but keeps the row legible.

It refreshes every 2s even when nothing changed, because **the refresh is the
liveness signal** for the firmware TTL. Stop refreshing and the row goes dark,
which is what happens when the Mac sleeps, locks (detected via
`CGSSessionScreenIsLocked`), unplugs, or the daemon dies.

It also publishes `<ws>:<tab> -> tab_id` to `/tmp/herdr-tabmap` for the focus
script.

### `openmicro/herdr-focus.sh`
Raises Ghostty and focuses a herdr tab. Used as the fallback path when Ghostty
is not frontmost.

Latency mattered here: every herdr call is a ~400ms round trip over the tunnel,
and the first version made four of them (~1.5s per press). Now it makes one,
because the daemon has already resolved the tab id and `tab focus` switches
workspace by itself.

### `openmicro/padwatch.py`
Prints the slot number of whatever key you press, from the firmware's own event
stream (`0x80` reports). Written after two rounds of wrong row arithmetic —
guessing the physical layout from source is how you end up binding the wrong
key twice.

### `karabiner/volstep-daemon.swift`
Resident volume stepper. Karabiner's `shell_command` tops out around 5-6/s
while a fast knob spin is 15-20 notches/s, so a fork-per-press dropped most of
them. This is the same resident-process pattern as the existing `swptt-daemon`:
Karabiner maps the media keys to `international3`/`international1` (keycodes no
US layout can produce) and the daemon does CoreAudio in-process. Measured ~45
steps/s afterwards, bounded by USB rather than software.

### `karabiner/volstep.py`
Pre-existing script, kept for the non-encoder paths, with two fixes:

- `FLOOR` 1e-4 -> 1/127. Below 1/127 a Bluetooth sink snaps the level straight
  back up, so the multiplicative ladder had a fixed point down there: press
  after press changed nothing.
- The bottom of the ladder now **mutes** rather than writing volume 0 (which
  the headset bounces back within ~28ms), and lands *on* the floor first so the
  quietest audible level stays selectable.

### `karabiner/volwatch.py`
Logs every volume change with its delta and ratio, so a multiplicative step is
distinguishable from an additive one at a glance. Diagnostic only.

## Wiring

- `launchagents/` — `RunAtLoad` + `KeepAlive` agents for both daemons. Install
  with `launchctl bootstrap gui/$(id -u) <plist>`. Both need **Input
  Monitoring**; the LED daemon opens the pad's HID interface and the volume
  daemon installs an event tap.
- `karabiner/karabiner-fragments.json` — the rules and the device entry to merge
  into `~/.config/karabiner/karabiner.json`. The device entry matters:
  Karabiner **ignores consumer-control interfaces by default**, so without it
  the pad's volume keys bypass every rule and hit macOS native 1/16 steps.
- `ghostty/initial-command.txt` — adds a Unix-socket forward so the Mac can
  reach herdr on the devserver. Without it the LEDs stay dark and key presses
  raise Ghostty but cannot switch tabs.

## Gotchas worth keeping

- **Only one process can hold the pad.** The companion app, `herdr-led.py`,
  `writemap.py`, `padwatch.py` and `hid-flash.py` all want the same raw HID
  interface. Stop the daemon before running any of the one-shot tools.
- **`HERDR_SOCKET_PATH`**, not `HERDR_CLIENT_SOCKET_PATH`. Both exist in the
  herdr binary; the latter is what herdr *exports into panes* and is silently
  ignored by the client.
- **`TabInfo.number` is an internal id**, not the tab number shown in the UI (a
  tab displayed as "1" reported 264). Tabs are addressed by position, matching
  what herdr's `Cmd+N` does.
- **F13/F18 are load-bearing.** A global Karabiner rule rewrites F13 to F18,
  which `swptt-daemon` watches for push-to-talk. Any pad key emitting F13,
  Shift+F13 or F18 starts dictation, which is why the tab keys use F14-F17 and
  the two Ghostty chords use International4/5.
