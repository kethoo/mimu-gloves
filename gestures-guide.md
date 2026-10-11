# Gestures Guide

How to play the gloves. For *why* the mapping is shaped this way, see
`renewed-design.md`.

The two hands are deliberately asymmetric. One **makes sound**; the other
**shapes what has already been captured**. Neither does the other's job, and
there is exactly one of each parameter in the instrument — one filter, one
reverb, one scrub, one pitch — each owned by exactly one hand.

**Which glove goes on which hand**

| | hand | board | carries |
|---|---|---|---|
| **Live hand** | left | `MIMU-GLOVE-I` | flex sensors + IMU, **microphone, button, LED** |
| **Loop hand** | right | `MIMU-GLOVE-V` | flex sensors + IMU |

That is the default, not a fact about the code. Which glove you wear on
which hand is a property of your build, so it is an option:

```bash
python main.py --ble --voice-glove --web --live-side right
```

`--live-side` decides two things at once: which card the panel draws on the
left, and which board has its orientation inverted — the board worn on the
left hand is mounted the other way round, and without that inversion
rolling that wrist moves the on-screen hand backwards. Override the
inversion on its own with `--flip-hand none|live|loop` if you remount a
board.

The microphone, button and LED are wired on one glove only, the one worn on
the left, so recording and the scene button are left-hand jobs. The panel
marks that card with a **mic · button** badge.

On both gloves the flex sensors are on the **index** and **middle** fingers,
and those are the two fingers the on-screen hand bends.

---

## Starting up

```bash
# both gloves
python main.py --ble --voice-glove --web

# with a record to work on (a file, or a link)
python main.py --ble --voice-glove --web --song "~/Music/track.mp3#60-90"

# with a written tune to play through the instruments
python main.py --ble --voice-glove --web --melody tune.mid

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

Everything about the note happening right now. It owns pitch, and it never
touches the loop hand's time and space controls — with one deliberate
exception, noted below: with a record loaded it sweeps a filter over it,
because otherwise this hand has nothing to do with someone else's recording.

| Gesture | Controls | Notes |
| --- | --- | --- |
| ↕️ **Tilt up/down** | Musical pitch | Quantized to the scene's scale. ±45° covers the full range |
| ↪️ **Rotate wrist (roll)** | Brightness | Filter cutoff, 200 Hz → 6 kHz, follows the note so low tilt never buries high pitches. **With a record loaded it sweeps a lowpass over the record instead** (200 Hz → 18 kHz) — never over a live mic |
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
| ↪️ **Rotate wrist (roll)** | **Seek** through the material | The signature gesture. Moving the wrist jumps the playhead; holding it still lets the track run on from there. Works in every mode — it used to do nothing outside granular |
| ↕️ **Tilt up/down** | Loop speed **and** pitch | ±1 octave varispeed, like a turntable. Also pitches the mic when there is no loop |
| 👈 **Point left/right** | Where the captured material sits | Independent of the live hand's pan |
| ◀ **`j` — reverse** | Plays backwards | Keyboard/UI only, no glove gesture yet |
| 🎚 **`k` — pitch only** | Tilt transposes, tempo stays 1× | Off, tilt moves both together. Keyboard/UI only |
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
| **Relative roll** — twist the hands oppositely | Stereo width. Hands level is normal stereo; twisting one way widens, the other collapses toward mono |
| ✊✊ **Both fists** | Total freeze — everything holds |
| 🖐️🖐️ **Both hands open** | Silence. The gesture for when something feeds back |

Relative roll is the only thing two IMUs give you that one cannot.

**Not possible:** hands far apart / close together. The BNO08x reports
orientation, not position, so distance between the hands is not measurable —
only relative rotation.

---

## 🎵 Working on a loaded track (`--song`, or a link in the panel)

Paste a path or a link into the **Song** box, or start with
`--song <path-or-link>`. Add `#60-90` to take a 20–30 second window: scrubbing
a whole four-minute track means one wrist rotation has to cover four minutes.

Loading a record **mutes the synth**, so your hands shape the record instead of
playing over it. Press `m` to bring the instrument back and solo on top.

**The loop hand owns the record.**

| Gesture | Effect |
| --- | --- |
| ↪️ **Roll** | Scrub through the track |
| ↕️ **Tilt** | Speed and pitch together — or pitch only with `k` |
| 👈 **Yaw** | Pan |
| 🤏 **Index bend** | Volume |
| **Middle bend** | Reverb |
| **Move faster** | Echo depth |
| ✊ **Fist** | Freeze on this moment |
| 🤚 **Point** | normal → granular → slices |
| 💥 **Wrist flick** | Fire the slice under your hand, or stutter |
| `j` | Reverse |
| `k` | Pitch without tempo |
| 👆 **Button tap** | Next preset — Dry, Hall, Slap, Cavern, Chipmunk, Demon |

**The live hand gets exactly one job:** roll sweeps a lowpass over the record,
200 Hz to 18 kHz. Its tilt, yaw and index bend do nothing to the track — they
still drive the synth, for when you un-mute it to play along.

**Together:** relative roll still sets the record's stereo width, and both
fists still freeze everything.

**What you cannot do.** A mixed recording is one waveform with everything
tangled together, so there is no way to change its notes, lift out its melody
or mute its drums. That is not a missing feature; the information is not in
the file. For note-level work, load a `.mid` instead.

---

## 🎼 Playing a written tune (`--melody`, or a `.mid` in the same box)

A MIDI file *is* the notes, so the synth plays it and the hands shape the
performance. The same Song box takes either — the file extension decides.

| Gesture | Effect |
| --- | --- |
| ↕️ **Live hand tilt** | **Transposes the piece**, ±1 octave — it no longer picks a note from the scale |
| ↪️ **Live hand roll** | Filter cutoff |
| 👈 **Live hand yaw** / 🤏 **index** | Pan / volume |
| 🤚 **Live hand point** | Changes which instrument plays the tune |
| ↕️ **Loop hand tilt** | **Tempo** |

Tilt is the only gesture that changes meaning. Picking a note from a scale
means nothing when the notes are already written, so it transposes instead.
Everything else is untouched, because filter, pan and volume are about *how* a
note sounds rather than *which* one.

The synth is monophonic, so a chord plays its top note — which is the tune in
almost all written music. Every track is merged first: files put the melody on
track 2, or split it across several, and MIDI has no "melody track".

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
| `m` | Instrument on/off | Instrument on/off |
| `c` | Recalibrate flex | Recalibrate flex |
| `j` | — | Reverse the loop |
| `k` | — | Pitch without tempo |
| `g` / `b` | Granular / slices | Granular / slices |
| `p` | Loop play/pause | Loop play/pause |
| `1`–`7` | Instrument | — |
| `r` / `x` | Reset / quit | Reset / quit |

Every one of these is also a button in the browser panel, including **eject**
for a loaded record and **↻ recalibrate flex**.

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
| A loaded song keeps playing after reloading the page | By design — the sound is in the python process, the page is only a window onto it. Use **eject**, `p` to pause, or Ctrl+C |
| The synth drones over a loaded record | Loading a record mutes it; `m` toggles. If it came back on its own you are on an old build |
| A finger's bend stops tracking | Press `c`, then bend each finger fully a few times |
