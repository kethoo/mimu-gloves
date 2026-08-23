# MiMu-Style Gesture Glove — Simulation & Instrument

A hand-motion musical instrument, inspired by MiMu gloves. An ESP32 with a
BNO08x IMU streams hand orientation over Bluetooth LE; the laptop maps
gestures to a real-time synthesizer. A keyboard simulator and a browser
frontend stand in for the glove, so every feature can be built and tested
with no hardware attached.

## Quick start (no hardware needed)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py --web    # browser frontend: XY pad, instrument picker, buttons
python main.py          # or play the "glove" with your keyboard
python main.py --demo   # scripted gesture demo, just listen
```

## Browser frontend (`--web`)

`main.py --web` opens `ui.html` and starts a WebSocket server on
`ws://127.0.0.1:8765`. The page is the glove: drag the XY pad (x = roll,
y = tilt), slide yaw, and click — or key — the same actions as terminal
mode. It also shows live state: hand values, active modes, loop length,
and a REC indicator. The browser is just another `GloveSource`; audio
still runs in Python.

## Tuning the feel

`ROLL_RANGE`, `PITCH_RANGE`, and `YAW_RANGE` at the top of `mapping.py` set
how far you must move to reach the extremes. They default to ~45°, matching
a comfortable wrist, not the ±90° the sensor can report — mapping against 90
leaves most of the musical range unreachable in practice. Lower them for a
twitchier instrument, raise them for finer control.

**Nothing responding to your hand?** The firmware never fabricates motion.
If the ESP32 cannot see the BNO08x it sends a status flag of 0 and no
orientation at all, and `ble_receiver.py` says so plainly instead of feeding
placeholder values to the synth:

```
[!! The ESP32 cannot see the BNO08x — no motion data is being produced.]
```

It retries the sensor every 3 s and prints `[sensor recovered — playing real
motion]` when it comes back. Over serial, each heartbeat line is labelled
`SENSOR` (live) or `NO-IMU!`, and the boot-time I2C scan should list `0x4B`.

(An earlier build streamed a synthetic "waving hand" when the sensor was
missing, so the instrument could be tested before the hardware arrived. It
was removed — convincing fake data is worse than silence.)

## Instruments

Five drone instruments, switchable live (keys `1`–`5` or the UI):
**Saw Lead**, **Organ** (additive drawbars), **String Pad** (detuned saw
ensemble), **FM Bell** (inharmonic 2-op FM), **Flute** (vibrato sine +
breath noise). All run through the same gesture-controlled filter/pan
chain — add your own recipe in `GloveSynth._render_tone`.

## How gestures map to sound (mapping.py)

| Hand gesture        | Sensor signal | Sound effect                          |
| ------------------- | ------------- | ------------------------------------- |
| Roll wrist left/right | roll        | Musical pitch (A-minor pentatonic) + voice-loop speed |
| Tilt hand up/down   | pitch         | Brightness (lowpass filter cutoff)    |
| Point left/right    | yaw           | Stereo pan                            |
| Bend the finger     | flex          | Volume / expression (like a breath controller) |
| Move faster         | motion        | Volume swells — only when no flex sensor is connected |
| Punch               | accel spike   | Percussive drum hit                   |

## Voice looping (mic in, gestures shape it)

Press `v` to record from the microphone, `v` again to stop — the take
immediately starts looping through the gesture chain: roll left/right slows
down/speeds up the voice (with pitch shift), tilt darkens/brightens it, yaw
pans it, and flex — or motion, with no flex sensor — controls its volume.
`p` pauses the loop, `m` mutes the drone tone so you hear the voice alone. On the hardware glove this becomes a
physical record button (a press edge sent in the BLE packet — hook noted in
`ble_receiver.py`). macOS will ask for microphone permission the first time.

Button-like gestures travel as an **event queue** (`GloveSource.events`),
separate from the continuous orientation stream, so a press is never missed
or applied twice.

## Live voice mode (`l`)

The mic streams straight through the synth while you speak — no recording
step. Roll pitch-shifts your voice in real time (0.5x demon to 2x chipmunk,
via a granular delay-line pitch shifter, ~25 ms latency), tilt
darkens/brightens it, yaw pans it, and hand motion controls a feedback
echo — wave and your words trail off into repeats. A noise gate keeps room
hiss from droning through the effects. **Wear headphones**: with speakers
the pitched-up output re-enters the mic and feeds back.

## Voice modes

- **Overdub (`o`)** — record another take *while the loop plays*; it's mixed
  on top, aligned to where the loop was when you pressed. Build up layers.
- **Granular (`g`)** — the loop becomes a cloud of 90 ms Hann-windowed
  grains. Time freezes; rolling your hand scrubs the playhead through the
  recording. Classic "frozen voice" texture.
- **Slice mode (`b`)** — the recording is split into 4 pads. A punch fires
  one chunk as a one-shot, and roll picks which pad — drum-machine style.
  Toggle off to make punches drum hits again.

## Architecture

```
ESP32 + BNO08x ──BLE notify, 50 Hz───> ble_receiver.py ──┐
                                                          ├─> SensorFrame ─> mapping.py ─> synth.py ─> speakers
keyboard / demo script ────────────────> sensors.py ─────┘        (100 Hz control rate)    (44.1 kHz audio rate)
```

Two clock rates, on purpose: the glove streams at 50 Hz and the control loop
runs at 100 Hz (zeroing, smoothing, mapping), while `synth.py` renders audio
at 44.1 kHz inside a PortAudio callback with per-sample parameter smoothing,
so control updates never click. End-to-end latency is roughly 10–20 ms.

The simulator (`sensors.py`) and the BLE receiver (`ble_receiver.py`) emit
identical `SensorFrame` objects, so switching to real hardware is just
`python main.py --ble` — mapping and synth code don't change.

## Why the glove never appears in macOS Bluetooth settings

It uses Bluetooth **Low Energy**, which does not pair with the OS. System
Settings → Bluetooth lists Classic devices that bond (headphones, keyboards);
BLE peripherals simply advertise, and apps connect straight to them via
CoreBluetooth. So there is nothing to pair and nothing to "forget" — but only
one program may hold the connection at a time.

```bash
python main.py --scan   # list BLE devices and confirm the glove is advertising
```

## Flex sensor

Wiring: `3V3 → flex → GPIO34 → 47k → GND`. GPIO34 is input-only and on ADC1,
which keeps working while the BLE radio is active (ADC2 pins do not).

The ESP32 sends the raw ADC value and the laptop self-calibrates: it widens
its min/max as you bend, so no fixed thresholds are needed. Until it sees a
swing of at least `FLEX_MIN_SPAN` (150 ADC counts) it reports "no flex
sensor" and volume falls back to hand motion — it never guesses. If bending
makes the sound quieter instead of louder, flip `FLEX_INVERT` in
`ble_receiver.py`.

`python diagnose.py` prints the live flex ADC value and the total range seen,
which is the quickest way to check the sensor and pick a divider resistor.

## Finding intermittent connections

```bash
python diagnose.py   # live health monitor; Ctrl+C for a dropout timeline
```

Prints per-second sensor status, flex ADC value and movement, and announces
the instant the BNO08x drops out or returns.

**Suspect bus speed before wiring.** Recurring dropouts here turned out to be
the 400 kHz I2C clock, not a loose jumper — see the stall trap below. At
100 kHz the connection held indefinitely. Only if dropouts persist is it
worth wiggling one wire at a time to find a bad contact.

The flex sensor cannot electrically disturb the IMU — it draws ~55 µA through
the 47k divider, GPIO34 is on ADC1 (independent of the radio), and it shares
no pins with I2C. If adding it breaks the IMU, the cause is mechanical
(disturbed jumpers) or a slipped jumper shorting 3V3 to GND, which would sag
the rail below ~3.0V.

## Hardware notes (BNO08x)

Wiring: VCC→3V3, GND→GND, SDA→GPIO21, SCL→GPIO22, RST→GPIO4, PS0/PS1→GND
(I2C mode), ADD→3V3 (address 0x4B). Flex sensor: 3V3 → flex → GPIO34, with a
47k pulldown to GND.

Flash with `UploadSpeed=115200` — the USB adapter fails at the default
921600:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 esp32/glove_ble
arduino-cli upload -p "$(ls /dev/cu.usbserial-* | head -1)" \
  --fqbn esp32:esp32:esp32:UploadSpeed=115200 esp32/glove_ble
```

macOS renames the port on each replug (`usbserial-10`, `-110`, …), hence the
`ls` rather than a fixed path.

The glove sends 32-byte packets at 50 Hz: `roll, pitch, yaw` (degrees),
`lax, lay, laz` (m/s², gravity removed), `status` (1 = live sensor, 0 = not
detected) and `flex` (raw ADC 0–4095). `ble_receiver.py` also accepts the
older 28- and 24-byte layouts.

**The BNO08x stall trap.** The sensor's SHTP transport can wedge: reports
stop, and it keeps ACKing its I2C address while refusing to re-initialize
(`I2C devices found: 0x4B` immediately followed by `I2C address not found`).
Four things fix or contain it, in order of importance:

1. **I2C at 100 kHz, not 400 kHz.** This was the root cause — measured, at
   400 kHz over dupont jumpers it wedged every ~15–20 s; at 100 kHz it ran
   90 s under BLE load with zero dropouts. Long breadboard wires add enough
   capacitance to corrupt fast-mode transfers.
2. 50 Hz reports, not 100 Hz on two sensors, which saturates the bus.
3. A 500 ms settle after hardware reset — the library's short pulse leaves
   the sensor undetected — plus a `delay(2)` yield in `loop()` so polling
   doesn't starve the BLE stack.
4. A watchdog that re-enables reports twice, then hard-resets, then reboots
   the ESP32 outright. Rebooting is the only recovery that works once the
   sensor wedges, and it restores streaming in ~4 s without you touching it.

The firmware prints a 1 Hz heartbeat over serial (115200) — the fastest way
to tell a live sensor (values jitter) from a wedged one (bit-identical).

## When the hardware is ready

```bash
python main.py --ble --web   # glove plays; browser picks instruments/modes
python main.py --ble         # glove only, no controls
```

`--ble` scans for the device named `MIMU-GLOVE`, holds the first ~0.8 s as
the neutral hand pose (keep it still until `[glove calibrated — play!]`),
then plays. Because the BNO08x fuses on-chip, no fusion filter runs here.

Combining `--ble --web` is the useful mode: the glove has no buttons yet, so
the browser supplies instrument switching, voice recording, and mode
toggles, while the XY pad mirrors your real hand. `MergedGloveSource` takes
the pose from the glove and merges control events from both.

## Libraries used

- **sounddevice** — PortAudio bindings, real-time duplex audio (mic + speakers)
- **numpy** — block-wise DSP in the audio callback
- **websockets** — browser frontend transport (`--web`)
- **bleak** — Bluetooth LE client (`--ble`, `--scan`, `diagnose.py`)

No sensor-fusion library is needed: the BNO08x fuses on-chip and sends
finished orientation, so the laptop only zeroes and smooths it.
