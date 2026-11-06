"""Turn a passage of a record into something you can cover on the gloves.

    python -m glove.cover track.mp3 --start 160 --seconds 16.5 --name "Posthumous"
    python main.py --ble --web --cover covers/posthumous.json

`--song` puts a record in the loop buffer: you can scrub it, freeze it and
slice it, but you are still playing *the recording*. This is the other
thing, and the one a cover actually needs — it **measures** the passage and
throws the audio away:

  * the pulse, from the onset envelope
  * the bass line, from a harmonic-sum pitch track low down
  * what sounds above each bass note, so the chords can be named
  * the lead line, from the same track higher up
  * which of the glove's own scales those notes fall in

Out of that comes a score: a backing part rendered from scratch, a chart of
what to play, and a scale lock so the hand cannot land on a wrong note. At
play time not one sample of the record is in the signal path. That is the
difference between a cover and a remix, and it is the whole point of doing
it this way.

What it does not do is play the tune for you. It works out the notes, sets
the key, lays down the vamp, and then it is your hand.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import numpy as np

SAMPLE_RATE = 44100
NAMES = "C C# D D# E F F# G G# A A# B".split()

# Where to look for each part. A mix is not separable, but it is layered:
# the bass and the lead almost never share a register, so band-limiting the
# pitch tracker is most of what source separation would have bought us.
BASS_BAND = (41.0, 175.0)      # E1 to F3
LEAD_BAND = (220.0, 900.0)     # A3 to A5
COLOUR_BAND = (200.0, 2000.0)  # where a chord's identity actually lives

N_FFT = 8192
HOP = 1024


def note_name(midi: float) -> str:
    m = int(round(midi))
    return f"{NAMES[m % 12]}{m // 12 - 1}"


def _spectrogram(x: np.ndarray, n=4096, hop=512):
    win = np.hanning(n)
    frames = max((len(x) - n) // hop, 1)
    out = np.empty((frames, n // 2 + 1))
    for i in range(frames):
        out[i] = np.abs(np.fft.rfft(x[i * hop:i * hop + n] * win))
    return out, hop


def pulse(x: np.ndarray, rate: int) -> tuple[float, float]:
    """The beat, as (BPM, seconds per pulse).

    Spectral flux, then autocorrelation: the lag that best explains the
    pattern of attacks. Autocorrelation is famously octave-blind — a track
    is just as self-similar at half and double the tempo — so the winning
    lag is checked against the one below it, and the faster reading is kept
    when it is nearly as strong. The bass here plays the eighths; locking to
    the half-speed reading would put the vamp on every other note.
    """
    spec, hop = _spectrogram(x)
    flux = np.maximum(np.diff(spec, axis=0), 0.0).sum(axis=1)
    flux -= flux.mean()
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    fps = rate / hop
    lo, hi = int(fps * 60 / 190), int(fps * 60 / 55)
    hi = min(hi, len(ac) - 1)
    lag = lo + int(np.argmax(ac[lo:hi]))
    half = lag // 2
    if half >= lo and ac[half] > 0.7 * ac[lag]:
        lag = half
    secs = lag / fps
    return 60.0 / secs, secs


def pitch_track(x: np.ndarray, rate: int, band: tuple[float, float],
                harmonics=5) -> list[tuple[float, int, float]]:
    """A pitch per frame, by harmonic sum.

    Each candidate semitone is scored by the energy at its first few
    harmonics, weighted 1/h. Autocorrelation and plain peak-picking both
    lose to the loudest thing in the mix; summing harmonics instead rewards
    a candidate for being a *fundamental*, which is what makes this work on
    a finished master rather than on an isolated stem.
    """
    flo, fhi = band
    win = np.hanning(N_FFT)
    cand = np.arange(int(np.ceil(12 * np.log2(flo / 440.0) + 69)),
                     int(12 * np.log2(fhi / 440.0) + 69) + 1)
    hz = 440.0 * 2.0 ** ((cand - 69) / 12.0)
    bins = [np.clip((hz * h / (rate / N_FFT)).astype(int), 0, N_FFT // 2)
            for h in range(1, harmonics + 1)]
    out = []
    for i in range(0, max(len(x) - N_FFT, 1), HOP):
        frame = x[i:i + N_FFT]
        if len(frame) < N_FFT:
            break
        mag = np.abs(np.fft.rfft(frame * win))
        score = np.zeros(len(cand))
        for h, idx in enumerate(bins, start=1):
            score += mag[idx] / h
        j = int(np.argmax(score))
        out.append((i / rate, int(cand[j]), float(score[j])))
    return out


def to_notes(track, min_secs=0.09) -> list[tuple[float, int, float]]:
    """Runs of the same pitch become notes: (start, midi, duration).

    A frame-by-frame track is not a melody — it is a staircase with grit on
    it. Anything shorter than a grace note is tracker noise and goes.
    """
    frame = HOP / SAMPLE_RATE
    runs, cur = [], None
    for t, m, _s in track:
        if cur is not None and cur[1] == m:
            cur[2] = t
        else:
            if cur is not None:
                runs.append(cur)
            cur = [t, m, t]
    if cur is not None:
        runs.append(cur)
    return [(st, m, en - st + frame) for st, m, en in runs
            if en - st + frame >= min_secs]


def chord_blocks(bass, gap=0.45):
    """Group the bass into chord blocks: a new block when the root changes.

    `gap` forgives a single stray frame mid-block. A pitch tracker on a
    distorted bass will report the fifth once in a while, and splitting the
    vamp in two every time it does would make the chart unreadable.
    """
    blocks = []
    for st, m, dur in bass:
        if blocks and blocks[-1]["root"] == m and st - blocks[-1]["end"] < gap:
            blocks[-1]["end"] = st + dur
            blocks[-1]["hits"].append(round(st, 3))
        elif blocks and st - blocks[-1]["end"] < 0.12 and dur < 0.2:
            blocks[-1]["end"] = st + dur      # a stray frame, not a change
        else:
            blocks.append({"root": m, "at": st, "end": st + dur,
                           "hits": [round(st, 3)]})
    return [b for b in blocks if b["end"] - b["at"] > 0.5]


def colour(x: np.ndarray, rate: int, a: float, b: float) -> np.ndarray:
    """Pitch-class energy above the bass: what the chord actually is.

    Taken from COLOUR_BAND rather than the whole spectrum, because the bass
    fundamental and its harmonics would otherwise vote for their own root
    and every block would come back major.
    """
    seg = x[int(a * rate):int(b * rate)]
    win = np.hanning(N_FFT)
    freq = np.fft.rfftfreq(N_FFT, 1.0 / rate)
    k = (freq > COLOUR_BAND[0]) & (freq < COLOUR_BAND[1])
    cls = np.round(69 + 12 * np.log2(freq[k] / 440.0)).astype(int) % 12
    acc = np.zeros(12)
    for i in range(0, max(len(seg) - N_FFT, 1), N_FFT // 2):
        frame = seg[i:i + N_FFT]
        if len(frame) < N_FFT:
            break
        np.add.at(acc, cls, np.abs(np.fft.rfft(frame * win))[k])
    return acc / (acc.max() or 1.0)


def name_chord(root: int, col: np.ndarray) -> tuple[str, list[int]]:
    """Name the triad over a root from the energy above it, then say which
    notes to actually voice. Only the third is decided by measurement — the
    fifth is assumed, because a chord without one is rare and a tracker
    confidently reporting no fifth is not."""
    r = root % 12
    major, minor = col[(r + 4) % 12], col[(r + 3) % 12]
    third = 4 if major >= minor else 3
    quality = "" if third == 4 else "m"
    extras = []
    for semi, mark in ((10, "7"), (9, "6"), (2, "9")):
        if col[(r + semi) % 12] > 0.55:
            extras.append((semi, mark))
    label = NAMES[r] + quality + "".join(m for _s, m in extras[:1])
    voiced = [root + 12, root + 12 + third, root + 19]
    if extras:
        voiced.append(root + 12 + extras[0][0])
    return label, voiced


SCALES = {
    "pentatonic": [57, 60, 62, 64, 67, 69, 72, 74, 76, 79, 81],
    "minor":      [57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74],
    "dorian":     [57, 59, 60, 62, 64, 66, 67, 69, 71, 72, 74],
    "whole":      [57, 59, 61, 63, 65, 67, 69, 71, 73, 75, 77],
}


def fit_scale(col: np.ndarray) -> tuple[str, float]:
    """Which of the glove's scales the passage lives in.

    The scales are fixed tables of MIDI notes — the instrument cannot be
    retuned mid-performance — so the honest question is not "what key is
    this in" but "which of the four can play it". Scored as the share of
    the passage's pitch-class energy the scale covers.
    """
    best, best_s = "dorian", -1.0
    for name, notes in SCALES.items():
        members = {n % 12 for n in notes}
        got = sum(col[j] for j in members) / (col.sum() or 1.0)
        if got > best_s:
            best, best_s = name, got
    return best, best_s


# --------------------------------------------------------------------------
# The backing
# --------------------------------------------------------------------------
# Rendered here rather than through GloveSynth on purpose. The synth is
# monophonic and gesture-driven — that is the instrument, and the instrument
# is what the player is holding. The backing is accompaniment: a vamp that
# has to sit still and stay out of the way while a hand plays over it.
def _saw(hz: float, n: int, detune=0.0) -> np.ndarray:
    t = np.arange(n) / SAMPLE_RATE
    out = np.zeros(n)
    for d in (-detune, 0.0, detune):
        f = hz * 2.0 ** (d / 1200.0)
        # Band-limited enough for a backing part: partials only up to 8 kHz,
        # so the vamp does not alias into the register the lead plays in.
        for k in range(1, max(int(8000.0 / f), 1) + 1):
            out += np.sin(2 * np.pi * f * k * t) / k
    return out / 3.0


def _env(n: int, attack: float, release: float) -> np.ndarray:
    a, r = max(int(attack * SAMPLE_RATE), 1), max(int(release * SAMPLE_RATE), 1)
    e = np.ones(n)
    a, r = min(a, n), min(r, n)
    e[:a] = np.linspace(0.0, 1.0, a)
    e[n - r:] *= np.linspace(1.0, 0.0, r)
    return e


def _close_loop(out: np.ndarray, n: int) -> np.ndarray:
    """Fold a part's overhanging release back round the loop.

    Everything here is going into the loop buffer, and a release that is
    simply cut at the end puts a seam in once a bar, which is worse than no
    backing at all.
    """
    tail = out[n:]
    out = out[:n].copy()
    out[:len(tail)] += tail
    return out


def render_parts(score: dict) -> dict[str, np.ndarray]:
    """The vamp, as separate parts rather than one mix.

    Separate on purpose. The synth already mutes, solos and deletes layers
    one at a time, and a backing delivered as a single wav can only be taken
    out whole — so covering a piece would have been the one place in the
    instrument where that did not work. As three parts, the bass can drop
    out for a verse, or the chords can be soloed to hear the changes, with
    the gestures and the panel that are already there.

    All parts are the same length, which is what lets the synth stack them:
    the first layer fixes the loop length and the rest wrap into it.
    """
    n = int(score["seconds"] * SAMPLE_RATE)
    pad_room = n + SAMPLE_RATE          # room for the last release to ring
    parts: dict[str, np.ndarray] = {}

    bass = np.zeros(pad_room)
    for at, midi, dur in score["bass"]:
        i = int(at * SAMPLE_RATE)
        ln = int(min(dur, score["pulse"] * 0.9) * SAMPLE_RATE)
        hz = 440.0 * 2.0 ** ((midi - 69) / 12.0)
        bass[i:i + ln] += 0.55 * _saw(hz, ln, detune=6.0) * _env(ln, 0.004, 0.05)
    parts["bass"] = _close_loop(bass, n)

    chords = np.zeros(pad_room)
    for b in score["blocks"]:
        i = int(b["at"] * SAMPLE_RATE)
        ln = int(b["secs"] * SAMPLE_RATE)
        pad = np.zeros(ln)
        for midi in b["voiced"]:
            hz = 440.0 * 2.0 ** ((midi - 69) / 12.0)
            pad += _saw(hz, ln, detune=11.0)
        # Level matters more here than it looks. At a third of this the
        # chords were present but taking them out was inaudible — measured
        # at 0.5 dB — which made a layer you could mute and not hear, the
        # one thing a layer must not be. The chord changes are the reason
        # this vamp is interesting; they are mixed to be heard leaving.
        chords[i:i + ln] += (0.3 * pad / max(len(b["voiced"]), 1)
                             * _env(ln, 0.08, 0.12))
    parts["chords"] = _close_loop(chords, n)

    # A pedal tone, if the passage has one. Held notes read as *sustain*
    # rather than as a part, so leaving it out cost the vamp its drone: the
    # chords were right and the thing still sounded like two chords rather
    # than like the record.
    if score.get("pedal") is not None:
        hz = 440.0 * 2.0 ** ((score["pedal"] - 69) / 12.0)
        parts["pedal"] = 0.14 * _saw(hz, n, detune=14.0) * _env(n, 0.3, 0.3)

    # Scaled together, by the loudest of them, so muting one does not change
    # how loud the others are.
    peak = max((float(np.abs(p).max()) for p in parts.values()), default=0.0)
    if peak > 1e-6:
        for k in parts:
            parts[k] = parts[k] / peak * 0.7
    return parts


def render_backing(score: dict) -> np.ndarray:
    """The whole vamp as one buffer, for anyone who just wants to hear it."""
    parts = render_parts(score)
    return sum(parts.values()) if parts else np.zeros(1)


def write_midi(score: dict, path: pathlib.Path) -> bool:
    """The lead line as a .mid, so `--melody` can play it back.

    Not the performance — a reference. Hearing the line you are about to
    play is worth a great deal when the only notation is a chart.
    """
    try:
        import mido
    except ImportError:
        return False
    mid = mido.MidiFile()
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tpb = mid.ticks_per_beat
    beat = 60.0 / score["bpm"]
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(int(score["bpm"]))))
    events = []
    for at, midi, dur in score["lead"]:
        events.append((at, "note_on", midi))
        events.append((at + dur, "note_off", midi))
    events.sort()
    now = 0.0
    for at, kind, midi in events:
        delta = int(round((at - now) / beat * tpb))
        tr.append(mido.Message(kind, note=midi, velocity=80, time=max(delta, 0)))
        now = at
    mid.save(str(path))
    return True


def chart(score: dict) -> str:
    """What to play, for a person, on one screen."""
    lines = [
        f"  {score['name']}  —  {score['bpm']:.0f} BPM, "
        f"pulse every {score['pulse'] * 1000:.0f} ms",
        f"  key: the glove's '{score['scale']}' scale covers "
        f"{score['scale_fit']:.0%} of the passage",
        f"  notes measured in it: {' '.join(score['pitch_classes'])}",
    ]
    if score.get("pedal") is not None:
        lines.append(f"  pedal tone held under the whole passage: "
                     f"{note_name(score['pedal'])}")
    lines += [
        "",
        "  the vamp (the backing plays this; you play over it)",
    ]
    for b in score["blocks"]:
        bars = b["secs"] / (score["pulse"] * 8)
        lines.append(
            f"    {b['at']:5.1f}s  {b['chord']:<4} on {note_name(b['root']):<4}"
            f"  {b['secs']:4.1f}s  ({len(b['hits'])} hits, {bars:.1f} x 8)")
    lines += ["", "  the lead line, as measured"]
    row = []
    for at, midi, dur in score["lead"]:
        row.append(f"{note_name(midi)}")
        if len(row) == 12:
            lines.append("    " + " ".join(f"{r:<4}" for r in row))
            row = []
    if row:
        lines.append("    " + " ".join(f"{r:<4}" for r in row))
    return "\n".join(lines)


def transcribe(x: np.ndarray, rate: int, name: str, start: float,
               instrument: str = "guitar") -> dict:
    bpm, secs_per_pulse = pulse(x, rate)
    bass = to_notes(pitch_track(x, rate, BASS_BAND))
    lead = to_notes(pitch_track(x, rate, LEAD_BAND), min_secs=0.12)
    blocks = chord_blocks(bass)
    whole = colour(x, rate, 0.0, len(x) / rate)
    scale, fit = fit_scale(whole)

    out_blocks = []
    cols = []
    for b in blocks:
        col = colour(x, rate, b["at"], b["end"])
        cols.append(col)
        label, voiced = name_chord(b["root"], col)
        out_blocks.append({
            "at": round(b["at"], 3), "secs": round(b["end"] - b["at"], 3),
            "root": b["root"], "chord": label, "voiced": voiced,
            "hits": b["hits"],
        })

    present = [NAMES[j] for j in np.argsort(whole)[::-1] if whole[j] > 0.3]

    # A pedal is a pitch class that stays strong while the chords move
    # underneath it. Required to be near the top in *every* block, not just
    # on average: an average would nominate whichever note the longest
    # chord happens to contain.
    pedal = None
    if len(cols) >= 2:
        for j in np.argsort(whole)[::-1]:
            if all(c[j] > 0.75 for c in cols):
                # Voiced where it was measured: the register matters, a
                # pedal an octave low is a bass note and changes the chords.
                in_band = [m for _t, m, _d in lead if m % 12 == j]
                pedal = int(np.median(in_band)) if in_band else int(69 + (j - 9) % 12)
                break

    return {
        "name": name,
        "start": start,
        "seconds": round(len(x) / rate, 3),
        "bpm": round(bpm, 1),
        "pulse": round(secs_per_pulse, 4),
        "scale": scale,
        "scale_fit": round(fit, 3),
        "pitch_classes": present,
        "pedal": pedal,
        "instrument": instrument,
        "blocks": out_blocks,
        "bass": [(round(a, 3), m, round(d, 3)) for a, m, d in bass],
        "lead": [(round(a, 3), m, round(d, 3)) for a, m, d in lead],
    }


def _read_wav(path: pathlib.Path) -> np.ndarray:
    try:
        import soundfile as sf
        return sf.read(str(path), dtype="float64", always_2d=True)[0].mean(axis=1)
    except ImportError:
        import wave
        with wave.open(str(path), "rb") as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return raw.astype(np.float64) / 32768.0


def load(path: str) -> dict:
    """Read a score written earlier, and the parts rendered beside it."""
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such cover: {p}")
    score = json.loads(p.read_text())
    files = score.get("parts") or {}
    parts = {name: _read_wav(p.parent / f)
             for name, f in files.items() if (p.parent / f).is_file()}
    # The score holds everything needed to make them again, so parts that
    # were moved, deleted or never written are not worth failing over.
    score["_parts"] = parts if len(parts) == len(files) and parts else render_parts(score)
    return score


def install(score: dict, synth, hand, mapping_mod) -> None:
    """Set the gloves up to play this cover.

    The cover is installed as a *scene*, which is the thing in this codebase
    that already means "instrument plus key plus modes, together". Appending
    to the table rather than reaching into the synth means the scale lock,
    the harmony interval, the button that cycles scenes and the name in the
    browser all work with no further wiring — and the scenes you had are
    still there to cycle back to.
    """
    mapping_mod.SCENES.append(
        (score["name"], score["instrument"], score["scale"], False, False))
    hand.scene = len(mapping_mod.SCENES) - 1
    mapping_mod.apply_scene(synth, hand, announce=False)

    # One layer per part, so the vamp can be taken apart while it plays.
    # set_loop clears the stack and installs the first, add_track stacks the
    # rest on top at the same length.
    parts = score["_parts"]
    first = True
    for name in ("bass", "chords", "pedal"):
        if name not in parts:
            continue
        if first:
            synth.set_loop(parts[name], SAMPLE_RATE, label=name)
            first = False
        else:
            synth.add_track(parts[name], name)
    synth.loop_on = True
    # The backing is there to play over, so the instrument stays up: a song
    # loaded with --song mutes it, because there you are playing the record.
    synth.mute_instrument(False)
    print("\n" + chart(score))
    print(f"\n  [the vamp is looping as {len(synth.tracks_state())} layers — "
          f"{synth.layer_summary()}"
          f"\n   mute or solo any of them from the Layers panel, or with a "
          f"gesture;"
          f"\n   the {score['instrument']} is in your hand, locked to the "
          f"{score['scale']} scale]\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="an audio file, or a link")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=16.0)
    ap.add_argument("--name", default="Cover")
    ap.add_argument("--instrument", default="guitar",
                    help="which glove instrument plays the lead")
    args = ap.parse_args()

    import glove.song as song_loader
    samples, rate, label, _artist = song_loader.load(
        args.source, start=args.start, seconds=args.seconds)
    print(f"\nmeasuring {label}  {args.start:g}s..{args.start + args.seconds:g}s\n")

    score = transcribe(samples, rate, args.name, args.start, args.instrument)
    score["source"] = label

    out = pathlib.Path(__file__).parent.parent / "covers"
    out.mkdir(exist_ok=True)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in args.name).strip("-")
    jpath = out / f"{slug}.json"
    mpath = out / f"{slug}-lead.mid"

    def write_wav(path: pathlib.Path, data: np.ndarray) -> None:
        try:
            import soundfile as sf
            sf.write(str(path), data.astype(np.float32), SAMPLE_RATE)
        except ImportError:
            import wave
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(
                    (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())

    parts = render_parts(score)
    score["parts"] = {}
    for name, data in parts.items():
        wpath = out / f"{slug}-{name}.wav"
        write_wav(wpath, data)
        score["parts"][name] = wpath.name
    # The mix too, for listening to the vamp without starting the instrument.
    write_wav(out / f"{slug}-backing.wav", sum(parts.values()))
    score["backing"] = f"{slug}-backing.wav"
    score["lead_midi"] = mpath.name if write_midi(score, mpath) else None
    jpath.write_text(json.dumps(score, indent=2))

    print(chart(score))
    print(f"\n  score    {jpath}")
    print(f"  backing  {len(parts)} layers — {', '.join(parts)} "
          f"({score['seconds']:.1f}s, loops)")
    if score["lead_midi"]:
        print(f"  lead     {mpath}  (reference only — play it yourself)")
    print(f"\n  python main.py --ble --web --cover covers/{slug}.json\n")


if __name__ == "__main__":
    main()
