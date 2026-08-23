"""Real hardware source: receives sensor frames from the ESP32 over BLE.

Matches the interface of SimulatedGloveSource, so main.py can swap them.

The glove's BNO08x does sensor fusion on-chip, so packets already contain
orientation — no fusion math needed here. We just zero the angles against
a baseline captured right after connecting (hold the glove still for a
second), smooth lightly, and derive motion/punch from linear acceleration.

Reconnects on its own: the glove reboots itself to recover a wedged sensor,
and that must not end a performance.

Protocol (must match esp32/glove_ble/glove_ble.ino):
  32 bytes, 8 little-endian floats, 50x/second:
    roll, pitch, yaw   -- degrees, absolute
    lax, lay, laz      -- linear acceleration (gravity removed), m/s^2
    status             -- 1 = live sensor, 0 = sensor not detected
    flex               -- raw ADC 0..4095 from the flex divider
  (28- and 24-byte packets from older firmware are still accepted.)

Requires:  pip install bleak
"""

from __future__ import annotations

import asyncio
import struct
import time

import numpy as np

from sensors import GloveSource, SensorFrame

SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
AUDIO_UUID = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
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


class _FlexChannel:
    """Self-calibrating flex input, one per finger.

    The usable ADC range depends on the sensor, the divider resistor and how
    tightly it is taped to the finger, so the range widens as you bend rather
    than being hardcoded. Until it has seen a real swing it reports None,
    which the mapping treats as "no sensor" instead of guessing.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.lo = float("inf")
        self.hi = float("-inf")
        self.ready = False

    def update(self, raw):
        if raw is None:
            return None
        self.lo = min(self.lo, raw)
        self.hi = max(self.hi, raw)
        span = self.hi - self.lo
        if span < FLEX_MIN_SPAN:
            return None
        u = (raw - self.lo) / span
        if not self.ready:
            self.ready = True
            print(f"\n[{self.name} detected — range {self.lo:.0f}..{self.hi:.0f}]")
        # Bending raises the sensor's resistance, pulling the divider down.
        return 1.0 - u if FLEX_INVERT else u


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
        self._flex = _FlexChannel("flex sensor 1")
        self._flex2 = _FlexChannel("flex sensor 2")
        # Audio arriving from the glove's own microphone.
        self._audio_expected = 0
        self._audio_rate = 16000
        self._audio_parts: list[bytes] = []

    def _run(self) -> None:
        asyncio.run(self._ble_loop())

    async def _ble_loop(self) -> None:
        """Stay connected for as long as the app runs.

        The glove reboots itself to recover a wedged sensor, and BLE links
        drop for ordinary radio reasons. Neither should end a performance, so
        this reconnects indefinitely; only stop() ends the loop.
        """
        from bleak import BleakClient, BleakScanner

        searching_announced = False
        while self._running:
            device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10)
            if device is None:
                if not searching_announced:
                    print(
                        f"\n[looking for '{DEVICE_NAME}' — is the glove powered? "
                        "still searching...]"
                    )
                    searching_announced = True
                continue
            searching_announced = False
            try:
                async with BleakClient(device) as client:
                    # The glove re-zeroes its own reference when it reboots, so
                    # the pose baseline must be recaptured on every connection.
                    self._reset_pose_calibration()
                    print(
                        f"Connected to {DEVICE_NAME}. "
                        "Hold the glove still to calibrate..."
                    )
                    await client.start_notify(CHAR_UUID, self._on_packet)
                    await client.start_notify(AUDIO_UUID, self._on_audio)
                    while self._running and client.is_connected:
                        await asyncio.sleep(0.2)
            except Exception as exc:  # dropped mid-transfer, adapter busy, ...
                print(f"\n[BLE connection lost: {exc}]")
            if self._running:
                print("\n[glove disconnected — reconnecting...]")
                await asyncio.sleep(1.0)
        self.quit_requested = True

    def _on_audio(self, _handle, data: bytearray) -> None:
        """Reassemble a take from the glove: a 12-byte header, then raw
        little-endian int16 samples."""
        if not self._audio_expected and len(data) >= 12 and data[:4] == b"AUD0":
            n, rate = struct.unpack("<II", bytes(data[4:12]))
            self._audio_expected = n * 2
            self._audio_rate = rate
            self._audio_parts = []
            print(f"\n[glove recording: {n / rate:.1f}s incoming...]")
            return
        if not self._audio_expected:
            return
        self._audio_parts.append(bytes(data))
        if sum(len(p) for p in self._audio_parts) < self._audio_expected:
            return

        raw = b"".join(self._audio_parts)[: self._audio_expected]
        self._audio_expected = 0
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        peak = float(np.abs(samples).max()) if len(samples) else 0.0
        if peak < 0.005:
            print("[glove recording was silent — discarded]")
            return
        self.audio = (samples, self._audio_rate)
        print(f"[glove recording received: {len(samples) / self._audio_rate:.1f}s, "
              f"peak {peak:.2f} — now playing as the loop]")

    def _reset_pose_calibration(self) -> None:
        """Forget the neutral pose so the next packets re-establish it.
        Flex calibration is kept: that sensor's range does not change."""
        self._baseline.clear()
        self._offset = None
        self._smoothed = [0.0, 0.0, 0.0]
        self._prev = (0.0, 0.0, 0.0)

    def _on_packet(self, _handle, data: bytearray) -> None:
        # 36 bytes = current firmware (status + two flex); 32/28/24 are older.
        flex_raw = flex2_raw = None
        if len(data) == 36:
            (roll, pitch, yaw, lax, lay, laz,
             status, flex_raw, flex2_raw) = struct.unpack("<9f", data)
        elif len(data) == 32:
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
            roll=r, pitch=p, yaw=y, motion=motion,
            flex=self._flex.update(flex_raw),
            flex2=self._flex2.update(flex2_raw),
        )

