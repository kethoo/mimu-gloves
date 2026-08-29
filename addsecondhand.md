# Adding a Second Hand

Notes on what it would take to run two gloves (left + right) instead of one.
Everything below is current as of the `main` branch; line numbers refer to it.

## Summary

The project is single-glove end to end: one ESP32, one BLE connection, one
`SensorFrame` per tick, one set of synth targets. Nothing is *architecturally*
opposed to two hands — `MergedGloveSource` already composes two sources — but
four places hardcode the assumption, and two of the fixes are real design work
rather than plumbing.

## Where "one glove" is baked in

### 1. Firmware advertises a fixed name

`esp32/glove_ble/glove_ble.ino:32`

```c
#define DEVICE_NAME  "MIMU-GLOVE"
```

Two boards flashed with this sketch advertise the **same** name.

### 2. Host discovery takes the first match

`ble_receiver.py:37` pins the name, and `ble_receiver.py:124` looks it up:

```python
device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10)
```

`find_device_by_name` returns the *first* device that matches. With two gloves
powered, we would connect to whichever the scanner happened to see first —
nondeterministically, run to run — and the second glove would never be
connected at all. No error; it would just silently be the wrong hand about half
the time. (`ble_receiver.py:128` and `:140` also print the constant, so those
messages would need the per-hand name too.)

### 3. The mapping collapses to one set of targets

`mapping.py:54`

```python
def apply(frame: SensorFrame, synth: GloveSynth) -> None:
```

One frame in, and every line writes to a single set of `synth.target_*`. Two
hands calling this would be last-writer-wins — the gloves would fight over
pitch, cutoff and pan at the 100 Hz control rate.

### 4. Events carry no hand identity

`ble_receiver.py:271` appends the bare string:

```python
self.events.append("punch")
```

`mapping.handle_event` dispatches on that string, so there is no way to know
which hand punched.

## What already helps

`sensors.py:110` `MergedGloveSource` is the right pattern already in the
codebase: pose from one source, events merged from both, with each child
keeping its own interface. Two gloves is the same shape, just with two pose
sources instead of one.

Per-connection state is also already per-source — the pose baseline
(`_reset_pose_calibration`) and the self-calibrating `_FlexChannel` instances
live on the `BleGloveSource`, so two instances calibrate independently with no
changes.

## Cheap plumbing

1. **Distinguish the boards.** Add a build-time `#define HAND_SUFFIX "-L"` /
   `"-R"` in the sketch and advertise `MIMU-GLOVE-L` / `MIMU-GLOVE-R`. The
   service and characteristic UUIDs can stay identical — devices are addressed
   by device, not by UUID.
2. **Make the name a parameter.** `DEVICE_NAME` becomes a constructor arg:
   `BleGloveSource(name="MIMU-GLOVE-L")`. Genuinely a one-line change.
3. **Tag events per hand.** Prefix on emit (`"L:punch"`) so `handle_event` can
   route them.

## The two things that are real work

### The BLE event loop

Do **not** just instantiate two `BleGloveSource` objects. `start()` spawns a
thread whose `_run` calls `asyncio.run` (`ble_receiver.py:111`), so two
instances means two independent event loops each driving bleak. That is fragile
on macOS/CoreBluetooth, particularly with two scanners running concurrently.

The right shape is **one BLE thread, one event loop**, with `asyncio.gather`
over both connections, writing into two separate frame slots.

### The mapping

This is the actual design question and there is no default answer — it depends
on what the second hand should *do*. The MiMu model is **disjoint parameter
sets**, not two hands averaged into one voice. For example:

- right hand: pitch / brightness / pan, as today
- left hand: filter resonance, loop scrub, mode switches

Keep **one** `GloveSynth` regardless. Two would mean two output streams
contending for the audio device; both hands should drive a single voice.

## Smaller bug this would expose

`sensors.py:154` — inside the loop over both children:

```python
got = source.take_audio()
if got is not None:
    self.audio = got
```

If both gloves finished a recording within the same ~10 ms tick, the second
assignment silently overwrites the first. Needs a queue, or at least a
"pending" guard, once two mics exist.

## Non-issues

- **Bandwidth.** 36 bytes at 50 Hz is ~1.8 kB/s per glove. Only the recorded
  audio-take transfers meaningfully share the link, and those are already
  asynchronous — two concurrent transfers are slower, not broken.
- **Firmware host assumptions.** The ESP32 is a pure BLE peripheral; it makes
  no assumptions about being the only glove.
