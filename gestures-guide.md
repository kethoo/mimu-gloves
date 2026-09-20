# Gestures Guide

How to play the gloves. For *why* the mapping is shaped this way, see
`renewed-design.md`.

The two hands are deliberately asymmetric. One **makes sound**; the other
**shapes what has already been captured**. Neither does the other's job, and
there is exactly one of each parameter in the instrument — one filter, one
reverb, one scrub, one pitch — each owned by exactly one hand.

---

## Starting up

```bash
# both gloves
python main.py --ble --voice-glove --web

# pick your audio devices (see them all with --list-devices)
python main.py --ble --voice-glove --web \
  --audio-out "AirPods" --audio-in "MacBook Air Microphone"

# no hardware: play both hands from the browser, 'h' or the hand buttons
python main.py --web
```

Wear headphones whenever the microphone is live. Processed output re-entering
an open mic is a feedback loop, and reverb and delay make it build rather than
just ring.

**Which glove is which:** the boards advertise as `MIMU-GLOVE-I` (live) and
`MIMU-GLOVE-V` (loop). Check with `python main.py --scan`, or
`python monitor.py --identify` over USB.

---

## 🎹 Live hand

Everything about the note happening right now. **This hand never touches an
effect.**

| Gesture | Controls | Notes |
| --- | --- | --- |
| ↕️ **Tilt up/down** | Musical pitch | Quantized to the scene's scale. ±45° covers the full range |
| ↪️ **Rotate wrist** | Brightness | Filter cutoff, 200 Hz → 6 kHz, follows the note so low tilt never buries high pitches |
| 👈 **Point left/right** | Stereo pan | |
| 🤏 **Index bend** | Volume | 34 dB curve, so the whole travel is audible |
| **Middle bend** | Vibrato depth | |
| **Move faster** | Hit velocity | Wrist flicks land harder when the hand is moving |
| ✊ **Fist** | Sound ON | |
| 🖐️ **Open hand** | Sound OFF | Needs a full second held — see *Why postures need a hold* |
| 🤚 **Point** | Next instrument | Index straight, middle bent |
| 💥 **Wrist flick** | Drum hit | Velocity from how fast you were moving |
| 👆 **Button tap** | Next scene | |
| 👆 **Button hold** | Capture the phrase into the loop | The handoff to the other hand |

**Scenes** (button tap) change instrument, scale and mode together:

| # | Scene | Instrument | Scale | Mode |
| --- | --- | --- | --- | --- |
| 1 | Lead | Saw | pentatonic | |
| 2 | Cathedral | Organ | minor | |
| 3 | Bowed | String pad | dorian | |
| 4 | Chimes | FM bell | whole-tone | |
| 5 | Amp | Electric guitar | minor | |
| 6 | Cloud | String pad | whole-tone | granular |

Instruments can also be picked directly with keys `1`–`7`: saw, organ,
strings, bell, flute, pluck, guitar.

---

## 🔁 Loop hand

Everything about time and space. **This hand never plays a note.**

| Gesture | Controls | Notes |
| --- | --- | --- |
| ↪️ **Rotate wrist** | **Scrub** the playhead | The signature gesture. In granular mode time is frozen and rolling walks through the recording |
| ↕️ **Tilt up/down** | Loop speed | ±1 octave. Also pitches the mic when there is no loop |
| 👈 **Point left/right** | Where the captured material sits | Independent of the live hand's pan |
| 🤏 **Index bend** | Blend live against looped | |
| **Middle bend** | Reverb amount | The one reverb in the instrument |
| **Move faster** | Delay feedback | Wave and the repeats ring on longer |
| ✊ **Fist** | **FREEZE** | Holds this exact moment. Time stops until you open |
| 🖐️ **Open hand** | Release | Fades back in rather than cutting |
| 🤚 **Point** | Cycle loop mode | normal → granular → slices |
| 💥 **Wrist flick** | Fire a slice, or stutter | Slice in slice mode, stutter otherwise |
| 👆 **Button tap** | Next preset | |
| 👆 **Button hold** | Overdub another layer | |

**Presets** (button tap) set reverb, delay and a pitch offset together. The
offset is *added* to what tilt is asking for, so a preset colours the hand
rather than overriding it:

| # | Preset | Reverb | Delay | Pitch |
| --- | --- | --- | --- | --- |
| 1 | Dry | 0.05 | – | – |
| 2 | Hall | 0.75 | – | – |
| 3 | Slap | 0.25 | ✓ | – |
| 4 | Cavern | 0.95 | ✓ | – |
| 5 | Chipmunk | 0.20 | – | +7 st |
| 6 | Demon | 0.45 | ✓ | −7 st |

**This hand is never idle.** Before you have recorded anything it shapes the
live microphone instead — reverb, delay, pitch. It is always the "space and
time" hand; it simply has more to do once material exists.

---

## 🤲 Both hands together

Only three, on purpose. One combination axis is expressive; three would mean
holding both hands' absolute angles steady while varying the difference.

| Gesture | Effect |
| --- | --- |
| **Relative roll** — rotate the hands oppositely | Stereo width. Aligned collapses toward mono, opposed widens |
| ✊✊ **Both fists** | Total freeze — everything holds |
| 🖐️🖐️ **Both hands open** | Silence. The gesture for when something feeds back |

Relative roll is the only thing two IMUs give you that one cannot.

**Not possible:** hands far apart / close together. The BNO08x reports
orientation, not position, so distance between the hands is not measurable —
only relative rotation.

---

## Playing it: a worked example

1. **Sing or play a phrase.** Live hand: tilt for pitch, index bend for
   dynamics.
2. **Hold the live hand's button** to capture it.
3. **The loop hand takes over.** Roll to scrub through your own voice; make a
   fist to freeze it mid-syllable.
4. **Play over the top.** The live hand starts the synth while the loop keeps
   running.
5. **Flick the loop hand** to stutter the voice as punctuation.
6. **Hold the loop hand's button** to overdub a harmony onto the loop.

Each hand is visibly doing different work, which matters for an audience.

---

## Why postures need a hold

A posture fires **once on entry**, and only after being held:

- ✊ Fist and 🤚 Point: **0.35 s**
- 🖐️ Open hand: **1.0 s**

The index finger is also a continuous control, so a volume swell sweeps
through the posture regions constantly. Without the dwell you would trigger
gestures every time you faded in or out.

Open hand needs longer because **a relaxed hand is an open hand** — fading out
passes through it every single time. Measured, a slow fade-out running into a
fade-in dwells about 0.84 s there, so 0.8 s was not enough margin.

Thresholds: a finger counts as **bent above 0.62** and **straight below 0.28**.
The gap between is "playing", not "posturing".

On the live hand, bending the index past the bent threshold always re-opens the
gate — so a stray deactivate can never strand you in silence. You just play and
it comes back.

---

## Without the gloves

Everything is playable from the keyboard or browser. `h` switches which hand
the controls drive.

| Key | Live hand | Loop hand |
| --- | --- | --- |
| `w`/`s` | Tilt — pitch | Tilt — loop speed |
| `a`/`d` | Roll — brightness | Roll — scrub |
| `q`/`e` | Yaw — pan | Yaw — its pan |
| `[`/`]` | Index bend | Index bend |
| `;`/`'` | Middle bend | Middle bend |
| `space` | Drum hit | Slice / stutter |
| `n` | Next scene | Next preset |
| `h` | Switch hand | Switch hand |
| `v` | Record | — |
| `o` | — | Overdub |
| `l` | Live mic | Live mic |
| `m` | Mute | Mute |
| `1`–`7` | Instrument | — |
| `r` / `x` | Reset / quit | Reset / quit |

In the browser the same applies — click **🎹 Live hand** or **🔁 Loop hand**,
then drag the pad and move the sliders.

---

## Quick troubleshooting

| Symptom | Cause |
| --- | --- |
| `flex1 -` in the status line | That channel has not swung 80 ADC counts yet. Bend the finger fully a few times |
| Postures do nothing | Both flex channels must be calibrated — if either shows `-`, no posture can fire |
| Orientation frozen, warning printed | The BNO08x has stalled. The link is fine; check its 3V3 and SDA/SCL |
| Sound from the wrong device | Audio binds when the stream opens. Restart, or pass `--audio-out` |
| Glove not found | `python main.py --scan`. Boards advertise as `MIMU-GLOVE-I` / `-V` |
| Feedback squeal | Wear headphones, or `m` then `l` to kill it |
