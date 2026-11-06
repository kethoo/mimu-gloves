"""Design an instrument that matches the tone of a recording.

    python -m tools.match song.wav --start 160 --seconds 6 --name "Haze"
    python -m tools.match "https://..." --start 160 --seconds 6

Guessing at a patch by ear and by description does not converge: this
measures instead. It takes the average spectral shape of a passage, searches
the designer's own parameter space for the patch whose shape is closest, and
saves the winner as an instrument.

It matches **tone** and nothing else. No melody, no chords, no timing is
read out of the source — the only thing taken is the balance of energy
across the spectrum, which is what makes a sound bright or dark, thin or
thick, hollow or full. Playing the tune is still yours to do.
"""

from __future__ import annotations

import argparse
import io
import contextlib

import numpy as np

from glove import instruments
import glove.song as song_loader
import glove.synth as S

# Log-spaced bands from the bottom of a bass note to the top of what a
# speaker reproduces. Comparing band energies rather than raw bins is what
# makes the score about tone rather than about which note was playing.
BANDS = np.geomspace(80.0, 12000.0, 25)
RENDER_SECONDS = 2.0
BLOCK = 256


def envelope(x: np.ndarray, rate: int) -> np.ndarray:
    """The tone of a sound as energy per band, normalised to unit length."""
    win = int(0.25 * rate)
    hop = max(win // 2, 1)
    frames = [x[i:i + win] for i in range(0, max(len(x) - win, 1), hop)]
    acc = np.zeros(len(BANDS) - 1)
    used = 0
    for fr in frames[:40]:
        if len(fr) < win or np.abs(fr).max() < 1e-4:
            continue
        mag = np.abs(np.fft.rfft(fr * np.hanning(len(fr))))
        freq = np.fft.rfftfreq(len(fr), 1.0 / rate)
        for b in range(len(BANDS) - 1):
            k = (freq >= BANDS[b]) & (freq < BANDS[b + 1])
            acc[b] += float(np.sqrt((mag[k] ** 2).sum()))
        used += 1
    if not used:
        raise RuntimeError("that passage is silent")
    acc /= used
    # Loudness is not tone: a quiet passage and a loud one with the same
    # balance should score the same.
    return acc / (np.linalg.norm(acc) or 1.0)


def dominant_hz(x: np.ndarray, rate: int) -> float:
    """Roughly where the source sits, so the candidate is rendered in the
    same register. A tone compared an octave out scores badly for a reason
    that has nothing to do with the patch."""
    mag = np.abs(np.fft.rfft(x[: int(2.0 * rate)] * np.hanning(min(len(x), int(2.0 * rate)))))
    freq = np.fft.rfftfreq(min(len(x), int(2.0 * rate)), 1.0 / rate)
    k = (freq > 60) & (freq < 900)
    return float(freq[k][np.argmax(mag[k])])


def render(patch: dict, hz: float) -> np.ndarray:
    with contextlib.redirect_stdout(io.StringIO()):
        sy = S.GloveSynth(no_mic=True, open_stream=False)
        sy.customs["(match)"] = instruments._clamp_patch(patch)
        sy.set_instrument("(match)", announce=False)
        sy.drone_on = True
        sy.target_freq = hz
        sy._freq = hz
        sy._note_on()
        out = np.zeros((BLOCK, 2), dtype=np.float32)
        acc = []
        for _ in range(int(RENDER_SECONDS * S.SAMPLE_RATE / BLOCK)):
            sy._callback(out, BLOCK, None, None)
            acc.append(out[:, 0].copy())
    return np.concatenate(acc)


def score(patch: dict, target: np.ndarray, hz: float) -> float:
    try:
        env = envelope(render(patch, hz), S.SAMPLE_RATE)
    except Exception:
        return 0.0
    return float(env @ target)


def random_patch(engine: str, rng) -> dict:
    spec = SCHEMA[engine]
    patch = {"engine": engine, "struck": engine in ("fm", "string"),
             "params": {}}
    for k, (lo, hi) in ENV.items():
        patch[k] = float(rng.uniform(lo, hi))
    for k, (lo, hi) in FILT.items():
        patch[k] = float(rng.uniform(lo, hi))
    for k, (lo, hi) in spec.items():
        patch["params"][k] = float(rng.uniform(lo, hi))
    return patch


# Ranges taken from the designer itself, so a matched patch is one a person
# could also have reached by hand.
ENV = {"attack": (0.002, 0.8), "decay": (0.05, 3.5), "sustain": (0.2, 1.0)}
FILT = {"cutoff": (200.0, 14000.0), "resonance": (0.0, 3.4),
        "env_amount": (-2.5, 4.5), "env_decay": (0.05, 2.8)}
SCHEMA = {
    "additive": {"p1": (0.0, 1.0), "p2": (0.0, 1.0), "p3": (0.0, 1.0),
                 "p4": (0.0, 1.0), "p5": (0.0, 1.0), "p6": (0.0, 1.0),
                 "noise": (0.0, 0.3)},
    "saws":     {"voices": (2, 6), "spread": (2.0, 60.0)},
    "fm":       {"ratio": (0.5, 8.0), "index": (0.0, 14.0),
                 "index_decay": (0.5, 6.0)},
    "string":   {"t60": (0.2, 8.0), "damping": (0.0, 0.95),
                 "pickup": (0.0, 0.5), "drive": (0.0, 20.0)},
}


def refine(patch: dict, target, hz, rng, rounds=60) -> tuple[dict, float]:
    """Nudge one number at a time and keep what helps. Crude, but the space
    is small and every evaluation is a real render through the real engine."""
    best, best_s = patch, score(patch, target, hz)
    for _ in range(rounds):
        cand = {**best, "params": dict(best["params"])}
        if rng.random() < 0.5:
            pool = {**ENV, **FILT}
            k = rng.choice(list(pool))
            lo, hi = pool[k]
            cand[k] = float(np.clip(cand[k] + rng.normal(0, (hi - lo) * 0.18), lo, hi))
        else:
            spec = SCHEMA[best["engine"]]
            k = rng.choice(list(spec))
            lo, hi = spec[k]
            cand["params"][k] = float(
                np.clip(cand["params"][k] + rng.normal(0, (hi - lo) * 0.18), lo, hi))
        s = score(cand, target, hz)
        if s > best_s:
            best, best_s = cand, s
    return best, best_s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="an audio file, or a link")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--name", default="Matched")
    ap.add_argument("--seeds", type=int, default=24,
                    help="random starts per engine")
    args = ap.parse_args()

    samples, rate, label, _ = song_loader.load(
        args.source, start=args.start, seconds=args.seconds)
    target = envelope(samples, rate)
    hz = dominant_hz(samples, rate)
    print(f"\ntarget: {label}  {args.start:g}s..{args.start + args.seconds:g}s")
    print(f"strongest pitch in the passage: {hz:.0f} Hz "
          f"(candidates are rendered there so register does not skew the score)\n")

    rng = np.random.default_rng(0)
    print(f"  {'engine':<10}{'best match':>12}")
    overall, overall_s, overall_engine = None, -1.0, ""
    for engine in SCHEMA:
        best, best_s = None, -1.0
        for _ in range(args.seeds):
            p = random_patch(engine, rng)
            s = score(p, target, hz)
            if s > best_s:
                best, best_s = p, s
        best, best_s = refine(best, target, hz, rng)
        print(f"  {engine:<10}{best_s:>12.3f}")
        if best_s > overall_s:
            overall, overall_s, overall_engine = best, best_s, engine

    print(f"\nclosest: {overall_engine} at {overall_s:.3f} "
          f"(1.000 would be the same tone)")
    with contextlib.redirect_stdout(io.StringIO()):
        instruments.save(args.name, overall)
    print(f"saved as '{args.name}' — it is in the instrument rail now.")
    shown = {k: round(v, 3) for k, v in overall.items()
             if k not in ("params", "engine", "struck")}
    print(f"  {overall_engine}  {shown}")
    print(f"  params {({k: round(v, 2) for k, v in overall['params'].items()})}")


if __name__ == "__main__":
    main()
