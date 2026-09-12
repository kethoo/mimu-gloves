# Adding a Second Hand

Notes on what it would take to run two gloves (left + right) instead of one.
Everything below is current as of `main` (refreshed after the gesture/scene
rework); line numbers refer to it.

## Summary

The project is single-glove end to end: one ESP32, one BLE connection, one
`SensorFrame` per tick, one set of synth targets. Nothing is *architecturally*
opposed to two hands — `MergedGloveSource` already composes two sources — but
five places hardcode the assumption, and three of the fixes are real design
work rather than plumbing.

The count went from four to five: the gesture rework moved per-hand state
(postures, scale step, current scene) into module-level globals in
`mapping.py`, which two hands would share. See §5 — it is the one that fails
most confusingly, because it breaks *both* hands rather than favouring one.

## Where "one glove" is baked in

### 1. Firmware advertises a fixed name

`esp32/glove_ble/glove_ble.ino:34`, used at `:277`

```c
#define DEVICE_NAME  "MIMU-GLOVE"
```

Two boards flashed with this sketch advertise the **same** name.

### 2. Host discovery takes the first match

`ble_receiver.py:37` pins the name, and `ble_receiver.py:169` looks it up:

```python
device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10)
```

`find_device_by_name` returns the *first* device that matches. With two gloves
powered, we would connect to whichever the scanner happened to see first —
nondeterministically, run to run — and the second glove would never be
connected at all. No error; it would just silently be the wrong hand about half
the time. (`ble_receiver.py:173` and `:185` also print the constant, so those
messages would need the per-hand name too.)

### 3. The mapping collapses to one set of targets

`mapping.py:184`

```python
def apply(frame: SensorFrame, synth: GloveSynth) -> None:
```

One frame in, and every line writes to a single set of `synth.target_*`. Two
hands calling this would be last-writer-wins — the gloves would fight over
pitch, cutoff and pan at the 100 Hz control rate. Measured with two frames
alternating on the same tick: left asking for 784 Hz and right for 262 Hz
leaves the synth at 262 Hz, silently.

### 4. Events carry no hand identity

`ble_receiver.py:322` and `:341` append bare strings:

```python
self.events.append("punch")
self.events.append("scene")
```

`mapping.handle_event` (`mapping.py:249`) dispatches on that string, so there
is no way to know which hand punched or which hand pressed its button.

### 5. The mapping keeps per-hand state in module globals

`mapping.py:117-121`

```python
_last_step = 0
_scene = 0
_posture: str | None = None
_pending: str | None = None
_pending_since = 0.0
```

These are module-level, so *every* caller shares them. This is the newest and
least obvious blocker, and it does not merely favour one hand — it breaks the
feature outright for both:

- **Postures never fire.** `_pending` and `_pending_since` implement the dwell
  timer (0.35 s, or 1.0 s for the open hand). With two hands alternating into
  `apply()`, each tick's second call sees a different posture from the first
  and resets `_pending_since`. Neither hand ever accumulates dwell time.
  Verified: a hand holding a deliberate fist for 0.6 s alongside a normally
  playing hand leaves `_posture` at `None` indefinitely.
- **Scene and scale are global.** `_scene` selects the instrument, scale and
  modes; `_last_step` is the committed scale step with its hysteresis. One
  hand's button press would move the other hand's scale, and the two hands'
  pitch quantisers would fight over the same committed step.

The fix is mechanical but touches the whole file: move this state into a small
per-hand object (or a `HandState` dataclass passed into `apply`), leaving
`SCENES`/`SCALES`/thresholds as the only true module-level constants. Decide at
the same time whether a scene is per-hand or shared — see below.

## What already helps

`sensors.py:122` `MergedGloveSource` is the right pattern already in the
codebase: pose from one source, events merged from both, with each child
keeping its own interface. Two gloves is the same shape, just with two pose
sources instead of one.

Per-connection state is also already per-source, so two `BleGloveSource`
instances calibrate independently with no changes:

- the pose baseline (`_reset_pose_calibration`, `ble_receiver.py:226`)
- the self-calibrating `_FlexChannel` instances (`:138`, `:139`)
- the scene-press counter (`:142`), which is re-seeded per connection

`midi_out.py` needs **no change at all**. `MidiOut.update(synth)` reads the
target values off the synth rather than mapping frames a second time, so
however many hands end up writing those targets, the MIDI sink follows
automatically. That is the payoff of keeping one place where gesture becomes
musical intent.

## Cheap plumbing

1. **Distinguish the boards.** Add a build-time `#define HAND_SUFFIX "-L"` /
   `"-R"` in the sketch and advertise `MIMU-GLOVE-L` / `MIMU-GLOVE-R`. The
   service and characteristic UUIDs can stay identical — devices are addressed
   by device, not by UUID.
2. **Make the name a parameter.** `DEVICE_NAME` becomes a constructor arg:
   `BleGloveSource(name="MIMU-GLOVE-L")`. Genuinely a one-line change.
3. **Tag events per hand.** Prefix on emit (`"L:punch"`) so `handle_event` can
   route them. Note there are now two event emitters in `ble_receiver.py`
   (punch and scene), plus the keyboard/web sources, which would need a
   convention for "no particular hand".

## The things that are real work

### The BLE event loop

Do **not** just instantiate two `BleGloveSource` objects. `start()` spawns a
thread whose `_run` calls `asyncio.run` (`ble_receiver.py:155`), so two
instances means two independent event loops each driving bleak. That is fragile
on macOS/CoreBluetooth, particularly with two scanners running concurrently.

The right shape is **one BLE thread, one event loop**, with `asyncio.gather`
over both connections, writing into two separate frame slots.

### The mapping state refactor

§5 above. Mechanical, but it touches every function in `mapping.py` and should
be done before, not after, the second hand is wired up — otherwise the
symptoms (postures silently never firing) are very hard to attribute.

### What the second hand should do

This is the actual design question and there is no default answer. The MiMu
model is **disjoint parameter sets**, not two hands averaged into one voice.
For example:

- right hand: pitch / brightness / pan / volume, as today
- left hand: filter resonance, loop scrub, granular position, mode switches

With scenes in the picture there is a further choice: one shared scene that
both hands play within (simplest, and probably right — a scene is a
performance setup, not a hand setting), or a scene per hand, which would mean
two scales sounding at once and needs a musical reason.

Keep **one** `GloveSynth` regardless (`main.py:123`). Two would mean two output
streams contending for the audio device; both hands should drive a single
voice.

## Smaller bug this would expose

`sensors.py:164` — inside the loop over both children:

```python
got = source.take_audio()
if got is not None:
    self.audio = got
```

If both gloves finished a recording within the same ~10 ms tick, the second
assignment silently overwrites the first. Needs a queue, or at least a
"pending" guard, once two mics exist.

## Non-issues

- **Bandwidth.** 40 bytes at 50 Hz is ~2 kB/s per glove. Only the recorded
  audio-take transfers meaningfully share the link, and those are already
  asynchronous — two concurrent transfers are slower, not broken.
- **Firmware host assumptions.** The ESP32 is a pure BLE peripheral; it makes
  no assumptions about being the only glove.
- **Flex calibration.** Self-calibrating per `_FlexChannel` instance, so two
  gloves with differently-taped sensors each learn their own range.
- **MIDI output.** See above — `midi_out.py` reads synth targets, not frames.
