"""The LOOP hand: everything about time and space.

The second of two deliberately asymmetric hands. `mapping.py` makes sound —
pitch, brightness, dynamics — and never touches an effect. This hand shapes
what has already been captured and never plays a note.

That asymmetry is the whole point. An earlier version gave both hands the same
vocabulary pointed at two sound sources (tilt=pitch, yaw=pan, flex=volume on
each), which is one instrument played twice: symmetric, duplicative, and dull
to play. The test it failed was "could you swap the hands and have it still
make sense?" Here it fails the other way round, which is correct — scrubbing a
recording is meaningless on the hand that is playing live.

There is exactly one of each parameter in the instrument. One filter, owned by
the live hand. One reverb, one scrub, one loop speed, owned by this one.

Gesture vocabulary:

  |> rotate wrist (roll)        -> scrub position through the recording
  ^v tilt hand up/down (pitch)  -> loop speed, and pitch when shaping the mic
  <> point left/right (yaw)     -> where the material sits in the stereo field
  -- index finger bend (flex)   -> blend between live and looped
  ~~ middle finger bend         -> reverb amount
  >> motion                     -> delay feedback: wave and the repeats ring on
  () fist, both fingers curled  -> FREEZE: hold this moment, time stops
  || open hand, both straight   -> release it
  ^  point: index straight,
     middle curled              -> cycle loop mode: normal / granular / slices
  *  wrist flick                -> fire a slice, or stutter
  #  button, short press        -> next preset
  #  button, hold               -> overdub another layer

Before anything has been recorded this hand shapes the **live microphone**
instead, so it is never sitting idle — it is always the "space and time" hand,
it simply has more to do once material exists. The synth routes this
automatically.

Wear headphones when the microphone is live: processed output re-entering it
is a feedback loop.
"""

from __future__ import annotations

from sensors import SensorFrame
from synth import GloveSynth

# Reuse the live hand's travel ranges so both gloves feel the same.
from mapping import (
    FLEX_CURVE,
    PITCH_RANGE,
    POSTURE_HOLD,
    ROLL_RANGE,
    YAW_RANGE,
    HandState,
    _norm,
    _read_posture,
)

# Loop speed travel. Half speed to double, exponential so the midpoint is
# unity and the two directions feel symmetric.
RATE_OCTAVES = 1.0

# Blend curve. Loudness is perceived logarithmically, so the finger maps
# across a dB range rather than a linear amplitude — same reasoning as the
# live hand's volume, and the same FLEX_CURVE shaping.
BLEND_RANGE_DB = 30.0
BLEND_MAX = 0.9

# Presets cycled by the button: (name, reverb, delay on, pitch offset in
# semitones). The offset is added to whatever tilt is asking for, so a preset
# colours the hand rather than overriding it.
PRESETS = [
    ("Dry",      0.05, False,  0.0),
    ("Hall",     0.75, False,  0.0),
    ("Slap",     0.25, True,   0.0),
    ("Cavern",   0.95, True,   0.0),
    ("Chipmunk", 0.20, False, +7.0),
    ("Demon",    0.45, True,  -7.0),
]


class LoopState(HandState):
    """Per-hand memory for the loop glove.

    Subclasses HandState so both hands carry the same posture/dwell fields and
    `_read_posture` works unchanged; `preset` is the only addition.
    """

    def __init__(self) -> None:
        super().__init__()
        self.preset = 0
        self.preset_semitones = 0.0


_default = LoopState()


def _fire_posture(posture: str, synth: GloveSynth) -> None:
    if posture == "fist":
        synth.set_freeze(True)       # hold this moment
    elif posture == "open":
        synth.set_freeze(False)      # let it run again
    elif posture == "point":
        synth.cycle_loop_mode()      # normal -> granular -> slices


def apply_preset(synth: GloveSynth, state: LoopState | None = None,
                 announce: bool = True) -> None:
    st = state or _default
    name, reverb, delay, semis = PRESETS[st.preset]
    synth.target_voice_reverb = reverb
    synth.voice_delay_on = delay
    st.preset_semitones = semis
    if announce:
        print(f"\n[loop preset {st.preset + 1}/{len(PRESETS)}: {name}]")


def next_preset(synth: GloveSynth, state: LoopState | None = None) -> None:
    st = state or _default
    st.preset = (st.preset + 1) % len(PRESETS)
    apply_preset(synth, st)


def apply(frame: SensorFrame, synth: GloveSynth,
          state: LoopState | None = None) -> None:
    st = state or _default

    # ---- postures ---------------------------------------------------------
    # Same dwell/hysteresis rules as the live hand: a posture fires once on
    # entry and only after being held, because the index finger is also a
    # continuous control and sweeps through the posture regions constantly.
    posture = _read_posture(frame.flex, frame.flex2)
    if posture != st.pending:
        st.pending, st.pending_since = posture, frame.t
    if posture is None:
        st.posture = None
    elif posture != st.posture and frame.t - st.pending_since >= POSTURE_HOLD[posture]:
        st.posture = posture
        _fire_posture(posture, synth)

    # ---- continuous control ----------------------------------------------
    # Roll -> scrub. The signature gesture of this hand: in granular mode time
    # is frozen and rolling the wrist walks the playhead through the
    # recording. It also picks which chunk a flick fires in slice mode.
    synth.target_scrub = _norm(frame.roll, ROLL_RANGE)

    # Tilt -> loop speed (which pitches it, as varispeed does), and the same
    # gesture pitches the microphone when there is no loop to play. Only one
    # of the two is ever audible, so this is one control, not two.
    u = 2.0 * _norm(frame.pitch, PITCH_RANGE) - 1.0
    synth.target_rate = 2.0 ** (RATE_OCTAVES * u)
    synth.target_voice_pitch = synth.target_rate

    # Yaw -> where the captured material sits, independent of the live hand's
    # pan for the instrument.
    synth.target_voice_pan = 2.0 * _norm(frame.yaw, YAW_RANGE) - 1.0

    # Index bend -> blend between live and looped, on the same dB curve the
    # live hand uses for its own volume.
    if frame.flex is not None:
        b = min(max(frame.flex, 0.0), 1.0) ** FLEX_CURVE
        synth.target_voice_volume = BLEND_MAX * 10.0 ** ((b - 1.0) * BLEND_RANGE_DB / 20.0)
    else:
        # No flex sensor on this hand: a fixed, audible level beats guessing.
        synth.target_voice_volume = 0.6

    # Middle bend -> reverb. The one reverb in the instrument lives here.
    if frame.flex2 is not None:
        synth.target_voice_reverb = min(max(frame.flex2, 0.0), 1.0)

    # Motion -> how long the delay repeats ring. Wave and the trails stretch.
    synth.target_echo = 0.15 + 0.55 * min(frame.motion, 1.0)


def handle_event(event: str, synth: GloveSynth,
                 state: LoopState | None = None) -> None:
    """Discrete gestures from the loop glove."""
    st = state or _default
    if event == "punch":
        # Wrist flick: fire a chunk in slice mode, otherwise stutter.
        if synth.slices_on:
            synth.trigger_slice()
        else:
            synth.trigger_stutter()
    elif event == "scene":
        next_preset(synth, st)          # short button press
    elif event == "record":
        synth.toggle_overdub()          # this hand layers rather than captures
    elif event == "live":
        synth.toggle_live()
    elif event == "loop":
        synth.toggle_loop()
