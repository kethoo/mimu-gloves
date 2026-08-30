"""Live glove health monitor — for finding intermittent connections.

Prints one line per second showing whether the ESP32 currently sees the
BNO08x and how much the readings are moving, and shouts the moment the
sensor drops out or comes back. Use it for a wiggle test: run this, then
disturb one wire at a time and watch which one causes a dropout.

    python diagnose.py

Ctrl+C to stop.
"""

from __future__ import annotations

import asyncio
import struct
import time

# Single source of truth for the flex thresholds — diagnose and the live
# receiver must agree, or this reports "good" on a channel main.py rejects.
from ble_receiver import FLEX_MIN_SPAN, FLEX_VALID_MIN

CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "MIMU-GLOVE"


class Health:
    def __init__(self) -> None:
        self.window: list[tuple[float, float, float]] = []
        self.flex_window: list[float] = []
        self.flex2_window: list[float] = []
        self.flex_seen_min = float("inf")
        self.flex_seen_max = float("-inf")
        self.flex2_seen_min = float("inf")
        self.flex2_seen_max = float("-inf")
        self.packets = 0
        self.live = None          # None = unknown yet
        self.events: list[str] = []
        self.last_packet = 0.0

    def on_packet(self, _handle, data: bytearray) -> None:
        if len(data) == 36:
            (roll, pitch, yaw, _lax, _lay, _laz,
             status, flex, flex2) = struct.unpack("<9f", data)
            live = status >= 0.5
            self.flex_window.append(flex)
            self.flex2_window.append(flex2)
        elif len(data) == 32:
            roll, pitch, yaw, _lax, _lay, _laz, status, flex = struct.unpack("<8f", data)
            live = status >= 0.5
            self.flex_window.append(flex)
        elif len(data) == 28:
            roll, pitch, yaw, _lax, _lay, _laz, status = struct.unpack("<7f", data)
            live = status >= 0.5
        elif len(data) == 24:
            roll, pitch, yaw = struct.unpack("<6f", data)[:3]
            live = True  # old firmware has no status flag
        else:
            return

        self.packets += 1
        self.last_packet = time.time()
        if live != self.live:
            stamp = time.strftime("%H:%M:%S")
            if self.live is not None:
                self.events.append(
                    f"{stamp}  {'SENSOR CAME BACK' if live else 'SENSOR DROPPED OUT'}"
                )
                print(f"\n  >>> {self.events[-1]} <<<\n")
            self.live = live
        if live:
            self.window.append((roll, pitch, yaw))


async def main() -> None:
    from bleak import BleakClient, BleakScanner

    print("Scanning for the glove...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
    if dev is None:
        print(f"'{DEVICE_NAME}' not found — is the ESP32 powered?")
        return

    h = Health()
    async with BleakClient(dev) as client:
        await client.start_notify(CHAR_UUID, h.on_packet)
        print(
            "Connected. Wiggle ONE wire at a time and watch for dropouts.\n"
            "Prime suspects: RST->GPIO4, then 3V3, GND, SDA->21, SCL->22.\n"
            "Ctrl+C to stop.\n"
        )
        print(f"{'time':<10} {'status':<12} {'pkts/s':>7}  {'flex1/flex2':<14} movement (deg/s)")
        print("-" * 84)
        try:
            while client.is_connected:
                before = h.packets
                h.window.clear()
                h.flex_window.clear()
                h.flex2_window.clear()
                await asyncio.sleep(1.0)
                rate = h.packets - before
                if h.flex_window:
                    lo, hi = min(h.flex_window), max(h.flex_window)
                    h.flex_seen_min = min(h.flex_seen_min, lo)
                    h.flex_seen_max = max(h.flex_seen_max, hi)
                    flex = f"{lo:4.0f}"
                    if h.flex2_window:
                        lo2, hi2 = min(h.flex2_window), max(h.flex2_window)
                        h.flex2_seen_min = min(h.flex2_seen_min, lo2)
                        h.flex2_seen_max = max(h.flex2_seen_max, hi2)
                        flex += f"/{lo2:4.0f}"
                else:
                    flex = "-"
                if h.window:
                    spans = [max(c) - min(c) for c in zip(*h.window)]
                    move = f"roll {spans[0]:6.2f}  pitch {spans[1]:6.2f}  yaw {spans[2]:6.2f}"
                    status = "LIVE" if h.live else "NO SENSOR"
                else:
                    move = "-"
                    status = "NO SENSOR" if h.live is False else "no data"
                print(f"{time.strftime('%H:%M:%S'):<10} {status:<12} {rate:>7}  {flex:<14} {move}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    print("\n--- summary ---")
    print(f"total packets: {h.packets}")
    for label, pin, lo, hi in (("flex 1", "GPIO34", h.flex_seen_min, h.flex_seen_max),
                               ("flex 2", "GPIO35", h.flex2_seen_min, h.flex2_seen_max)):
        if hi < lo:
            continue
        span = hi - lo
        print(f"\n{label} ({pin}): range {lo:.0f}..{hi:.0f}, span {span:.0f}")
        # Divider is 3V3 -> flex -> pin -> 15k -> GND, so the resting value
        # alone says which leg is broken.
        if hi < 100:
            print(f"  -> STUCK AT 0: nothing is pulling {pin} up. Check the 3V3 leg\n"
                  f"     and the leg into {pin}. (The 15k pulldown is clearly fine —\n"
                  f"     it is what is holding the pin at 0.)")
        elif lo > 4000:
            print(f"  -> STUCK AT MAX: {pin} sits at 3.3V. The 15k pulldown to GND\n"
                  f"     is missing or disconnected.")
        elif lo < FLEX_VALID_MIN:
            # A 13-16k sensor into a 15k leg cannot go below ~1120 counts even
            # at 40k, so a low minimum is an open circuit, not a deep bend —
            # and it looks like a huge healthy span if you only read the range.
            print(f"  -> INTERMITTENT: dipped to {lo:.0f}, which this divider\n"
                  f"     cannot produce. That is the connection dropping out, not\n"
                  f"     a bend. Ignore the span above; fix the joint on {pin}\n"
                  f"     or its 3V3 leg first.")
        elif span < FLEX_MIN_SPAN:
            print(f"  -> CONNECTED but barely swinging (span {span:.0f} < "
                  f"{FLEX_MIN_SPAN:.0f}): the divider\n"
                  "     works, yet bending hardly changes it. Either the bend is not\n"
                  "     reaching the resistive strip, or the sensor is damaged.")
        else:
            # ~212 counts is the physical ceiling for a 13-16k sensor here, so
            # anything near it is as good as this hardware gets.
            print(f"  -> good swing ({span:.0f} counts; ~212 is the ceiling for a\n"
                  "     13-16k sensor into a 15k leg). Usable as a control.")
    print()
    if h.events:
        print("dropout timeline:")
        for e in h.events:
            print(f"  {e}")
        print("\nWhatever you touched at those times is your bad connection.")
    elif h.live is False:
        print(
            "The BNO08x was NEVER detected during this whole test — this is not\n"
            "an intermittent fault right now, the sensor is simply not talking.\n"
            "Check its wiring, then watch the ESP32 boot log for the I2C scan."
        )
    elif h.live is None:
        print("No packets arrived at all — is the ESP32 powered and advertising?")
    else:
        print("Sensor stayed LIVE for the whole test. No dropouts.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
