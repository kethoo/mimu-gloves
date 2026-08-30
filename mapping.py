"""The instrument's "personality": hand state -> sound parameters.

Gesture vocabulary:

  |> rotate wrist (roll)        -> filter cutoff / brightness
  ^v tilt hand up/down (pitch)  -> musical pitch, quantized to the scene's scale
  <> point left/right (yaw)     -> stereo pan
  -- index finger bend (flex)   -> volume / expression
  () fist, both fingers curled  -> activate the sound
  || open hand, both straight   -> deactivate it
  ^  point: index straight,
     middle curled              -> next instrument
  *  wrist flick                -> drum hit
  #  button, short press        -> next scene

Two notes on what the hardware can actually sense.

"Up/down" and "left/right" are tilt and yaw, not translation. The BNO08x
reports orientation only; position would mean double-integrating acceleration,
which drifts into nonsense within seconds. Tilting the hand is the honest
proxy and is what the sensor can support.

Postures come from two flex sensors, which give four states in total, and the
index finger is already busy being the volume control — a swell sweeps it
through the posture regions constantly. So a posture fires only on entry, once,
and only after it has been held steady for POSTURE_HOLD seconds. The dead band
between POSTURE_LO and POSTURE_HI is the hysteresis: values in between are
"playing", not "posturing".

Discrete gesture/button events (see handle_event):
  punch    -> wrist flick: percussive hit; in slice mode, fires a chunk
  scene    -> next scene (instrument + scale + modes)
  record   -> start/stop recording the mic into the voice loop
  overdub  -> start/stop layering a new take on top of the playing loop
  loop     -> voice loop play/pause
  granular -> normal loop <-> granular cloud
  slices   -> punch = drum hit <-> punch = recording slice
  mute     -> drone tone on/off (hear the voice alone)
  instrument:<name> -> switch drone instrument (see synth.INSTRUMENTS)
"""

from __future__ import annotations

import math

from sensors import SensorFrame
from synth import GloveSynth

# Scales as MIDI note numbers, low to high. A scene picks one.
SCALES = {
    "pentatonic": [57, 60, 62, 64, 67, 69, 72, 74, 76, 79, 81],
    "minor":      [57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74],
    "dorian":     [57, 59, 60, 62, 64, 66, 67, 69, 71, 72, 74],
    "whole":      [57, 59, 61, 63, 65, 67, 69, 71, 73, 75, 77],
}

# A scene is a whole performance setup: what it plays, in what key, in what
# mode. The button cycles them, so keep the list short enough to reach the one
# you want mid-performance.
#            name         instrument  scale         granular  slices
SCENES = [
    ("Lead",           "saw",      "pentatonic", False, False),
    ("Cathedral",      "organ",    "minor",      False, False),
    ("Bowed",          "strings",  "dorian",     False, False),
    ("Chimes",         "bell",     "whole",      False, False),
    ("Amp",            "guitar",   "minor",      False, False),
    ("Cloud",          "strings",  "whole",      True,  False),
]

# How far you actually have to move to reach the extremes. A wrist holding
# the board comfortably covers roughly +/-45 deg, not the +/-90 the sensor
# can report, so mapping against 90 wastes most of the musical range.
ROLL_RANGE = 45.0   # degrees of roll from darkest to brightest
PITCH_RANGE = 45.0  # degrees of tilt for the full scale, low to high
YAW_RANGE = 45.0    # degrees of yaw for hard-left to hard-right pan

CUTOFF_LO, CUTOFF_HI = 200.0, 6000.0

# The scale quantizer remembers the step it last committed to, so a hand
# parked on a boundary between two notes does not flutter between them.
# Plain rounding measured 50 note changes a second with only +/-0.3 deg of
# sensor noise — and since a note change re-articulates the envelope, that is
# a machine-gun rattle on the struck instruments, not just a wobbly pitch.
# The hand has to travel 68% of the way to a neighbour before the note moves.
STEP_HYSTERESIS = 0.18

# Volume response to finger bend. `flex` already arrives as 0..1 across the
# sensor's calibrated range, so making it "more sensitive" means changing the
# shape of the response, not the range.
#
# FLEX_CURVE < 1 expands the early part of the bend, where a finger has the
# most control: at 0.6, bending 20% from straight gives ~38% of the travel
# instead of 20%. Lower it for a hair trigger, raise it past 1 to make the
# first part of the bend do less.
FLEX_CURVE = 0.6

# Loudness is perceived roughly logarithmically, so a linear amplitude ramp
# wastes the top half of the bend — going from 0.5 to 1.0 amplitude is only
# 6 dB, which barely reads as a change. Mapping the finger across a dB range
# instead makes the whole travel feel even, and is most of why this now
# responds to a small bend.
VOL_RANGE_DB = 34.0
AMP_MAX = 0.55

POSTURE_HI = 0.62    # bent past this counts as curled
POSTURE_LO = 0.28    # straighter than this counts as extended

# Seconds a posture must hold before it fires. "open" needs much longer than
# the others because a relaxed hand *is* an open hand: fading out with the
# index finger passes through the open posture every single time. Nobody holds
# both fingers flat for the best part of a second while still playing, so the
# long dwell separates "quiet" from "done".
# Measured: a slow fade-out running straight into a fade-in dwells about
# 0.84 s below the threshold, so 0.8 s was not enough margin.
POSTURE_HOLD = {"fist": 0.35, "point": 0.35, "open": 1.00}

_last_step = 0
_scene = 0
_posture: str | None = None       # posture currently committed to
_pending: str | None = None       # posture being held, not yet committed
_pending_since = 0.0


def midi_to_hz(note: float) -> float:
    return 440.0 * 2.0 ** ((note - 69) / 12.0)


def _norm(value: float, limit: float) -> float:
    """Map -limit..+limit onto 0..1, clamped."""
    return min(max((value + limit) / (2.0 * limit), 0.0), 1.0)


def _read_posture(flex: float | None, flex2: float | None) -> str | None:
    """Which of the three postures the hand is in, or None for "playing".

    Anything between the two thresholds is deliberately nothing: that gap is
    where the index finger lives while it is working as the volume control.
    """
    if flex is None or flex2 is None:
        return None
    if flex > POSTURE_HI and flex2 > POSTURE_HI:
        return "fist"
    if flex < POSTURE_LO and flex2 < POSTURE_LO:
        return "open"
    if flex < POSTURE_LO and flex2 > POSTURE_HI:
        return "point"
    return None


def _fire_posture(posture: str, synth: GloveSynth) -> None:
    if posture == "fist":
        synth.set_gate(True)
    elif posture == "open":
        synth.set_gate(False)
    elif posture == "point":
        synth.next_instrument()


def apply_scene(synth: GloveSynth, announce: bool = True) -> None:
    """Install the current scene: instrument, scale and modes together."""
    global _last_step
    name, instrument, scale, granular, slices = SCENES[_scene]
    synth.set_instrument(instrument, announce=False)
    synth.granular_on = granular
    synth.slices_on = slices
    # The new scale may be shorter than the old one.
    _last_step = min(_last_step, len(SCALES[scale]) - 1)
    if announce:
        modes = ", ".join(
            m for m, on in (("granular", granular), ("slices", slices)) if on
        )
        print(
            f"\n[scene {_scene + 1}/{len(SCENES)}: {name} — {instrument}, "
            f"{scale}{', ' + modes if modes else ''}]"
        )


def next_scene(synth: GloveSynth) -> None:
    global _scene
    _scene = (_scene + 1) % len(SCENES)
    apply_scene(synth)


def apply(frame: SensorFrame, synth: GloveSynth) -> None:
    global _last_step, _posture, _pending, _pending_since

    # ---- postures ---------------------------------------------------------
    posture = _read_posture(frame.flex, frame.flex2)
    if posture != _pending:
        _pending, _pending_since = posture, frame.t
    if posture is None:
        _posture = None  # leaving a posture re-arms it
    elif posture != _posture and frame.t - _pending_since >= POSTURE_HOLD[posture]:
        _posture = posture
        _fire_posture(posture, synth)

    # Playing implies wanting sound. Bending the index past the posture
    # threshold always re-opens the gate, so an accidental deactivate can
    # never strand you in silence waiting to remember the recovery gesture —
    # you just play, and it comes back.
    if frame.flex is not None and frame.flex > POSTURE_HI:
        synth.set_gate(True)

    # ---- continuous control ----------------------------------------------
    scale = SCALES[SCENES[_scene][2]]

    # tilt up/down -> index into the scale, with hysteresis so a hand
    # hovering on a boundary holds its note
    pos = _norm(frame.pitch, PITCH_RANGE) * (len(scale) - 1)
    if abs(pos - _last_step) > 0.5 + STEP_HYSTERESIS:
        _last_step = min(max(int(round(pos)), 0), len(scale) - 1)
    synth.target_freq = midi_to_hz(scale[min(_last_step, len(scale) - 1)])

    # rotate wrist -> cutoff, exponential so it feels even to the ear
    roll_u = _norm(frame.roll, ROLL_RANGE)
    cutoff = CUTOFF_LO * math.exp(roll_u * math.log(CUTOFF_HI / CUTOFF_LO))
    # Key-follow. The filter is 4-pole now, so a fixed 200 Hz floor would put
    # the top of the scale two octaves below its own corner and bury it. Keep
    # the corner above the fundamental so "dark" means the same thing at every
    # pitch instead of "silent up high".
    synth.target_cutoff = max(cutoff, synth.target_freq * 1.4)

    # Roll keeps its old second job of sweeping the recorded voice: playback
    # speed in loop mode, scrub position in granular, slice choice in slice
    # mode. Those need a continuous sweep and roll is the natural one for it.
    synth.target_rate = 2.0 ** (2.0 * roll_u - 1.0)
    synth.target_scrub = roll_u

    # point left/right -> pan
    synth.target_pan = 2.0 * _norm(frame.yaw, YAW_RANGE) - 1.0

    # index finger bend -> volume, like a breath controller. Without a flex
    # sensor connected, fall back to movement energy.
    if frame.flex is not None:
        u = min(max(frame.flex, 0.0), 1.0) ** FLEX_CURVE
        synth.target_amp = AMP_MAX * 10.0 ** ((u - 1.0) * VOL_RANGE_DB / 20.0)
    else:
        synth.target_amp = 0.12 + 0.35 * min(frame.motion, 1.0)

    # motion -> echo trails on the live voice: wave your hand, it rings
    synth.target_echo = 0.15 + 0.55 * min(frame.motion, 1.0)

    # Second finger also adds vibrato while it is not holding a posture. It
    # only reads as "point" with the index straight, i.e. at near-zero volume,
    # so the two uses rarely collide in practice.
    synth.target_vibrato = frame.flex2 if frame.flex2 is not None else 0.0


def handle_event(event: str, synth: GloveSynth) -> None:
    """Discrete gestures / button presses (drained from the event queue)."""
    if event == "punch":
        if synth.slices_on:
            synth.trigger_slice()
        else:
            synth.pluck()
    elif event == "scene":
        next_scene(synth)
    elif event == "record":
        synth.toggle_record()
    elif event == "overdub":
        synth.toggle_overdub()
    elif event == "loop":
        synth.toggle_loop()
    elif event == "granular":
        synth.toggle_granular()
    elif event == "slices":
        synth.toggle_slices()
    elif event == "mute":
        synth.toggle_drone()
    elif event == "live":
        synth.toggle_live()
    elif event.startswith("instrument:"):
        synth.set_instrument(event.split(":", 1)[1])
