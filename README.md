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

Seven instruments, switchable live (keys `1`–`7` or the UI): **Saw Lead**,
**Organ** (drawbar additive), **String Pad** (detuned saw ensemble, slow
bow), **FM Bell** (inharmonic 2-op FM with a decaying modulation index),
**Flute** (vibrato sine with a breath chiff on the onset), **Plucked
String** (Karplus-Strong), and **Electric Guitar**. All run through the same
gesture-controlled filter/pan chain — add your own recipe in
`GloveSynth._render_tone`.

The guitar reuses the plucked string and sends it through the rest of a real
rig, in order: a second tap on the delay line for the pickup (the line *is*
the string, so a second tap really is a second listening point, and the comb
notches it produces are what make a bridge pickup sound nasal), then an
overdriven clipper, then a speaker cabinet. The cabinet is not optional — a
clipper generates harmonics all the way to Nyquist, and a real cab is a
narrow lossy box that removes them. Its string is also tuned differently from
the acoustic pluck (`KS_VOICES`): an electric string drives a magnetic pickup
instead of radiating into a soundboard, so it keeps its highs and rings far
longer. Measured at 1.25 s, the guitar is 9 dB down where the acoustic pluck
is 37 dB down; most of that is the clipper compressing the loud part of the
note, which is also why a real one blooms instead of just fading.

The shared filter is a 4-pole resonant ladder (−24 dB/oct). It replaced a
single pole, which only reached −4 dB an octave above its cutoff: most of
every instrument's harmonics survived it, so everything came out fizzy and
tilt behaved like a weak tone knob. Because a real 4-pole *does* cut, the
cutoff now key-follows the note (`mapping.py`) — otherwise the darkest tilt
would put the top of the scale two octaves below its own corner and bury it.
The sawtooths are band-limited with PolyBLEP; the naive phase ramp they used
before folded 2% of its energy back as inharmonic grit at the top of the
scale.

Each has its own amplitude envelope (`ENVELOPES` in `synth.py`), and that
table carries most of the difference between them. An earlier build gave
every instrument a flat, constant envelope, and they were hard to tell
apart even though their spectra differed — the ear identifies an
instrument mostly by its attack and decay, not by its steady-state
harmonics. Instruments marked *struck* (bell, pluck) re-articulate on a
note change instead of gliding, and a punch re-strikes them.

## Driving a DAW instead (--midi)

Add `--midi` to any mode and the glove also streams MIDI, so the same
gestures can play a sampled or modelled instrument in a DAW:

```bash
python main.py --midi                              # virtual port; pick "Glove" as a MIDI input
python main.py --midi --midi-port "IAC Driver Bus 1"
python main.py --ble --web --midi                  # real glove, browser buttons, MIDI out
```

The built-in synth keeps playing, so this is an A/B rather than a
replacement. `midi_out.py` is a *sink*, not a second mapping: `mapping.apply`
has already turned the hand into musical intent and left it on the synth, and
`MidiOut.update` reads those same target values back out. There is still only
one place where a gesture becomes music, so the two outputs cannot drift
apart, and every input (keyboard, browser, BLE, demo) works with it unchanged.

| MIDI                    | Driven by                                    |
| ----------------------- | -------------------------------------------- |
| Note on/off, channel 1  | Tilt — the same scale step the synth plays    |
| Velocity                | Flex 1 at the moment the note starts          |
| CC 11 expression        | Flex 1, continuously                          |
| CC 74 brightness        | Roll (the standard filter-cutoff CC)          |
| CC 1 mod wheel          | Flex 2 (vibrato depth)                        |
| CC 10 pan               | Yaw                                           |
| Note 38, channel 10     | Punch (GM acoustic snare)                     |
| Program change          | Instrument keys `1`–`7`, mapped to GM patches |

A held note stays held — only a change of scale step is a new note, the same
rule the synth uses — and `m` (mute drone) releases it. Quitting sends All
Notes Off, so an interrupted note can't drone on in the DAW.

Needs `python-rtmidi`. The import is lazy, so everything else runs without it.

## How gestures map to sound (mapping.py)

| Hand gesture              | Sensor signal | Sound effect                    |
| ------------------------- | ------------- | ------------------------------- |
| ↪️ Rotate wrist            | roll          | Filter cutoff / brightness, and the voice-loop scrub |
| ↕️ Tilt hand up/down       | pitch         | Musical pitch, quantized to the scene's scale |
| 👈 Point left/right        | yaw           | Stereo pan                      |
| 🤏 Bend index finger       | flex (GPIO34) | Volume / expression, like a breath controller |
| ✊ Fist (both fingers bent) | flex + flex2  | Activate the sound              |
| 🖐️ Open hand (both straight) | flex + flex2 | Deactivate it                   |
| 🤚 Index straight, middle bent | flex + flex2 | Next instrument             |
| Bend middle finger        | flex2 (GPIO35)| Vibrato depth — the note wobbles like a singer |
| Move faster               | motion        | Volume swells — only when no flex sensor is connected |
| 💥 Wrist flick             | accel spike   | Percussive drum hit             |
| 👆 Button, short press     | GPIO18        | Next scene                      |
| Button, hold              | GPIO18        | Records your voice on the glove (see below) |

**Scenes** bundle an instrument, a scale and the active modes, so one button
press moves the whole setup — `Lead` (saw, pentatonic), `Cathedral` (organ,
minor), `Bowed` (strings, dorian), `Chimes` (bell, whole-tone), `Amp`
(guitar, minor), `Cloud` (strings, whole-tone, granular). Edit `SCENES` in
`mapping.py`.

**Two honest limits.** "Up/down" and "left/right" are tilt and yaw, not
translation: the BNO08x reports orientation only, and deriving position would
mean double-integrating acceleration, which drifts into nonsense within
seconds. And two flex sensors give four states total, while the index finger
is already the volume control — so a posture fires only on entry, once, and
only after being held (0.35 s, or 1.0 s for the open hand). Measured, a slow
fade-out running into a fade-in dwells ~0.84 s in the open-hand region, which
is why that one needs the longer dwell. Bending the index past the posture
threshold always re-opens the gate, so a stray deactivate can never strand you
in silence.

## Recording on the glove (button + INMP441)

Hold the glove's button, speak into the microphone on your hand, release.
The ESP32 records to its own memory (LED solid), transfers the take over
Bluetooth (LED slow blink), and it becomes the voice loop:

```
[glove recording: 2.1s incoming...]
[glove recording received: 2.1s, peak 0.43 — now playing as the loop]
```

From there every voice feature already works on it — roll changes speed and
pitch, tilt filters it, granular freezes it, slices chop it into drum pads,
overdub layers on top. The glove take just fills the same buffer a laptop
recording does.

This works because a recording tolerates delay in a way live monitoring
cannot: Bluetooth is far too slow to carry live audio (~32 kB/s for 16 kHz
against a link that manages a fraction of that, plus buffering latency), but
a finished take only has to *arrive*. Capture on the glove, transfer
afterwards, play on the laptop.

Takes are up to 8 seconds, held in the module's PSRAM — internal RAM has no
room for the buffer beside the BLE stack. Without PSRAM it falls back to 2
seconds. `mapping.py`'s `record` event still drives the laptop microphone,
which is unchanged and lower latency for the live modes.

## Voice looping (laptop mic, gestures shape it)

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

## Flex sensors

Two of them, each its own divider:

```
3V3 ── flex ── GPIO34 ── 15k ── GND     finger 1 -> volume
3V3 ── flex ── GPIO35 ── 15k ── GND     finger 2 -> vibrato
```

The resistor is what makes it readable at all: a flex sensor is a variable
resistor, but the ADC measures voltage. The two form a divider, so bending
raises the sensor's resistance and drops the voltage at the pin. Pick a
fixed resistor near the middle of the sensor's own range for the widest
swing. Without it the pin floats and reads noise.

Both pins are on ADC1, which keeps working while the BLE radio is active
(ADC2 pins do not), and GPIO34–39 are input-only — fine for sensors, never
usable as outputs. That leaves GPIO36 and 39 for two more fingers.

The ESP32 sends the raw ADC value and the laptop self-calibrates: it widens
its min/max as you bend, so no fixed thresholds are needed. Until it sees a
swing of at least `FLEX_MIN_SPAN` (80 ADC counts) it reports "no flex
sensor" and volume falls back to hand motion — it never guesses. If bending
makes the sound quieter instead of louder, flip `FLEX_INVERT` in
`ble_receiver.py`.

`python diagnose.py` prints the live flex ADC value and the total range seen,
which is the quickest way to check the sensor and pick a divider resistor.

## Is it the code or the hardware?

`esp32/bno_min/` is a minimal sketch — no BLE, no watchdog, no bus recovery —
that just initializes the BNO08x and prints how many reports per second it
receives. Flash it to settle the question in 30 seconds:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 esp32/bno_min
arduino-cli upload -p "$(ls /dev/cu.usbserial-* | head -1)" \
  --fqbn esp32:esp32:esp32:UploadSpeed=115200 esp32/bno_min
```

A healthy sensor prints `reports/s=~50` continuously. `begin_I2C OK` followed
by `reports/s=0` means the sensor initializes but will not stream — that is a
hardware fault (power, wiring or a latched sensor), not a firmware bug. Try a
full power cycle first: unplugging USB for ~10 s clears states that toggling
RST does not.

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
the 15k divider, GPIO34 is on ADC1 (independent of the radio), and it shares
no pins with I2C. If adding it breaks the IMU, the cause is mechanical
(disturbed jumpers) or a slipped jumper shorting 3V3 to GND, which would sag
the rail below ~3.0V.

## Hardware notes (BNO08x)

| Part | Pins |
| --- | --- |
| BNO08x | SDA→21, SCL→22, RST→4, ADD→3V3 (0x4B), PS0/PS1→GND |
| INMP441 mic | SCK→33, WS→25, SD→32, L/R→GND |
| Flex sensors | GPIO34, GPIO35 (each with a 15k to GND) |
| Button | GPIO18 → GND (internal pull-up) |
| Status LED | GPIO19 via 220Ω |

Avoid GPIO0/2/12/15 (boot strapping), GPIO6–11 (flash), and **GPIO16/17** —
measured on this board, GPIO16 reads LOW with the pull-up enabled and nothing
attached, because it belongs to the PSRAM on WROVER modules.

Flash with **both** options: `PSRAM=enabled` (the audio buffer lives there)
and `UploadSpeed=115200` (the USB adapter fails at the default 921600).
Dropping either one is a silent way to waste ten minutes:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32:PSRAM=enabled esp32/glove_ble
arduino-cli upload -p "$(ls /dev/cu.usbserial-* | head -1)" \
  --fqbn esp32:esp32:esp32:PSRAM=enabled,UploadSpeed=115200 esp32/glove_ble
```

macOS renames the port on each replug (`usbserial-10`, `-110`, …), hence the
`ls` rather than a fixed path.

The glove sends 40-byte packets at 50 Hz: `roll, pitch, yaw` (degrees),
`lax, lay, laz` (m/s², gravity removed), `status` (1 = live sensor, 0 = not
detected), `flex, flex2` (raw ADC 0–4095) and `scenePresses` (a running count
of short button presses — a count rather than a pulse, so a dropped
notification cannot swallow a press). Recordings travel separately on their
own characteristic: a 12-byte header (`AUD0`, sample count, rate) then raw
little-endian int16. `ble_receiver.py` also accepts the older 36/32/28/24-byte
layouts, so a glove on previous firmware still plays — it just has no scene
button.

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

It runs until you stop it. If the link drops — or the glove reboots itself to
clear a wedged sensor — the receiver reconnects and re-zeroes the neutral
pose automatically, so a dropout costs a few seconds rather than the session.

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
