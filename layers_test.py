"""Walk the layering flow and measure that each layer is really isolated.

    python layers_test.py

The flow this checks is the one you perform: record a phrase, overdub a
second on top of it, overdub a third, then take one of them out again and
hear the other two carry on. By hand that is `v`, then `o`, `o`, then the
Layers panel — and by hand you can only tell whether it *sounds* right.

Each take here is given its own note, which is the whole trick: 220, 330
and 440 Hz. Once they are stacked, the energy at those three frequencies
says which layers are actually sounding, so "the bass dropped out" stops
being an impression and becomes a number. A layer that is muted should fall
away to nothing and leave the other two where they were.

No microphone and no glove. The takes are synthesised and pushed through
the real `_stop_recording` path, so what runs is the overdub, alignment,
normalisation and remix code that runs when you play — not a copy of it.
"""

from __future__ import annotations

import contextlib
import io

import numpy as np

import synth as S

TAKES = [("take 1", 220.0), ("take 2", 330.0), ("take 3", 440.0)]
SECONDS = 2.0
BLOCK = 256


class _FakeStream:
    """`_stop_recording` closes the input stream it opened. There is no
    stream here, and stubbing these two methods is cheaper and more honest
    than opening a real microphone the test does not use."""

    def stop(self) -> None: pass
    def close(self) -> None: pass


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def tone(hz: float, secs: float = SECONDS) -> np.ndarray:
    """One take: a steady note with a little fade so it loops cleanly."""
    t = np.arange(int(secs * S.SAMPLE_RATE)) / S.SAMPLE_RATE
    x = 0.4 * np.sin(2 * np.pi * hz * t)
    edge = int(0.01 * S.SAMPLE_RATE)
    x[:edge] *= np.linspace(0, 1, edge)
    x[-edge:] *= np.linspace(1, 0, edge)
    return x.astype(np.float32)


def record(sy: S.GloveSynth, hz: float, overdub: bool) -> None:
    """Hand a take to the synth the way the microphone would.

    The first take replaces the stack and later ones add to it, which is the
    difference between `v` and `o` and the reason the flow works at all.
    """
    sy._rec_chunks = [tone(hz).reshape(-1, 1)]
    sy._rec_stream = _FakeStream()
    sy._overdub = overdub
    sy._overdub_from = sy._loop_pos if overdub else 0.0
    with quiet():
        sy._stop_recording()


def render(sy: S.GloveSynth, secs: float = SECONDS) -> np.ndarray:
    out = np.zeros((BLOCK, 2), dtype=np.float32)
    acc = []
    for _ in range(int(secs * S.SAMPLE_RATE / BLOCK)):
        sy._callback(out, BLOCK, None, None)
        acc.append(out[:, 0].copy())
    return np.concatenate(acc)


def at_hz(x: np.ndarray, hz: float) -> float:
    """How much of one note is in the output, in dB.

    A narrow window around the bin rather than the bin alone: the loop plays
    at whatever speed the hand asks for, and a note that has drifted a few
    cents would otherwise read as absent.
    """
    mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freq = np.fft.rfftfreq(len(x), 1.0 / S.SAMPLE_RATE)
    k = (freq > hz * 0.97) & (freq < hz * 1.03)
    return 20 * np.log10(float(np.sqrt((mag[k] ** 2).sum())) / len(x) + 1e-12)


def clicks(x: np.ndarray) -> int:
    """Sample-to-sample jumps big enough to hear as a click."""
    return int(np.sum(np.abs(np.diff(x)) > 0.15))


def row(label: str, x: np.ndarray, base: list[float] | None) -> list[float]:
    got = [at_hz(x, hz) for _n, hz in TAKES]
    cells = []
    for i, v in enumerate(got):
        mark = ""
        if base is not None:
            d = v - base[i]
            mark = "  out" if d < -20 else ("     " if abs(d) < 3 else f"{d:+5.0f}")
        cells.append(f"{v:7.1f}{mark}")
    print(f"  {label:<22}{''.join(cells)}   {clicks(x):>3}")
    return got


def main() -> None:
    print(__doc__.strip().split("\n\n")[0] + "\n")
    with quiet():
        sy = S.GloveSynth(no_mic=True, open_stream=False)
        sy.mute_instrument(True)       # listen to the layers, not the voice

    print("recording the flow:")
    for i, (label, hz) in enumerate(TAKES):
        record(sy, hz, overdub=i > 0)
        print(f"  {'v' if i == 0 else 'o'}  {label} at {hz:.0f} Hz"
              f"   ->  layers: {sy.layer_summary()}")

    print(f"\nenergy at each take's own note, dB"
          f"   ({' / '.join(f'{hz:.0f}' for _n, hz in TAKES)} Hz)")
    print(f"  {'state':<22}{'take 1':>12}{'take 2':>12}{'take 3':>12}"
          f"   {'clicks':>6}")
    base = row("all three", render(sy), None)

    ids = [t["id"] for t in sy.tracks_state()]
    checks: list[tuple[str, bool]] = []
    for i, (label, _hz) in enumerate(TAKES):
        with quiet():
            sy.track_action(ids[i], "mute")
        got = row(f"{label} muted", render(sy), base)
        with quiet():
            sy.track_action(ids[i], "mute")
        gone = got[i] - base[i] < -20
        others = all(abs(got[j] - base[j]) < 3 for j in range(len(TAKES)) if j != i)
        checks.append((f"muting {label} removes only {label}", gone and others))

    for i, (label, _hz) in enumerate(TAKES):
        with quiet():
            sy.track_action(ids[i], "solo")
        got = row(f"{label} soloed", render(sy), base)
        with quiet():
            sy.track_action(ids[i], "solo")
        kept = got[i] - base[i] > -3
        others = all(got[j] - base[j] < -20 for j in range(len(TAKES)) if j != i)
        checks.append((f"soloing {label} leaves only {label}", kept and others))

    with quiet():
        sy.track_action(ids[1], "delete")
    got = row("take 2 deleted", render(sy), base)
    checks.append(("deleting take 2 removes only take 2",
                   got[1] - base[1] < -20 and got[0] - base[0] > -3
                   and got[2] - base[2] > -3))
    print(f"\n  layers left: {sy.layer_summary()}")

    # Every transition above was rendered immediately after the change, so a
    # click would have shown up in that render's own count. This is the one
    # thing the ear catches that the dB columns do not.
    quietest = max(clicks(render(sy)) for _ in range(3))
    checks.append(("no clicks on any transition", quietest == 0))

    print("\n" + "=" * 62)
    for what, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    print("=" * 62)
    bad = [w for w, ok in checks if not ok]
    print(f"\n  {len(checks) - len(bad)}/{len(checks)} passed"
          + (f" — FAILED: {'; '.join(bad)}" if bad else ""))
    print("\n  by hand, the same flow is:  v (record)  o (overdub)  o (again),")
    print("  then mute or solo a row in the browser's Layers panel.\n")


if __name__ == "__main__":
    main()
