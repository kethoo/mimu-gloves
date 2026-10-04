"""Record a raw flex trace, so the calibration can be tuned on real numbers.

diagnose.py shows you the flex values live; this one writes down every
sample, labelled with the pose you were holding at the time. That is the
difference between "the span looks small" and knowing exactly which counts a
fully bent finger produces on THIS glove, how much the reading drifts while
you hold still, and whether the dropouts are single spikes or bursts.

    python flexlog.py                 whichever glove is advertising
    python flexlog.py --instrument    the instrument hand (MIMU-GLOVE-I)
    python flexlog.py --voice         the voice hand (MIMU-GLOVE-V)

It prompts for one pose at a time and counts down while it samples. Follow
the prompts; the whole routine takes about a minute. The trace lands in
flexlog.csv, and `python flexlog.py --report flexlog.csv` reads it back.
"""

from __future__ import annotations

import asyncio
import csv
import struct
import sys
import time

# Shared with the receiver so this agrees with what main.py actually rejects.
from ble_receiver import FLEX_VALID_MIN

CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "MIMU-GLOVE"
OUT = "flexlog.csv"

# (label, seconds, what to tell the player)
#
# Ordered so the two extremes come first: if nothing else completes, those
# two alone give the usable range. The holds in the middle are what reveal
# drift and dropouts, which a quick bend would hide.
ROUTINE = [
    ("straight", 5, "Hold BOTH fingers completely STRAIGHT. Don't move."),
    ("bent", 5, "Now CURL BOTH fingers as tight as they go. Hold."),
    ("straight2", 5, "Straight again, completely flat."),
    ("bent2", 5, "Tight fist again."),
    ("half", 5, "Hold both fingers HALF bent, as steady as you can."),
    ("hold_bent", 15, "Make a tight fist and HOLD IT STILL for 15 seconds."),
    ("sweep", 15, "Slowly open and close your hand, over and over."),
    ("index_only", 6, "Bend ONLY the INDEX finger. Middle stays straight."),
    ("middle_only", 6, "Bend ONLY the MIDDLE finger. Index stays straight."),
    ("rest", 6, "Relax your hand completely and leave it still."),
]


class Recorder:
    def __init__(self) -> None:
        self.rows: list[tuple[float, str, float, float]] = []
        self.label = "idle"
        self.t0 = time.monotonic()
        self.packets = 0
        self.last: tuple[float, float] | None = None

    def on_packet(self, _handle, data: bytearray) -> None:
        if len(data) == 36:
            flex, flex2 = struct.unpack("<9f", data)[7:9]
        elif len(data) == 32:
            flex, flex2 = struct.unpack("<8f", data)[7], float("nan")
        else:
            return
        self.packets += 1
        self.last = (flex, flex2)
        self.rows.append((time.monotonic() - self.t0, self.label, flex, flex2))


def _clean(vals):
    """Pose samples with dropouts removed, and the count of those removed.

    Mixed in, a handful of dropouts dominate every min/max and make a steady
    pose look like a wild one — which is the misreading this whole exercise
    exists to avoid. They are counted, not silently dropped.
    """
    good = [v for v in vals if v == v and v >= FLEX_VALID_MIN]
    return sorted(good), len(vals) - len(good)


def _summarise(rows) -> None:
    """Per-pose statistics, plus the two numbers the calibration needs."""
    by_label: dict[str, list[tuple[float, float]]] = {}
    for _t, label, f1, f2 in rows:
        by_label.setdefault(label, []).append((f1, f2))

    print(f"\n{'pose':<12} {'n':>5}   {'flex1 (middle)':<28} {'flex2 (index)':<28}")
    print("-" * 78)
    for label, _dur, _msg in ROUTINE:
        vals = by_label.get(label)
        if not vals:
            continue
        cells = []
        for i in (0, 1):
            col, bad = _clean([v[i] for v in vals])
            if not col:
                cells.append(f"- ({bad} dropouts)")
                continue
            med = col[len(col) // 2]
            note = f"  !{bad}" if bad else ""
            cells.append(f"{col[0]:4.0f}..{col[-1]:4.0f}  med {med:4.0f}  "
                         f"+/-{(col[-1] - col[0]) / 2:3.0f}{note}")
        print(f"{label:<12} {len(vals):>5}   {cells[0]:<28} {cells[1]:<28}")
    print("  (! = dropouts in that pose, excluded from the figures)")

    print()
    for i, name in ((0, "flex1 (middle)"), (1, "flex2 (index)")):
        def med(label):
            vals, _bad = _clean([v[i] for v in by_label.get(label, [])])
            return vals[len(vals) // 2] if vals else None

        straight = [m for m in (med("straight"), med("straight2")) if m]
        bent = [m for m in (med("bent"), med("bent2")) if m]
        if not straight or not bent:
            print(f"{name}: not enough data")
            continue
        s, b = sum(straight) / len(straight), sum(bent) / len(bent)
        travel = abs(s - b)
        print(f"{name}: straight ~{s:.0f}, bent ~{b:.0f}  ->  travel {travel:.0f} counts")
        if travel < 80:
            print("    TOO LITTLE TRAVEL — the sensor is barely bending. Check how "
                  "it is mounted before touching any constant.")

        # Drift while holding still is the figure that decides whether a
        # steady pose can be told apart from a slow real movement.
        hold, _bad = _clean([v[i] for v in by_label.get("hold_bent", [])])
        if hold:
            drift = hold[-1] - hold[0]
            print(f"    drift over a 15 s hold: {drift:.0f} counts "
                  f"({drift / travel * 100:.0f}% of travel)")

    # Dropouts, and how they arrive. A scatter of single spikes and one long
    # burst need different fixes, and the raw count cannot tell them apart.
    print()
    for i, name in ((0, "flex1 (middle)"), (1, "flex2 (index)")):
        col = [r[2 + i] for r in rows if r[2 + i] == r[2 + i]]
        if not col:
            continue
        runs, cur = [], 0
        for v in col:
            if v < FLEX_VALID_MIN:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        if not runs:
            print(f"{name}: no dropouts in {len(col)} samples "
                  f"(lowest reading {min(col):.0f})")
        else:
            print(f"{name}: {sum(runs)} of {len(col)} samples below "
                  f"{FLEX_VALID_MIN:.0f}, in {len(runs)} bursts of up to "
                  f"{max(runs)} — lowest {min(col):.0f}")


async def record() -> None:
    from bleak import BleakClient

    from ble_receiver import _Discovery

    name = DEVICE_NAME
    if "--voice" in sys.argv:
        name = "MIMU-GLOVE-V"
    elif "--instrument" in sys.argv:
        name = "MIMU-GLOVE-I"
    for i, a in enumerate(sys.argv):
        if a == "--name" and i + 1 < len(sys.argv):
            name = sys.argv[i + 1]

    print(f"Scanning for {name}...")
    dev = await _Discovery(timeout=15).find(name)
    if dev is None:
        print(f"'{name}' not found — is the ESP32 powered and in range?")
        return

    rec = Recorder()
    async with BleakClient(dev) as client:
        await client.start_notify(CHAR_UUID, rec.on_packet)
        await asyncio.sleep(1.0)
        if rec.packets == 0:
            print("Connected, but no packets — is the firmware current?")
            return
        print(f"\nConnected, {rec.packets} packets in the first second.\n"
              "Put the glove on. Each step says what to do, then counts down\n"
              "while it records. Hold each pose until the countdown ends.\n")
        # In a thread: a bare input() blocks the event loop, which stops
        # bleak servicing notifications for as long as you take to read the
        # prompt, and the link can drop while it waits.
        await asyncio.get_running_loop().run_in_executor(
            None, input, "Press Enter when you are ready... ")

        for label, dur, msg in ROUTINE:
            rec.label = "idle"
            print(f"\n>>> {msg}")
            for n in (3, 2, 1):
                print(f"    starting in {n}...", end="\r", flush=True)
                await asyncio.sleep(1.0)
            rec.label = label
            end = time.monotonic() + dur
            while time.monotonic() < end:
                left = end - time.monotonic()
                live = rec.last or (float("nan"), float("nan"))
                print(f"    recording {label:<12} {left:4.1f}s left   "
                      f"flex1 {live[0]:6.0f}  flex2 {live[1]:6.0f}",
                      end="\r", flush=True)
                await asyncio.sleep(0.1)
            print(" " * 78, end="\r")
            print(f"    {label} done ({sum(1 for r in rec.rows if r[1] == label)} samples)")
        rec.label = "idle"

    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "pose", "flex1", "flex2"])
        w.writerows(rec.rows)
    print(f"\nWrote {len(rec.rows)} samples to {OUT}")
    _summarise(rec.rows)


def report(path: str) -> None:
    rows = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            rows.append((float(r["t"]), r["pose"], float(r["flex1"]), float(r["flex2"])))
    print(f"{len(rows)} samples from {path}")
    _summarise(rows)


if __name__ == "__main__":
    if "--report" in sys.argv:
        i = sys.argv.index("--report")
        report(sys.argv[i + 1] if i + 1 < len(sys.argv) else OUT)
    else:
        try:
            asyncio.run(record())
        except KeyboardInterrupt:
            print("\nStopped.")
