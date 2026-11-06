"""MIDI output: the same gestures, sent to a DAW instead of the built-in synth.

This is a *sink*, not a second mapping. `mapping.apply` has already turned the
hand into musical intent — a note, a brightness, a pan, an expression level —
and left it on the synth as target values. This module reads those same values
back out and sends them as MIDI, so there is exactly one place where gesture
becomes music and the two outputs can never drift apart. Everything upstream
(keyboard, browser, BLE glove, demo) works unchanged.

    python main.py --midi                             virtual port for a DAW
    python main.py --midi --midi-port "IAC Driver Bus 1"
    python main.py --ble --web --midi                 glove + browser + MIDI

Notes go out on channel 1 and the punch drum on channel 10. The notes are the
boring half: a gestural instrument lives in the continuous controllers, so
expression, brightness, pan and vibrato stream out as CCs at the control rate.
"""

from __future__ import annotations

import math

PORT_NAME = "Glove"

NOTE_CHANNEL = 0    # MIDI channel 1
DRUM_CHANNEL = 9    # MIDI channel 10 — General MIDI percussion
DRUM_NOTE = 38      # acoustic snare
DRUM_TICKS = 2      # control ticks a punch is held before its note-off

CC_MOD = 1          # vibrato depth (second finger)
CC_PAN = 10
CC_EXPRESSION = 11
CC_BRIGHTNESS = 74  # the standard "filter cutoff" CC

# General MIDI program for each synth instrument, so switching instruments on
# the glove switches the patch downstream too.
PROGRAMS = {
    "saw": 81,      # Lead 2 (sawtooth)
    "organ": 16,    # Drawbar Organ
    "strings": 48,  # String Ensemble 1
    "bell": 14,     # Tubular Bells
    "flute": 73,    # Flute
    "pluck": 24,    # Acoustic Guitar (nylon)
    "guitar": 29,   # Overdriven Guitar
}

# mapping.py's ranges, needed to send the same values back as 0..127.
AMP_FULL = 0.5
CUTOFF_LO, CUTOFF_HI = 200.0, 6000.0


def _hz_to_note(hz: float) -> int:
    """Back to a MIDI note number. mapping.py built the frequency from one in
    the first place, so this round-trips exactly for every note in SCALE."""
    return int(round(69.0 + 12.0 * math.log2(max(hz, 1e-6) / 440.0)))


class MidiOut:
    """Mirrors a GloveSynth's target values out as MIDI."""

    def __init__(self, port_name: str | None = None) -> None:
        try:
            import rtmidi
        except ImportError as exc:  # pragma: no cover - depends on install
            raise SystemExit(
                "--midi needs python-rtmidi:  pip install python-rtmidi"
            ) from exc

        self._midi = rtmidi.MidiOut()
        ports = self._midi.get_ports()
        if port_name:
            hits = [i for i, p in enumerate(ports) if port_name.lower() in p.lower()]
            if not hits:
                listing = "\n".join(f"    {p}" for p in ports) or "    (none)"
                raise SystemExit(
                    f"No MIDI output port matching {port_name!r}. Available:\n{listing}"
                )
            self._midi.open_port(hits[0])
            print(f"\n[MIDI -> {ports[hits[0]]}]")
        else:
            # A virtual port needs nothing configured on the other end: the DAW
            # just sees a new MIDI input appear.
            self._midi.open_virtual_port(PORT_NAME)
            print(
                f"\n[MIDI -> virtual port '{PORT_NAME}' — select it as a MIDI "
                "input in your DAW]"
            )

        self._note: int | None = None
        self._instrument: str | None = None
        self._cc_last: dict[int, int] = {}
        self._drum_ticks = 0

    def update(self, synth) -> None:
        """One control tick. Cheap to call at the control rate: notes only go
        out when the note changes, and a CC only when its 0..127 value does."""
        if self._drum_ticks:
            self._drum_ticks -= 1
            if not self._drum_ticks:
                self._send([0x80 | DRUM_CHANNEL, DRUM_NOTE, 0])

        if synth.instrument != self._instrument:
            self._instrument = synth.instrument
            program = PROGRAMS.get(synth.instrument)
            if program is not None:
                self._send([0xC0 | NOTE_CHANNEL, program])

        # A held note stays held — only a change of target_freq is a new note,
        # the same rule the synth uses. Muting the drone releases it.
        want = _hz_to_note(synth.target_freq) if synth.drone_on else None
        if want != self._note:
            if self._note is not None:
                self._send([0x80 | NOTE_CHANNEL, self._note, 0])
            if want is not None:
                velocity = min(max(int(synth.target_amp / AMP_FULL * 127.0), 1), 127)
                self._send([0x90 | NOTE_CHANNEL, want, velocity])
            self._note = want

        self._cc(CC_EXPRESSION, synth.target_amp / AMP_FULL)
        self._cc(CC_MOD, synth.target_vibrato)
        self._cc(CC_PAN, (synth.target_pan + 1.0) * 0.5)
        self._cc(CC_BRIGHTNESS, math.log(
            min(max(synth.target_cutoff, CUTOFF_LO), CUTOFF_HI) / CUTOFF_LO
        ) / math.log(CUTOFF_HI / CUTOFF_LO))

    def handle_event(self, event: str) -> None:
        """Discrete gestures. Only the punch has a MIDI meaning; the rest are
        modes of the local engine and are ignored here."""
        if event == "punch":
            self._send([0x90 | DRUM_CHANNEL, DRUM_NOTE, 100])
            self._drum_ticks = DRUM_TICKS

    def close(self) -> None:
        if self._note is not None:
            self._send([0x80 | NOTE_CHANNEL, self._note, 0])
            self._note = None
        # All Notes Off + Reset All Controllers, or quitting mid-note leaves
        # the DAW droning with nothing to stop it.
        for channel in (NOTE_CHANNEL, DRUM_CHANNEL):
            self._send([0xB0 | channel, 123, 0])
            self._send([0xB0 | channel, 121, 0])
        self._midi.close_port()

    def _cc(self, cc: int, value: float) -> None:
        v = min(max(int(round(value * 127.0)), 0), 127)
        if self._cc_last.get(cc) != v:
            self._cc_last[cc] = v
            self._send([0xB0 | NOTE_CHANNEL, cc, v])

    def _send(self, message: list[int]) -> None:
        self._midi.send_message(message)
