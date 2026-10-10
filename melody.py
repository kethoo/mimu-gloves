"""Play a MIDI melody through the glove's own instruments.

A song loaded with --song is a waveform: you can scrub it, stretch it and
freeze it, but you cannot change a note of it, because once a track is
mixed there is no "melody" in there to change. A MIDI file is the opposite
— it *is* the notes — so the synth can play it, and the hands can then do
things no audio effect can: transpose it, change the instrument playing it,
pull the tempo around, bend it into a different key.

    python main.py --web --melody tune.mid

The synth is monophonic, so a chord has to become one note. The top note
wins, which is the melody in almost all written music.

Requires:  pip install mido
"""

from __future__ import annotations

import pathlib

# A note the sequencer will play: when it starts, how long it lasts, which
# MIDI note, how hard. Times are seconds from the start of the piece, so
# tempo changes inside the file are already baked in and playback only has
# to compare against a clock.
Note = tuple[float, float, int, float]   # (start, duration, note, velocity)


class Melody:
    """A parsed MIDI file, reduced to one line of notes."""

    def __init__(self, notes: list[Note], name: str) -> None:
        self.notes = notes
        self.name = name

    @property
    def length(self) -> float:
        if not self.notes:
            return 0.0
        start, dur, _n, _v = self.notes[-1]
        return start + dur

    @property
    def span(self) -> tuple[int, int]:
        """Lowest and highest note, for reporting whether it will fit."""
        if not self.notes:
            return (0, 0)
        pitches = [n for _s, _d, n, _v in self.notes]
        return min(pitches), max(pitches)

    def __len__(self) -> int:
        return len(self.notes)


def load(path: str) -> Melody:
    """Read a .mid into a single monophonic line.

    Every track is merged first. Picking one track sounds like the obvious
    thing and is usually wrong: plenty of files put the tune on track 2, or
    split it across several, and a "melody track" is not a thing the format
    actually has.
    """
    try:
        import mido
    except ImportError as exc:
        raise RuntimeError("MIDI files need mido: pip install mido") from exc

    p = pathlib.Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    try:
        mid = mido.MidiFile(str(p))
    except Exception as exc:
        raise RuntimeError(f"{p.name} is not a readable MIDI file: {exc}") from exc

    # mido's merged iteration already resolves tempo changes into seconds.
    sounding: dict[int, tuple[float, float]] = {}   # note -> (start, velocity)
    raw: list[Note] = []
    now = 0.0
    for msg in mid:
        now += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            sounding[msg.note] = (now, msg.velocity / 127.0)
        elif msg.type in ("note_off", "note_on"):
            # A note_on with velocity 0 is a note_off; the format allows both
            # and files in the wild use each.
            started = sounding.pop(msg.note, None)
            if started is not None:
                start, vel = started
                if now > start:
                    raw.append((start, now - start, msg.note, vel))
    for note, (start, vel) in sounding.items():
        # Unterminated notes: give them to the end of the file rather than
        # dropping them, which would silently lose the final note.
        raw.append((start, max(mid.length - start, 0.1), note, vel))

    if not raw:
        raise RuntimeError(f"{p.name} contains no notes")

    raw.sort(key=lambda n: (n[0], -n[2]))
    notes = _monophonic(raw)
    mel = Melody(notes, p.stem)
    lo, hi = mel.span
    print(f"[melody] {mel.name}: {len(notes)} notes, {mel.length:.1f}s, "
          f"range {_name(lo)}..{_name(hi)}")
    return mel


def _monophonic(raw: list[Note]) -> list[Note]:
    """Reduce overlapping notes to one line, keeping the top voice.

    Written for a monophonic synth, but it is also what makes a piano piece
    recognisable: the top of the chord is the tune, and playing the bottom
    of each chord instead sounds like a different piece entirely.
    """
    out: list[Note] = []
    for start, dur, note, vel in raw:
        if out:
            p_start, p_dur, p_note, p_vel = out[-1]
            if start < p_start + p_dur - 1e-6:
                # Overlaps the previous note.
                if note <= p_note:
                    continue                     # lower voice: drop it
                out[-1] = (p_start, start - p_start, p_note, p_vel)
                if start - p_start < 1e-3:
                    out.pop()                    # the old note never sounded
        out.append((start, dur, note, vel))
    return [n for n in out if n[1] > 1e-3]


_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _name(note: int) -> str:
    return f"{_NAMES[int(note) % 12]}{int(note) // 12 - 1}"


class Sequencer:
    """Plays a Melody into a GloveSynth, under the hands' control.

    Owns the pitch while it runs: the live hand's tilt stops choosing a note
    from the scale and transposes the whole piece instead, which is the one
    reading of "tilt" that still makes sense when the tune is already
    written. Everything else the hands do — filter, pan, volume, instrument,
    effects — is unchanged, because none of it is about which note sounds.
    """

    # Tilt covers two octaves, an octave either way. Wider than that and
    # small hand movements jump the piece into a register where the
    # instrument's envelope and the filter stop sounding like themselves.
    TRANSPOSE_RANGE = 12

    def __init__(self, melody: Melody) -> None:
        self.melody = melody
        self.playing = True
        self.loop = True
        self.rate = 1.0            # tempo multiplier, driven by the loop hand
        self.transpose = 0         # semitones, driven by live-hand tilt
        self.pos = 0.0             # seconds into the piece
        self._i = 0                # index of the next note to fire
        self._note: int | None = None
        self._note_ends = 0.0

    @property
    def current(self) -> str:
        """What is sounding, for the panel."""
        if self._note is None:
            return "—"
        name = _name(self._note + self.transpose)
        return name if not self.transpose else f"{name} ({self.transpose:+d})"

    @property
    def progress(self) -> float:
        return 0.0 if self.melody.length <= 0 else self.pos / self.melody.length

    def restart(self) -> None:
        self.pos = 0.0
        self._i = 0
        self._note = None
        self._note_ends = 0.0

    def update(self, synth, dt: float) -> None:
        """Advance by dt seconds and play whatever falls due.

        Called from the control loop at ~100 Hz, so dt is about 10 ms: fine
        for note timing, which only has to be tight to a few milliseconds to
        sound exact.
        """
        if not self.playing or not self.melody.notes:
            return
        self.pos += dt * self.rate
        if self.pos >= self.melody.length:
            if not self.loop:
                self.playing = False
                synth.set_gate(False)
                return
            self.restart()

        from mapping import midi_to_hz

        fired = False
        while self._i < len(self.melody.notes):
            start, dur, note, vel = self.melody.notes[self._i]
            if start > self.pos:
                break
            self._i += 1
            self._note = note
            self._note_ends = start + dur
            synth.target_freq = midi_to_hz(note + self.transpose)
            synth.target_amp = 0.35 + 0.65 * vel
            synth.set_gate(True)
            synth._note_on()
            fired = True

        # Release at the end of the note, so rests are actually silent and
        # a staccato passage does not smear into one continuous tone.
        if not fired and self._note is not None and self.pos >= self._note_ends:
            self._note = None
            synth.set_gate(False)
