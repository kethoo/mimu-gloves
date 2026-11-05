"""Measure the right glove's middle finger: straight, bent, and how steady.

    python calibrate_flex.py

Stop main.py first, because a glove takes one connection at a time. Follow the
prompts, then start main.py again; it reads the result from flex-cal.json.
"""

from __future__ import annotations

import asyncio
import statistics
import struct
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


def summarise(samples: dict[str, list[float]]) -> dict:
    """Turn the five recordings into a verdict. Saving is decided here."""
    med = {k: statistics.median(v) for k, v in samples.items()}
    straight = (med["straight1"] + med["straight2"]) / 2
    bent = (med["bent1"] + med["bent2"]) / 2
    swing = abs(bent - straight)
    wave = max(abs(x - straight) for x in samples["wave"])
    drift = max(abs(med["straight1"] - med["straight2"]),
                abs(med["bent1"] - med["bent2"]))
    problems = []
    usable = swing >= MIN_SWING
    if not usable:
        problems.append(
            f"Bending moves the reading by only {swing:.0f} counts, so nothing "
            "was saved. Check the sensor is taped along the finger and its "
            "wires are firm.")
    else:
        if drift > 0.3 * swing:
            problems.append(
                f"The two tries differ by {drift:.0f} counts, a lot for a "
                f"{swing:.0f}-count bend. The sensor may be moving under its tape.")
        if wave > 0.5 * swing:
            problems.append(
                f"Waving your hand moved the reading by {wave:.0f} counts with "
                "the finger straight. That can look like a bend: firm up the "
                "wires and the tape, then run this again.")
    return {"straight": straight, "bent": bent, "swing": swing, "wave": wave,
            "drift": drift, "usable": usable, "problems": problems}


async def record() -> dict[str, list[float]] | None:
    print(f"Looking for {GLOVE}...")
    dev = await BleakScanner.find_device_by_name(GLOVE, timeout=10)
    if dev is None:
        print("Not found. Is main.py still running? Stop it with Ctrl+C.")
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
    samples = asyncio.run(record())
    if samples is None:
        return
    r = summarise(samples)
    print(f"\nstraight {r['straight']:.0f}   bent {r['bent']:.0f}   "
          f"bend moves it {r['swing']:.0f} counts   "
          f"hand waving moves it {r['wave']:.0f}")
    if r["usable"]:
        k = flex_kohm(r["bent"], solo_pulldown(r["straight"]))
        print(f"If straight is 7.9k, a full bend here is {k:.1f}k (you measured 8.8k).")
        save_flex_cal(GLOVE, r["straight"], r["bent"])
        print("Saved. Start main.py again, then keep your finger straight as it connects.")
    for line in r["problems"]:
        print("! " + line)


if __name__ == "__main__":
    main()
