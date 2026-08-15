"""The instrument's "personality": hand state -> sound parameters.

This is the file you will tweak the most. Current mapping:

  roll   -> musical pitch (quantized to A-minor pentatonic, theremin-style)
            ...and voice-loop speed (chipmunk right, slow-mo left)
            ...in granular mode: scrub position through the frozen voice
            ...in slice mode: which of the 4 slices a punch fires
  pitch  -> brightness (filter cutoff): hand up = bright, hand down = dark
            (shapes the voice too — it runs through the same filter)
  yaw    -> stereo pan: point left = sound left
  flex   -> volume/expression: bend the finger to swell the note
  motion -> volume swell, but only when no flex sensor is connected

Discrete gesture/button events (see handle_event):
  punch    -> percussive hit; in slice mode, fires a chunk of the recording
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

# A-minor pentatonic across two octaves (MIDI note numbers).
SCALE = [57, 60, 62, 64, 67, 69, 72, 74, 76, 79, 81]

# How far you actually have to move to reach the extremes. A wrist holding
# the board comfortably covers roughly +/-45 deg, not the +/-90 the sensor
# can report, so mapping against 90 wastes most of the musical range.
ROLL_RANGE = 45.0   # degrees of roll for the full scale, low to high
PITCH_RANGE = 40.0  # degrees of tilt from darkest to brightest
YAW_RANGE = 45.0    # degrees of yaw for hard-left to hard-right pan


def midi_to_hz(note: float) -> float:
    return 440.0 * 2.0 ** ((note - 69) / 12.0)


def _norm(value: float, limit: float) -> float:
    """Map -limit..+limit onto 0..1, clamped."""
    return min(max((value + limit) / (2.0 * limit), 0.0), 1.0)


def apply(frame: SensorFrame, synth: GloveSynth) -> None:
    # roll -> index into the scale (low notes left, high right)
    roll_u = _norm(frame.roll, ROLL_RANGE)
    idx = min(max(int(round(roll_u * (len(SCALE) - 1))), 0), len(SCALE) - 1)
    synth.target_freq = midi_to_hz(SCALE[idx])

    # roll also drives voice-loop speed: 0.5x at full left, 2x at full right
    synth.target_rate = 2.0 ** (2.0 * roll_u - 1.0)
    # ...and the 0..1 scrub/slice selector used by granular and slice modes
    synth.target_scrub = roll_u

    # tilt -> cutoff 200..6000 Hz, exponential so it feels even to the ear
    u = _norm(frame.pitch, PITCH_RANGE)
    synth.target_cutoff = 200.0 * math.exp(u * math.log(6000.0 / 200.0))

    # yaw -> pan
    synth.target_pan = 2.0 * _norm(frame.yaw, YAW_RANGE) - 1.0

    # Volume/expression. With a working flex sensor the finger controls it
    # directly, like a breath controller — the most expressive option, and it
    # frees the wrist for pitch. Without one, fall back to movement energy.
    if frame.flex is not None:
        synth.target_amp = 0.05 + 0.45 * frame.flex
    else:
        synth.target_amp = 0.12 + 0.35 * min(frame.motion, 1.0)

    # motion -> echo trails on the live voice: wave your hand, it rings
    synth.target_echo = 0.15 + 0.55 * min(frame.motion, 1.0)


def handle_event(event: str, synth: GloveSynth) -> None:
    """Discrete gestures / button presses (drained from the event queue)."""
    if event == "punch":
        if synth.slices_on:
            synth.trigger_slice()
        else:
            synth.pluck()
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
