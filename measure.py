"""Measure the three things the project promised to measure.

    python measure.py

The proposal set four success criteria: gesture recognition, response time,
stability under long use, and subjective usability. The first three are
properties of the software and are measured here, against the real mapping
and synthesis code rather than a copy of it. The fourth needs people, and
its protocol is the testing guide, not this file.

Nothing here needs a glove attached. That is deliberate: a measurement that
only runs when the hardware is healthy is a measurement you cannot repeat.
"""

from __future__ import annotations

import contextlib
import io
import statistics
import time

import numpy as np


@contextlib.contextmanager
def quiet():
    """The synth narrates what it does, which is right when you are playing
    it and noise when ninety trials are running."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield

import mapping
import synth as S
from sensors import SensorFrame

BLOCK = 256
DEADLINE_MS = BLOCK / S.SAMPLE_RATE * 1000.0
RATE = 100.0          # control rate, Hz
TRIALS = 30           # gesture attempts per posture


# --------------------------------------------------------------------------
# 1. Gesture recognition
# --------------------------------------------------------------------------
# Noise is set from the flex measurement in README: the divider swings about
# 212 of 4095 counts end to end, and oversampling 16 reads leaves roughly 13
# counts of noise. 13/212 is 6% of the usable range, which is what a posture
# detector actually has to survive.
FLEX_NOISE = 0.06

POSES = {
    "fist":  (0.90, 0.90),   # both curled
    "open":  (0.08, 0.08),   # both straight
    "point": (0.08, 0.90),   # index straight, middle curled
}


def watcher():
    """A real synth, with a spy on the one function that fires postures.

    Stubbing a fake synth means guessing which attributes mapping touches
    and getting a different answer than the instrument would. Watching
    _fire_posture instead counts exactly the posture events and nothing
    else — the gate also opens when the index finger bends while playing,
    and that must not be mistaken for a fist.
    """
    sy = S.GloveSynth(no_mic=True, open_stream=False)
    fired: list[str] = []
    original = mapping._fire_posture

    def spy(posture, synth):
        fired.append(posture)
        original(posture, synth)

    mapping._fire_posture = spy
    return sy, fired, original


def perform(posture: str, rng, hold=None, noise=FLEX_NOISE):
    """One attempt at a gesture: move in, hold, come back — with noise.

    Held for its own dwell time plus a margin. The three are not the same:
    `open` asks for a full second because an open hand is also what a hand
    does at rest, and firing on every relaxation would be unplayable.
    """
    if hold is None:
        hold = mapping.POSTURE_HOLD[posture] + 0.25
    f1, f2 = POSES[posture]
    frames = []
    t = 0.0
    legs = ((0.25, (0.45, 0.45), (f1, f2)),      # travel into the pose
            (hold, (f1, f2), (f1, f2)),          # hold it
            (0.25, (f1, f2), (0.45, 0.45)))      # and back to playing
    for secs, a, b in legs:
        n = int(secs * RATE)
        for i in range(n):
            u = (i + 1) / n
            x = a[0] + (b[0] - a[0]) * u + rng.normal(0, noise)
            y = a[1] + (b[1] - a[1]) * u + rng.normal(0, noise)
            frames.append((t, float(np.clip(x, 0, 1)), float(np.clip(y, 0, 1))))
            t += 1.0 / RATE
    return frames


def gesture_recognition():
    rng = np.random.default_rng(20251101)
    print("1. GESTURE RECOGNITION")
    print(f"   {TRIALS} attempts per posture, sensor noise sigma = "
          f"{FLEX_NOISE:.0%} of usable range,")
    print("   each held for its own dwell time plus 0.25 s\n")
    print(f"   {'posture':<10}{'dwell':>8}{'detected':>10}{'spurious':>10}"
          f"{'accuracy':>10}")
    total_ok = total_extra = 0
    for posture in POSES:
        ok = extra = 0
        for _ in range(TRIALS):
            with quiet():
                synth, fired, original = watcher()
                st = mapping.HandState()
                for t, f1, f2 in perform(posture, rng):
                    mapping.apply(SensorFrame(t=t, flex=f1, flex2=f2), synth, st)
                mapping._fire_posture = original
            wanted = [f for f in fired if f == posture]
            others = [f for f in fired if f != posture]
            ok += 1 if wanted else 0
            extra += len(others)
        total_ok += ok
        total_extra += extra
        print(f"   {posture:<10}{mapping.POSTURE_HOLD[posture]:>7.2f}s"
              f"{ok:>7}/{TRIALS}{extra:>10}{ok / TRIALS:>9.0%}")
    n = TRIALS * len(POSES)
    print(f"\n   overall {total_ok}/{n} = {total_ok / n:.1%}"
          f"   ({total_extra} gestures fired that were not asked for)")
    return total_ok / n


def gesture_rejection():
    """The other half: a hand that is playing must not fire anything.

    The index finger is the volume control, so a swell sweeps it through the
    whole posture range. If that fires postures the instrument is unplayable,
    and this is the case the dead band and the dwell time exist for.
    """
    rng = np.random.default_rng(7)
    spurious = trials = 0
    for _ in range(TRIALS):
        with quiet():
            synth, fired, original = watcher()
            st = mapping.HandState()
            t = 0.0
            for i in range(int(3.0 * RATE)):   # a slow volume swell
                u = 0.5 - 0.5 * np.cos(2 * np.pi * i / (1.5 * RATE))
                f1 = float(np.clip(u + rng.normal(0, FLEX_NOISE), 0, 1))
                f2 = float(np.clip(0.45 + rng.normal(0, FLEX_NOISE), 0, 1))
                mapping.apply(SensorFrame(t=t, flex=f1, flex2=f2), synth, st)
                t += 1.0 / RATE
            mapping._fire_posture = original
        trials += 1
        spurious += 1 if fired else 0
    print(f"\n   false triggers while playing: {spurious}/{trials} swells"
          f"   ({1 - spurious / trials:.0%} clean)")
    return 1 - spurious / trials


# --------------------------------------------------------------------------
# 2. Response time
# --------------------------------------------------------------------------
def three_sensor():
    """The branch's own question: what one hand loses with a single sensor.

    Point needs the two fingers to disagree, so it cannot survive. Fist and
    open can, but the one remaining finger is also a continuous control, and
    separating "using it" from "meaning a posture" is the whole difficulty.
    """
    rng = np.random.default_rng(11)
    print("\n1b. THREE SENSORS — one hand with a single flex channel")

    def trial(frames):
        with quiet():
            synth, fired, original = watcher()
            st = mapping.HandState()
            t = 0.0
            for f in frames:
                mapping.apply(SensorFrame(t=t, flex=f, flex2=None), synth, st)
                t += 1.0 / RATE
            mapping._fire_posture = original
        return fired

    def noisy(v):
        return float(np.clip(v + rng.normal(0, FLEX_NOISE), 0, 1))

    def held(target, secs):
        return [noisy(target) for _ in range(int(secs * RATE))]

    def ramp(a, b, secs):
        n = int(secs * RATE)
        return [noisy(a + (b - a) * (i + 1) / n) for i in range(n)]

    cases = {
        "deliberate fist":      ramp(0.45, 0.97, 0.25) + held(0.97, 1.3) + ramp(0.97, 0.45, 0.25),
        "deliberate open":      ramp(0.45, 0.05, 0.25) + held(0.05, 1.4) + ramp(0.05, 0.45, 0.25),
        "sweeping the control": [noisy(0.5 - 0.5 * np.cos(2 * np.pi * i / (1.5 * RATE)))
                                 for i in range(int(3.0 * RATE))],
        "parked at maximum":    held(0.97, 3.0),
    }
    want = {"deliberate fist": "fist", "deliberate open": "open"}
    print(f"   {'case':<24}{'fires':>10}{'wanted':>10}")
    for name, frames in cases.items():
        hits = sum(1 for _ in range(TRIALS) if trial(frames))
        tag = want.get(name, "nothing")
        print(f"   {name:<24}{hits:>7}/{TRIALS}{tag:>10}")
    print("   point is unavailable: it is the two fingers disagreeing.")
    print("   Parking a control at maximum reads as a fist. That is the")
    print("   price of one finger doing two jobs, and it is why the fourth")
    print("   sensor is worth fitting where there is room for it.")


def response_time():
    """How long from a gesture value changing to the sound following it.

    Measured on the synth itself: move the pitch target, then find the block
    at which the rendered tone has travelled 90% of the way. That is the
    engine's own contribution; the fixed costs above it are added after.
    """
    print("\n2. RESPONSE TIME")

    def settle(start_hz, end_hz):
        with quiet():
            sy = S.GloveSynth(no_mic=True, open_stream=False)
        sy.set_instrument("saw", announce=False)
        sy.drone_on = True
        sy.target_freq = start_hz
        out = np.zeros((BLOCK, 2), dtype=np.float32)
        for _ in range(200):              # let it reach the start
            sy._callback(out, BLOCK, None, None)
        sy.target_freq = end_hz
        marks = {}
        for frac in (0.10, 0.90):
            marks[frac] = float("nan")
        for i in range(400):
            sy._callback(out, BLOCK, None, None)
            done = (sy._freq - start_hz) / (end_hz - start_hz)
            for frac in (0.10, 0.90):
                if np.isnan(marks[frac]) and done >= frac:
                    marks[frac] = (i + 1) * BLOCK / S.SAMPLE_RATE * 1000.0
        return marks

    runs = [settle(220.0, 440.0) for _ in range(5)]
    onset = statistics.mean(r[0.10] for r in runs)
    engine = statistics.mean(r[0.90] for r in runs)
    sensor, control = 1000.0 / 50, 1000.0 / RATE
    block = DEADLINE_MS
    print(f"   sensor reporting interval      {sensor:6.1f} ms")
    print(f"   control loop interval          {control:6.1f} ms")
    print(f"   audio block                    {block:6.1f} ms")
    print(f"   ---------------------------------------")
    print(f"   gesture to sound, worst case   {sensor + control + block:6.1f} ms")
    print(f"\n   Parameter smoothing is a separate thing and deliberate: a")
    print(f"   step in pitch begins moving after {onset:.0f} ms and is 90% of")
    print(f"   the way there {engine:.0f} ms later. Without that glide every")
    print(f"   control change would click.")
    return sensor + control + block


# --------------------------------------------------------------------------
# 3. Stability under load
# --------------------------------------------------------------------------
def stability(seconds=30.0):
    """Every block has 5.8 ms to be computed. A block that misses is a click.

    Run the heaviest configuration the instrument has — a designed voice over
    a loop in harmony, with the effects on — and count the misses.
    """
    print(f"\n3. STABILITY  ({seconds:.0f}s of audio, worst configuration)")
    with quiet():
        sy = S.GloveSynth(no_mic=True, open_stream=False)
    sy.set_instrument("guitar", announce=False)
    sy.drone_on = True
    sy.target_freq = 220.0
    t = np.arange(int(3.0 * S.SAMPLE_RATE)) / S.SAMPLE_RATE
    with quiet():
        sy.set_loop(np.sin(2 * np.pi * 220 * t) * 0.5, S.SAMPLE_RATE, label="test")
    sy.harmony_on = True
    sy.harmony_scale = "minor"
    sy.target_voice_reverb = 0.8

    out = np.zeros((BLOCK, 2), dtype=np.float32)
    times, bad = [], 0
    for _ in range(int(seconds * S.SAMPLE_RATE / BLOCK)):
        t0 = time.perf_counter()
        sy._callback(out, BLOCK, None, None)
        times.append((time.perf_counter() - t0) * 1000.0)
        if not np.isfinite(out).all():
            bad += 1
    times.sort()
    p99 = times[int(len(times) * 0.99)]
    over = sum(1 for x in times if x > DEADLINE_MS)
    print(f"   blocks rendered                {len(times)}")
    print(f"   mean                           {statistics.mean(times):6.3f} ms")
    print(f"   99th percentile                {p99:6.3f} ms")
    print(f"   worst                          {times[-1]:6.3f} ms")
    print(f"   deadline                       {DEADLINE_MS:6.3f} ms")
    print(f"   headroom at the 99th           {DEADLINE_MS / p99:6.1f}x")
    print(f"   blocks over deadline           {over}  ({over / len(times):.2%})")
    print(f"   blocks with NaN or infinity    {bad}")
    return over / len(times)


if __name__ == "__main__":
    print("=" * 62)
    print("  MEASUREMENTS — gesture recognition, response time, stability")
    print("=" * 62 + "\n")
    acc = gesture_recognition()
    clean = gesture_rejection()
    three_sensor()
    lat = response_time()
    miss = stability()
    print("\n" + "=" * 62)
    print("  SUMMARY")
    print("=" * 62)
    print(f"  gesture recognition      {acc:.1%}")
    print(f"  clean under playing      {clean:.0%}")
    print(f"  gesture-to-sound latency {lat:.0f} ms")
    print(f"  missed audio deadlines   {miss:.2%}")
    print("\n  Subjective usability is the fourth criterion and is not")
    print("  measured here: run the testing guide with people who have not")
    print("  used the instrument before.")
