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
SCENE_BURST_CAP = 4      # more missed presses than this means a glove reboot
FROZEN_WARN_S = 2.0      # identical orientation for this long = stalled sensor
# Orientation smoothing. The BNO08x fuses on-chip and its output is already
# very clean (jitter measured at ~0.04 deg), so this is deliberately lighter
# than the original sketch's 0.85 — at 50 Hz that gives ~40 ms of lag instead
# of ~100 ms, which is the difference between playable and sluggish.
ALPHA = 0.5

# Flex sensor: how much ADC swing counts as a real, deliberate bend rather
# than noise. Below this the sensor is treated as absent.
#
# Sized against the actual hardware, not a guess: a 13-16k sensor into a 15k
# leg swings 3.3*15/(13+15) - 3.3*15/(16+15) = 0.17 V, which is only ~212 of
# 4095 counts end to end. The old 150 demanded 71% of that before a channel
# would come alive, so a normal bend never registered.
FLEX_MIN_SPAN = 80.0
FLEX_INVERT = True  # bending increases resistance -> lowers the divider voltage
# Set True if the two sensors are wired to the opposite pins from what the
# mapping expects — i.e. bending the index finger moves vibrato instead of
# volume. Swapping here costs nothing and saves unpicking the glove.
FLEX_SWAP = False

# Readings this low are electrically impossible for the real divider and mean
# the connection dropped out, not that a finger bent. 3V3 -> flex -> pin ->
# 15k -> GND with a 13-16k sensor sits around 1980-2195 counts; even a 40k
# sensor could only fall to ~1120. Anything under this is an open circuit.
#
# They have to be rejected rather than merely ignored downstream: calibration
# tracks the min and max ever seen, so a single 96-count dropout permanently
# rescales the channel and squashes every real bend into the bottom tenth of
# its range for the rest of the session.
FLEX_VALID_MIN = 800.0


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


class _Discovery:
    """One BLE scanner shared by every glove.

    Two gloves means two reconnect coroutines, and running two BleakScanners
    at once is unreliable on CoreBluetooth. So scanning is serialized behind a
    lock and the results are handed out by name: whoever asks first pays for
    the scan, and the other hand takes its device from the same sweep.
    """

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._seen: dict = {}

    def _take(self, name: str):
        """Exact match first, then prefix — so 'MIMU-GLOVE' still finds a
        glove running the older un-suffixed firmware."""
        if name in self._seen:
            return self._seen.pop(name)
        for found in list(self._seen):
            if found.startswith(name):
                return self._seen.pop(found)
        return None

    async def find(self, name: str):
        async with self._lock:
            device = self._take(name)
            if device is not None:
                return device          # another hand's scan already found it
            from bleak import BleakScanner

            # `return_adv=True` and `adv.local_name` are load-bearing, not
            # style. CoreBluetooth caches a peripheral's name from the first
            # time it ever saw it, so after reflashing a board to a new hand
            # `dev.name` keeps reporting the OLD name indefinitely while
            # `adv.local_name` carries what it is actually advertising now.
            # Measured on this machine: dev.name "MIMU-GLOVE",
            # adv.local_name "MIMU-GLOVE-V", same device, same instant.
            self._seen = {}
            found = await BleakScanner.discover(
                timeout=self.timeout, return_adv=True
            )
            for dev, adv in found.values():
                resolved = adv.local_name or dev.name
                if resolved:
                    self._seen[resolved] = dev
            return self._take(name)


def run_gloves(sources: "list[BleGloveSource]") -> None:
    """Drive several gloves from ONE background thread and ONE event loop.

    Do not call `start()` on each source instead: that spawns a thread per
    glove, each with its own `asyncio.run`, so two independent event loops end
    up driving bleak concurrently. One loop with `gather` is the supported
    shape, and it lets the gloves share a single scanner.
    """
    import threading

    async def _all() -> None:
        discovery = _Discovery()
        await asyncio.gather(*(s._ble_loop(discovery) for s in sources))

    for src in sources:
        src._running = True
    threading.Thread(target=lambda: asyncio.run(_all()), daemon=True).start()


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
        self.last: float | None = None   # last good mapped value
        self.dropouts = 0                # count of rejected readings

    @property
    def span(self) -> float:
        """ADC counts seen so far. Below FLEX_MIN_SPAN the channel stays
        None, so this is what to watch while bending a finger."""
        return 0.0 if self.hi < self.lo else self.hi - self.lo

    def update(self, raw):
        if raw is None:
            return None
        if raw < FLEX_VALID_MIN:
            # Dropout. Hold the last good value rather than poisoning the
            # calibration or flapping the mapped output to zero.
            self.dropouts += 1
            return self.last
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
        self.last = 1.0 - u if FLEX_INVERT else u
        return self.last


class BleGloveSource(GloveSource):
    """One glove. Two of these can run side by side — see run_gloves().

    `name` is the BLE advertised name to look for; matching is by prefix, so
    the default finds a glove on either the old single-hand firmware
    ("MIMU-GLOVE") or the new suffixed builds. `tag` prefixes every event this
    glove emits ("V:punch"), which is how main.py tells the hands apart.
    """

    def __init__(self, name: str = DEVICE_NAME, tag: str = "",
                 label: str | None = None, managed: bool = False) -> None:
        super().__init__()
        self.name = name
        self.tag = tag
        self.label = label or name
        # `managed` means run_gloves() owns this source's event loop, so
        # start() must not spawn a second thread of its own. Anything that
        # composes sources (MergedGloveSource) still calls start() blindly.
        self.managed = managed
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
        # Re-seeded on every reconnect too, so a glove that rebooted with a
        # higher press count does not fire a burst of scene changes.
        self._presses: int | None = None
        # Audio arriving from the glove's own microphone.
        self._audio_expected = 0
        self._audio_rate = 16000
        self._audio_parts: list[bytes] = []

    @property
    def flex_spans(self) -> tuple[float, float]:
        """ADC swing each flex channel has seen. Until one reaches
        FLEX_MIN_SPAN that channel reports None, so this is the number to
        watch when a finger 'does nothing'."""
        return (self._flex.span, self._flex2.span)

    def start(self) -> None:
        if self.managed:
            return          # run_gloves() already has it
        super().start()

    def _run(self) -> None:
        asyncio.run(self._ble_loop(_Discovery()))

    async def _ble_loop(self, discovery: "_Discovery") -> None:
        """Stay connected for as long as the app runs.

        The glove reboots itself to recover a wedged sensor, and BLE links
        drop for ordinary radio reasons. Neither should end a performance, so
        this reconnects indefinitely; only stop() ends the loop.

        Takes a shared `discovery` rather than scanning itself: two gloves
        means two of these coroutines, and two concurrent BleakScanners is
        unreliable on CoreBluetooth.
        """
        from bleak import BleakClient

        searching_announced = False
        while self._running:
            device = await discovery.find(self.name)
            if device is None:
                if not searching_announced:
                    print(
                        f"\n[looking for '{self.name}' — is the glove powered? "
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
                        f"Connected to {self.label}. "
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
        self._presses = None  # re-seed from the first packet of this session

    def _on_packet(self, _handle, data: bytearray) -> None:
        # 40 bytes = current firmware (adds the scene-press counter);
        # 36/32/28/24 are older builds and still decode.
        flex_raw = flex2_raw = None
        presses = None
        if len(data) == 40:
            (roll, pitch, yaw, lax, lay, laz,
             status, flex_raw, flex2_raw, presses) = struct.unpack("<10f", data)
        elif len(data) == 36:
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
            # 2 s, not 5: measured, a stalled BNO08x on this hardware only
            # stays frozen for ~3 s before the firmware gives up and reboots
            # the ESP32, so a 5 s threshold never fired and the freeze looked
            # like a mapping bug instead of a stalled sensor.
            if self._frozen_since and now - self._frozen_since > FROZEN_WARN_S:
                if not self._warned_frozen:
                    print(
                        f"\n[WARNING: {self.label} orientation frozen for "
                        f"{FROZEN_WARN_S:.0f}s — the BNO08x has stalled. The link is "
                        "fine; the sensor is not. Check its 3V3 and SDA/SCL.]"
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
            self.events.append(self.tag + "punch")
            self._punch_armed = False
        elif accel_mag < 3.0:
            self._punch_armed = True

        # Short button presses arrive as a running count, not a pulse, so a
        # dropped notification costs nothing: whatever the count has advanced
        # by since the last packet is how many presses we missed. Seed from
        # the first packet rather than 0, or reconnecting to a glove that has
        # been running a while would fire a burst of scene changes.
        if presses is not None:
            count = int(presses)
            if self._presses is None:
                self._presses = count
            elif count != self._presses:
                # Cap the catch-up: a counter that jumped by hundreds means a
                # glove reboot, not that someone pressed the button 300 times.
                missed = count - self._presses
                for _ in range(missed if 0 < missed <= SCENE_BURST_CAP else 1):
                    self.events.append(self.tag + "scene")
                self._presses = count

        if FLEX_SWAP:
            flex_raw, flex2_raw = flex2_raw, flex_raw

        motion = min(speed / 400.0 + accel_mag / 30.0, 1.5)
        self.latest = SensorFrame(
            roll=r, pitch=p, yaw=y, motion=motion,
            flex=self._flex.update(flex_raw),
            flex2=self._flex2.update(flex2_raw),
            flex_raw=flex_raw,
            flex2_raw=flex2_raw,
        )

