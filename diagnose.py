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

CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "MIMU-GLOVE"


class Health:
    def __init__(self) -> None:
        self.window: list[tuple[float, float, float]] = []
        self.flex_window: list[float] = []
        self.flex_seen_min = float("inf")
        self.flex_seen_max = float("-inf")
        self.packets = 0
        self.live = None          # None = unknown yet
        self.events: list[str] = []
        self.last_packet = 0.0

    def on_packet(self, _handle, data: bytearray) -> None:
        if len(data) == 32:
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
        print(f"{'time':<10} {'status':<12} {'pkts/s':>7}  {'flex ADC':<18} movement (deg/s)")
        print("-" * 84)
        try:
            while client.is_connected:
                before = h.packets
                h.window.clear()
                h.flex_window.clear()
                await asyncio.sleep(1.0)
                rate = h.packets - before
                if h.flex_window:
                    lo, hi = min(h.flex_window), max(h.flex_window)
                    h.flex_seen_min = min(h.flex_seen_min, lo)
                    h.flex_seen_max = max(h.flex_seen_max, hi)
                    flex = f"{lo:4.0f} (all {h.flex_seen_min:.0f}-{h.flex_seen_max:.0f})"
                else:
                    flex = "-"
                if h.window:
                    spans = [max(c) - min(c) for c in zip(*h.window)]
                    move = f"roll {spans[0]:6.2f}  pitch {spans[1]:6.2f}  yaw {spans[2]:6.2f}"
                    status = "LIVE" if h.live else "NO SENSOR"
                else:
                    move = "-"
                    status = "NO SENSOR" if h.live is False else "no data"
                print(f"{time.strftime('%H:%M:%S'):<10} {status:<12} {rate:>7}  {flex:<18} {move}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    print("\n--- summary ---")
    print(f"total packets: {h.packets}")
    if h.flex_seen_max >= h.flex_seen_min:
        lo, hi = h.flex_seen_min, h.flex_seen_max
        span = hi - lo
        print(f"flex ADC range seen: {lo:.0f}..{hi:.0f} (span {span:.0f})")
        # The divider is 3V3 -> flex -> GPIO34 -> 47k -> GND, so the resting
        # value alone says which leg is broken.
        if hi < 100:
            print(
                "  -> STUCK AT 0: nothing is pulling GPIO34 up. The flex sensor is\n"
                "     not connected — check its 3V3 leg and the leg into GPIO34.\n"
                "     (The 47k pulldown is clearly fine; it is holding the pin at 0.)"
            )
        elif lo > 4000:
            print(
                "  -> STUCK AT MAX: GPIO34 is sitting at 3.3V. The 47k pulldown to\n"
                "     GND is missing or disconnected."
            )
        elif span < 150:
            print(
                "  -> CONNECTED but not swinging: the divider works, yet bending does\n"
                "     not change it. Either the bend is not reaching the resistive\n"
                "     strip (check which side is mounted), or the sensor is damaged."
            )
        else:
            print("  -> good swing; the flex sensor is usable as a control.")
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
