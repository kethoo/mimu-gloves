"""Measure the right glove's middle finger: straight, bent, and how steady.

    python calibrate_flex.py

    python calibrate_flex.py --watch

Stop main.py first, because a glove takes one connection at a time. Follow the
prompts, then start main.py again; it reads the result from flex-cal.json.
--watch shows the live reading, for wiggling each wire to find a loose one.
"""

from __future__ import annotations

import asyncio
import statistics
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

from ble_receiver import (CHAR_UUID, FLEX_VALID_MIN, flex_kohm, save_flex_cal,
                          solo_pulldown)

GLOVE = "MIMU-GLOVE-V"
SETTLE = 2.0        # seconds to get into position before measuring
STEPS = [("straight1", "Keep the middle finger STRAIGHT and relaxed", 3),
         ("bent1", "Bend the middle finger FULLY and hold", 3),
         ("straight2", "STRAIGHT again", 3),
         ("bent2", "FULLY bent again", 3),
         ("wave", "Finger STRAIGHT: now rotate and wave your whole hand", 6)]
MIN_SWING = 25.0    # counts; under this it is too close to the noise to use


def _held(values: list[float]) -> float:
    """The reading once the finger got there: the last part of the hold, so a
    late or slow bend does not drag the number towards where it started."""
    return statistics.median(values[len(values) * 2 // 5:])


def summarise(samples: dict[str, list[float]]) -> dict:
    """Turn the five recordings into a verdict. A result that fails any check
    is not saved: a bad calibration is worse than the 7.9-8.8k default."""
    med = {k: _held(v) for k, v in samples.items()}
    straight = (med["straight1"] + med["straight2"]) / 2
    bent = (med["bent1"] + med["bent2"]) / 2
    swing = abs(bent - straight)
    wave_lo, wave_hi = min(samples["wave"]), max(samples["wave"])
    wave = max(abs(wave_lo - straight), abs(wave_hi - straight))
    drift = max(abs(med["straight1"] - med["straight2"]),
                abs(med["bent1"] - med["bent2"]))
    problems = []
    if swing < MIN_SWING:
        problems.append(
            f"Bending moves the reading by only {swing:.0f} counts. Check the "
            "sensor is taped along the finger and its wires are firm.")
    if drift > 0.3 * swing:
        problems.append(
            f"The same position read {drift:.0f} counts apart on the two tries, "
            f"against a {swing:.0f}-count bend. The sensor is moving under its "
            "tape or a connection is unsteady.")
    if wave > 0.5 * swing:
        problems.append(
            f"With the finger straight, waving your hand took the reading from "
            f"{wave_lo:.0f} to {wave_hi:.0f}, against a {swing:.0f}-count bend, "
            "so hand movement will look like bending. A connection is loose: "
            "find it with --watch.")
    return {"straight": straight, "bent": bent, "swing": swing, "wave": wave,
            "wave_lo": wave_lo, "wave_hi": wave_hi, "drift": drift, "med": med,
            "usable": not problems, "problems": problems}


async def _find_glove():
    print(f"Looking for {GLOVE}...")
    dev = await BleakScanner.find_device_by_name(GLOVE, timeout=10)
    if dev is None:
        print("Not found. Is main.py still running? Stop it with Ctrl+C.")
    return dev


async def watch() -> None:
    """Live reading, with the lowest and highest since the last press of the
    wire you are testing. A firm circuit barely moves when wiggled."""
    dev = await _find_glove()
    if dev is None:
        return
    raw = [0.0, 0.0]

    def on(_handle, data: bytearray) -> None:
        v = struct.unpack("<%df" % (len(data) // 4), bytes(data)[:len(data) // 4 * 4])
        raw[0], raw[1] = v[7], v[8]

    async with BleakClient(dev) as client:
        await client.start_notify(CHAR_UUID, on)
        await asyncio.sleep(1.0)
        pin = 1 if raw[1] >= raw[0] else 0
        print(f"Sensor on GPIO{34 + pin}. Wiggle one joint at a time, Ctrl+C to stop.")
        print("Range restarts every 5 seconds.\n")
        lo, hi, start = raw[pin], raw[pin], time.time()
        while True:
            if time.time() - start > 5.0:
                print()
                lo, hi, start = raw[pin], raw[pin], time.time()
            lo, hi = min(lo, raw[pin]), max(hi, raw[pin])
            print(f"\r    reading {raw[pin]:5.0f}   low {lo:5.0f}   high {hi:5.0f}"
                  f"   moved {hi - lo:5.0f} ", end="", flush=True)
            await asyncio.sleep(0.05)


async def record() -> dict[str, list[float]] | None:
    dev = await _find_glove()
    if dev is None:
        return None
    raw = [0.0, 0.0]

    def on(_handle, data: bytearray) -> None:
        v = struct.unpack("<%df" % (len(data) // 4), bytes(data)[:len(data) // 4 * 4])
        raw[0], raw[1] = v[7], v[8]

    out: dict[str, list[float]] = {}
    async with BleakClient(dev) as client:
        await client.start_notify(CHAR_UUID, on)
        await asyncio.sleep(1.0)
        pin = 1 if raw[1] >= raw[0] else 0
        if raw[pin] < FLEX_VALID_MIN:
            print(f"No sensor: GPIO34 reads {raw[0]:.0f}, GPIO35 reads {raw[1]:.0f}.")
            return None
        print(f"Sensor found on GPIO{34 + pin}.\n")
        for key, label, secs in STEPS:
            print(f">>> {label}")
            await asyncio.sleep(SETTLE)
            vals = []
            end = time.time() + secs
            while time.time() < end:
                vals.append(raw[pin])
                print(f"\r    reading {raw[pin]:5.0f}   {end - time.time():3.1f} s ",
                      end="", flush=True)
                await asyncio.sleep(0.05)
            print()
            out[key] = vals
    return out


def main() -> None:
    if "--watch" in sys.argv:
        try:
            asyncio.run(watch())
        except KeyboardInterrupt:
            print()
        return
    samples = asyncio.run(record())
    if samples is None:
        return
    r = summarise(samples)
    print()
    for key, label in (("straight1", "straight"), ("bent1", "bent"),
                       ("straight2", "straight"), ("bent2", "bent")):
        vals = samples[key]
        print(f"  {label:<9} held at {r['med'][key]:5.0f}   "
              f"(low {min(vals):5.0f}, high {max(vals):5.0f})")
    print(f"  hand wave from {r['wave_lo']:5.0f} to {r['wave_hi']:5.0f}\n")
    print(f"straight {r['straight']:.0f}   bent {r['bent']:.0f}   "
          f"bend moves it {r['swing']:.0f} counts")
    if r["usable"]:
        k = flex_kohm(r["bent"], solo_pulldown(r["straight"]))
        print(f"If straight is 7.9k, a full bend here is {k:.1f}k (you measured 8.8k).")
        save_flex_cal(GLOVE, r["straight"], r["bent"])
        print("Saved. Start main.py again, keeping your finger straight as it connects.")
    else:
        print("NOT saved:")
        for line in r["problems"]:
            print("! " + line)


if __name__ == "__main__":
    main()
