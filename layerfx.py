"""Effects that belong to a layer rather than to the hand holding it.

The loop hand's effects are performance: one reverb, one scrub, one speed,
applied to everything at once while you move. That is right for playing and
wrong for building, because a stack where every layer must sound the same
is not a stack. Put a long reverb on the pad, mute it, give the bass an
octave down, bring the pad back, and the pad arrives an octave down too.

These are the other kind. Each is rendered into the layer's own samples
once, when you add it, and the untouched recording is kept beside it — so a
layer carries a list of what has been done to it, and taking one off means
rendering the list again without it. Nothing is destroyed and nothing is
decided at audio time: the realtime cost of a layer with four effects on it
is identical to one with none.

Every effect returns an array of exactly the same length, because layers
are stacked sample for sample and a layer that came back longer would slip
against the others. Tails are wrapped rather than cut, the way a loop
needs: the material is run through twice and the second pass is kept, so
what rings past the end has already rung into the beginning.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 44100

# Named for what they do to the sound, not for the algorithm behind them.
# The order is the order they appear in the panel.
EFFECTS = LAYER_FX = ("reverb", "echo", "octave up", "octave down",
                      "reverse", "darken")


def _wrap(fn):
    """Run a tail-producing effect so its tail lands back at the start.

    A reverb on a two-bar loop that simply stops at the end puts a hole
    where the decay should be and a seam every time round. Running the
    material twice and keeping the second pass costs one more render and
    gets a loop whose tail is already in it.
    """
    def run(x: np.ndarray) -> np.ndarray:
        n = len(x)
        out = fn(np.concatenate([x, x]))
        return out[n:]
    return run


def _reverse(x: np.ndarray) -> np.ndarray:
    return x[::-1].copy()


def _darken(x: np.ndarray) -> np.ndarray:
    """Two one-poles at 700 Hz. Takes the edge off a layer so it sits under
    something else instead of fighting it."""
    a = 1.0 - np.exp(-2.0 * np.pi * 700.0 / SAMPLE_RATE)
    y = np.empty_like(x)
    s = 0.0
    for _ in range(2):
        for i, v in enumerate(x):
            s += a * (v - s)
            y[i] = s
        x = y.copy()
        s = 0.0
    return y


def _comb(x: np.ndarray, delay: int, fb: float, damp: float) -> np.ndarray:
    """One damped comb: the building block of a Schroeder reverb."""
    out = np.zeros(len(x))
    buf = np.zeros(delay)
    store = 0.0
    i = 0
    for n in range(len(x)):
        v = buf[i]
        out[n] = v
        store = v * (1.0 - damp) + store * damp
        buf[i] = x[n] + store * fb
        i = (i + 1) % delay
    return out


def _allpass(x: np.ndarray, delay: int, g: float) -> np.ndarray:
    out = np.zeros(len(x))
    buf = np.zeros(delay)
    i = 0
    for n in range(len(x)):
        v = buf[i]
        out[n] = -x[n] + v
        buf[i] = x[n] + v * g
        i = (i + 1) % delay
    return out


@_wrap
def _reverb(x: np.ndarray) -> np.ndarray:
    """Schroeder: four combs in parallel, two allpasses in series.

    The comb delays are mutually prime so their repeats do not line up into
    a pitch, which is what a reverb made of even delays sounds like.
    """
    wet = np.zeros(len(x))
    for delay, fb in ((1557, 0.82), (1617, 0.81), (1491, 0.83), (1422, 0.80)):
        wet += _comb(x, delay, fb, 0.22)
    wet /= 4.0
    wet = _allpass(wet, 225, 0.5)
    wet = _allpass(wet, 556, 0.5)
    return x + 0.6 * wet


@_wrap
def _echo(x: np.ndarray) -> np.ndarray:
    """A feedback delay at 300 ms. Long enough to hear as repeats rather
    than as a thickening, short enough to stay inside a short loop."""
    d = int(0.3 * SAMPLE_RATE)
    out = x.copy()
    for tap in range(1, 6):
        g = 0.5 ** tap
        if g < 0.02:
            break
        i = tap * d
        if i >= len(out):
            break
        out[i:] += x[:len(out) - i] * g
    return out


def _stretch(x: np.ndarray, out_len: int, grain=2048) -> np.ndarray:
    """Change how long something takes without changing its pitch.

    Overlap-add, with each grain slid up to a grain's half-length to wherever
    it correlates best with what has already been written. That search is the
    whole difference between this and a plain crossfade: dropped straight in
    at a fixed hop, consecutive grains of a held note land at arbitrary
    phases and cancel, which is why the granular shifter this replaces put
    its energy 75 cents below the note it was asked for instead of on it.
    """
    n = len(x)
    if out_len < 2 or n < grain * 2:
        idx = np.linspace(0, n - 1, max(out_len, 2))
        return np.interp(idx, np.arange(n), x)
    hop_out = grain // 2
    win = np.hanning(grain)
    out = np.zeros(out_len + grain)
    norm = np.zeros(out_len + grain) + 1e-9
    hop_in = hop_out * n / out_len
    search = grain // 2
    pos_out = 0
    while pos_out < out_len:
        want = int(pos_out * hop_in / hop_out)
        if want + grain + search >= n:
            break
        if pos_out == 0:
            best = want
        else:
            # Where this grain would overlap what is already down.
            ref = out[pos_out:pos_out + search]
            lo, hi = max(want - search, 0), min(want + search, n - grain)
            cand = np.arange(lo, hi)
            if len(cand) == 0:
                best = want
            else:
                # One correlation against every candidate start at once.
                block = np.lib.stride_tricks.sliding_window_view(
                    x[lo:hi + len(ref)], len(ref))[:len(cand)]
                best = int(cand[np.argmax(block @ ref)])
        out[pos_out:pos_out + grain] += x[best:best + grain] * win
        norm[pos_out:pos_out + grain] += win
        pos_out += hop_out
    return (out / norm)[:out_len]


def _pitch(x: np.ndarray, ratio: float) -> np.ndarray:
    """Shift the pitch by `ratio` and leave the length alone.

    Tape first, then put the time back. Reading the samples at `ratio` speed
    moves the pitch by exactly `ratio` and changes the length with it, the
    way slowing a record down does; the overlap-add then restores the length
    and leaves the new pitch alone. Doing it the other way round — stretch,
    then resample — lands on the reciprocal, an octave up where an octave
    down was asked for.

    Offline there is no reason to settle for the realtime shifter's
    approximation: the whole buffer is in hand.
    """
    n = len(x)
    if n < 8:
        return x.copy()
    slowed = np.interp(np.arange(0.0, n - 1.0, ratio), np.arange(n), x)
    return _stretch(slowed, n)


_RENDER = {
    "reverb":      _reverb,
    "echo":        _echo,
    "octave up":   lambda x: _pitch(x, 2.0),
    "octave down": lambda x: _pitch(x, 0.5),
    "reverse":     _reverse,
    "darken":      _darken,
}


def render(src: np.ndarray, chain) -> np.ndarray:
    """The recording with its list of effects applied, in order.

    Always from the untouched source, never from the last result: that is
    what lets any one of them be taken off again. An effect that is not
    known is skipped rather than raising, so an old saved chain cannot stop
    a layer from playing.
    """
    out = np.asarray(src, dtype=np.float64)
    for name in chain or ():
        fn = _RENDER.get(name)
        if fn is None:
            continue
        out = fn(out)
    peak = float(np.abs(out).max()) if len(out) else 0.0
    # Effects add energy — a reverb and an echo together comfortably double
    # it. Held to where it started so adding one does not also turn the
    # layer up, which would read as the effect being louder rather than
    # different.
    src_peak = float(np.abs(src).max()) if len(src) else 0.0
    if peak > src_peak > 0.0:
        out *= src_peak / peak
    return out
