"""The voice hand's personality: hand state -> live voice processing.

The second glove. Where `mapping.py` plays an instrument, this one shapes the
microphone in real time. The two hands drive **disjoint** parameter sets —
nothing here touches pitch, brightness, pan or volume of the instrument, and
nothing in `mapping.py` touches the voice chain. That is the whole point of a
second hand: two independent controllers, one synth.

Gesture vocabulary:

  ^v tilt hand up/down (pitch)  -> voice pitch (octave down .. octave up)
  |> rotate wrist (roll)        -> reverb amount
  <> point left/right (yaw)     -> voice stereo pan
  -- index finger bend (flex)   -> voice volume
  () fist, both fingers curled  -> voice effects ON
  || open hand, both straight   -> normal voice (fully dry)
  ^  point: index straight,
     middle curled              -> toggle delay
  *  wrist flick                -> stutter: freeze and repeat what you just said
  #  button, short press        -> next voice preset
  #  button, hold               -> record a take on the glove's own mic

The glove's microphone reaches the effects as recorded takes, not as a live
stream, and that is a hardware limit rather than a choice: 16 kHz mono needs
32 kB/s where the link measures 30 kB/s with the sensor stream already off,
and the round trip would be 100 ms+ — enough delay that hearing yourself
actively disrupts speaking. So hold the button to record a phrase on the
glove, and the voice hand then shapes it in real time. `voice_from_loop`
selects which source the chain processes.

Either way, wear headphones: processed output re-entering a live microphone
is a feedback loop.
"""

from __future__ import annotations

from sensors import SensorFrame
from synth import GloveSynth

# Reuse the instrument hand's travel ranges so both gloves feel the same.
from mapping import (
    POSTURE_HI,
    POSTURE_HOLD,
    POSTURE_LO,
    PITCH_RANGE,
    ROLL_RANGE,
    YAW_RANGE,
    FLEX_CURVE,
    HandState,
    _norm,
    _read_posture,
)

# Voice pitch travel, in semitones either side of natural. An octave each way
# is the usable limit of the granular shifter before artefacts dominate.
PITCH_SEMITONES = 12.0

# Volume curve. Loudness is perceived logarithmically, so the finger maps
# across a dB range rather than a linear amplitude — the same reasoning as the
# instrument hand's volume, and the same FLEX_CURVE shaping.
VOICE_RANGE_DB = 30.0
VOICE_MAX = 0.9

# Presets cycled by the button: (name, reverb, delay on, pitch offset in
# semitones). The pitch offset is added to whatever tilt is asking for, so a
# preset colours the hand rather than overriding it.
PRESETS = [
    ("Dry",      0.05, False,  0.0),
    ("Hall",     0.75, False,  0.0),
    ("Slap",     0.25, True,   0.0),
    ("Cavern",   0.95, True,   0.0),
    ("Chipmunk", 0.20, False, +7.0),
    ("Demon",    0.45, True,  -7.0),
]


class VoiceState(HandState):
    """Per-hand memory for the voice glove.

    Subclasses HandState so both hands carry the same posture/dwell fields and
    `_read_posture` works unchanged; `preset` is the only addition.
    """

    def __init__(self) -> None:
        super().__init__()
        self.preset = 0
        self.preset_semitones = 0.0


_default = VoiceState()


def _fire_posture(posture: str, synth: GloveSynth) -> None:
    if posture == "fist":
        synth.set_voice_fx(True)
    elif posture == "open":
        synth.set_voice_fx(False)      # normal voice: fully dry
    elif posture == "point":
        synth.toggle_voice_delay()


def apply_preset(synth: GloveSynth, state: VoiceState | None = None,
                 announce: bool = True) -> None:
    st = state or _default
    name, reverb, delay, semis = PRESETS[st.preset]
    synth.target_voice_reverb = reverb
    synth.voice_delay_on = delay
    st.preset_semitones = semis
    if announce:
        print(f"\n[voice preset {st.preset + 1}/{len(PRESETS)}: {name}]")


def next_preset(synth: GloveSynth, state: VoiceState | None = None) -> None:
    st = state or _default
    st.preset = (st.preset + 1) % len(PRESETS)
    apply_preset(synth, st)


def apply(frame: SensorFrame, synth: GloveSynth,
          state: VoiceState | None = None) -> None:
    st = state or _default

    # ---- postures ---------------------------------------------------------
    # Same dwell/hysteresis rules as the instrument hand: a posture fires once
    # on entry, and only after being held, because the index finger is also
    # the volume control and sweeps through the posture regions constantly.
    posture = _read_posture(frame.flex, frame.flex2)
    if posture != st.pending:
        st.pending, st.pending_since = posture, frame.t
    if posture is None:
        st.posture = None
    elif posture != st.posture and frame.t - st.pending_since >= POSTURE_HOLD[posture]:
        st.posture = posture
        _fire_posture(posture, synth)

    # ---- continuous control ----------------------------------------------
    # Tilt -> voice pitch. Continuous, not quantized: a voice does not step
    # between scale degrees, and a sliding formant shift is the expressive
    # part. Exponential in semitones so it feels even across the travel.
    semis = (2.0 * _norm(frame.pitch, PITCH_RANGE) - 1.0) * PITCH_SEMITONES
    synth.target_voice_pitch = 2.0 ** ((semis + st.preset_semitones) / 12.0)

    # Roll -> reverb amount.
    synth.target_voice_reverb = _norm(frame.roll, ROLL_RANGE)

    # Yaw -> voice pan, independent of where the instrument sits.
    synth.target_voice_pan = 2.0 * _norm(frame.yaw, YAW_RANGE) - 1.0

    # Index bend -> voice volume, on the same dB curve the instrument uses.
    if frame.flex is not None:
        u = min(max(frame.flex, 0.0), 1.0) ** FLEX_CURVE
        synth.target_voice_volume = VOICE_MAX * 10.0 ** ((u - 1.0) * VOICE_RANGE_DB / 20.0)
    else:
        # No flex sensor on this hand: a fixed, audible level beats guessing.
        synth.target_voice_volume = 0.6

    # Motion -> how long the delay repeats ring, shared with the instrument
    # hand's echo target. Waving either hand lengthens the trails.
    synth.target_echo = 0.15 + 0.55 * min(frame.motion, 1.0)


def handle_event(event: str, synth: GloveSynth,
                 state: VoiceState | None = None) -> None:
    """Discrete gestures from the voice glove."""
    st = state or _default
    if event == "punch":
        synth.trigger_stutter()        # wrist flick: freeze and repeat
    elif event == "scene":
        next_preset(synth, st)         # short button press
    elif event == "record":
        synth.toggle_record()
    elif event == "live":
        synth.toggle_live()
    elif event == "loop":
        synth.toggle_voice_source()   # glove's own mic <-> laptop mic
