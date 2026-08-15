"""Real hardware source: receives sensor frames from the ESP32 over BLE.

Matches the interface of SimulatedGloveSource, so main.py can swap them.

The glove's BNO08x does sensor fusion on-chip, so packets already contain
orientation — no fusion math needed here. We just zero the angles against
a baseline captured right after connecting (hold the glove still for a
second), smooth lightly, and derive motion/punch from linear acceleration.

Protocol (must match esp32/glove_ble/glove_ble.ino):
  24 bytes, 6 little-endian floats, ~100x/second:
    roll, pitch, yaw   -- degrees, absolute
    lax, lay, laz      -- linear acceleration (gravity removed), m/s^2

Requires:  pip install bleak
"""

from __future__ import annotations

import asyncio
import struct
import time

from sensors import GloveSource, SensorFrame

SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
DEVICE_NAME = "MIMU-GLOVE"

BASELINE_PACKETS = 80    # ~0.8 s of "hold still" used as the zero pose
PUNCH_THRESHOLD = 12.0   # linear accel magnitude (m/s^2) that counts as a punch
# Orientation smoothing. The BNO08x fuses on-chip and its output is already
# very clean (jitter measured at ~0.04 deg), so this is deliberately lighter
# than the original sketch's 0.85 — at 50 Hz that gives ~40 ms of lag instead
# of ~100 ms, which is the difference between playable and sluggish.
ALPHA = 0.5

# Flex sensor: how much ADC swing counts as a real, deliberate bend rather
# than noise. Below this the sensor is treated as absent.
FLEX_MIN_SPAN = 150.0
FLEX_INVERT = True  # bending increases resistance -> lowers the divider voltage


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


class BleGloveSource(GloveSource):
    def __init__(self) -> None:
        super().__init__()
        self._baseline: list[tuple[float, float, float]] = []
        self._offset: tuple[float, float, float] | None = None
        self._smoothed = [0.0, 0.0, 0.0]
        self._prev = (0.0, 0.0, 0.0)
        self._prev_t = time.monotonic()
        self._punch_armed = True
        self.quit_requested = False
        self._last_raw: tuple[float, float, float] | None = None
        self._frozen_since = 0.0
        self._warned_frozen = False
        self._testpattern_hits = 0
        self._warned_testpattern = False
        self._flex_min = float("inf")
        self._flex_max = float("-inf")
        self._flex_ready = False

    def _run(self) -> None:
        asyncio.run(self._ble_loop())

    async def _ble_loop(self) -> None:
        from bleak import BleakClient, BleakScanner

        device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
        if device is None:
            print(f"Could not find BLE device '{DEVICE_NAME}'. Is the glove on?")
            self.quit_requested = True
            return
        async with BleakClient(device) as client:
            print(f"Connected to {DEVICE_NAME}. Hold the glove still to calibrate...")
            await client.start_notify(CHAR_UUID, self._on_packet)
            while self._running and client.is_connected:
                await asyncio.sleep(0.2)
        print("BLE disconnected.")
        self.quit_requested = True

    def _on_packet(self, _handle, data: bytearray) -> None:
        # 32 bytes = current firmware (status + flex); 28 and 24 are older.
        flex_raw = None
        if len(data) == 32:
            roll, pitch, yaw, lax, lay, laz, status, flex_raw = struct.unpack("<8f", data)
        elif len(data) == 28:
            roll, pitch, yaw, lax, lay, laz, status = struct.unpack("<7f", data)
        elif len(data) == 24:
            roll, pitch, yaw, lax, lay, laz = struct.unpack("<6f", data)
            status = 1.0
        else:
            return

        if status < 0.5:
            self._testpattern_hits += 1
            if self._testpattern_hits > 50 and not self._warned_testpattern:
                self._warned_testpattern = True
                print(
                    "\n[!! The ESP32 cannot see the BNO08x — no motion data is"
                    "\n    being produced. It retries every 3s, so fix the wiring"
                    "\n    (VCC/GND/SDA=21/SCL=22, RST=GPIO4) and it will recover"
                    "\n    on its own — no reflash, no restart needed.]"
                )
            return  # never feed placeholder values into the synth
        if self._warned_testpattern:
            print("\n[sensor recovered — playing real motion]")
        self._testpattern_hits = 0
        self._warned_testpattern = False

        # A live BNO08x always jitters slightly. Bit-identical packets mean
        # its I2C transport has wedged and the ESP32 is resending stale
        # values — otherwise indistinguishable from "holding perfectly still".
        now = time.monotonic()
        current = (roll, pitch, yaw)
        if current == self._last_raw:
            if self._frozen_since and now - self._frozen_since > 5.0:
                if not self._warned_frozen:
                    print(
                        "\n[WARNING: sensor values frozen for 5s — the BNO08x "
                        "has stalled. Power-cycle the glove.]"
                    )
                    self._warned_frozen = True
        else:
            self._frozen_since = now
            self._warned_frozen = False
        self._last_raw = current

        # First ~0.8 s of packets define the neutral hand pose.
        if self._offset is None:
            self._baseline.append((roll, pitch, yaw))
            if len(self._baseline) < BASELINE_PACKETS:
                return
            n = len(self._baseline)
            self._offset = (
                sum(b[0] for b in self._baseline) / n,
                sum(b[1] for b in self._baseline) / n,
                sum(b[2] for b in self._baseline) / n,
            )
            print("[glove calibrated — play!]")

        raw = (
            _wrap(roll - self._offset[0]),
            _wrap(pitch - self._offset[1]),
            _wrap(yaw - self._offset[2]),
        )
        for i in range(3):
            self._smoothed[i] = ALPHA * self._smoothed[i] + (1 - ALPHA) * raw[i]
        r, p, y = self._smoothed

        now = time.monotonic()
        dt = max(now - self._prev_t, 1e-3)
        speed = (
            abs(r - self._prev[0]) + abs(p - self._prev[1]) + abs(y - self._prev[2])
        ) / dt
        self._prev = (r, p, y)
        self._prev_t = now

        accel_mag = (lax * lax + lay * lay + laz * laz) ** 0.5
        if accel_mag > PUNCH_THRESHOLD and self._punch_armed:
            self.events.append("punch")
            self._punch_armed = False
        elif accel_mag < 3.0:
            self._punch_armed = True

        # When the glove gets buttons (record/overdub...), extend the packet
        # with a button byte and append those events here on press edges.

        motion = min(speed / 400.0 + accel_mag / 30.0, 1.5)
        self.latest = SensorFrame(
            roll=r, pitch=p, yaw=y, motion=motion, flex=self._flex(flex_raw)
        )

    def _flex(self, raw: float | None) -> float | None:
        """Normalize the flex ADC to 0..1 (0 straight, 1 fully bent).

        The usable range depends on the sensor, the divider resistor and how
        it is taped to the finger, so it self-calibrates: the observed min and
        max expand as you bend. Until it has seen a real span it reports None,
        which the mapping treats as "no flex sensor" rather than guessing.
        """
        if raw is None:
            return None
        self._flex_min = min(self._flex_min, raw)
        self._flex_max = max(self._flex_max, raw)
        span = self._flex_max - self._flex_min
        if span < FLEX_MIN_SPAN:
            return None
        u = (raw - self._flex_min) / span
        if not self._flex_ready:
            self._flex_ready = True
            print(f"\n[flex sensor detected — range {self._flex_min:.0f}..{self._flex_max:.0f}]")
        # Bending raises the flex sensor's resistance, which pulls the divider
        # voltage DOWN, so invert to get "bent = 1".
        return 1.0 - u if FLEX_INVERT else u
