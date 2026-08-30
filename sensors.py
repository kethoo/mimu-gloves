"""Sensor sources for the glove.

Every source produces SensorFrame objects and exposes the same interface,
so the synth/mapping code never knows whether data is simulated or real.
When the ESP32 hardware is ready, swap SimulatedGloveSource for
BleGloveSource in main.py and nothing else changes.
"""

from __future__ import annotations

import math
import select
import sys
import termios
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SensorFrame:
    """One snapshot of hand state, ~100 times per second.

    roll:   rotation around forearm axis, degrees (-90 palm-left .. +90 palm-right)
    pitch:  tilt up/down, degrees (-90 pointing down .. +90 pointing up)
    yaw:    heading left/right, degrees (-90 .. +90)
    motion: overall movement intensity, 0.0 (still) .. 1.0+ (shaking)
    flex:   first finger bend, 0.0 (straight) .. 1.0 (fully bent), or None
            if that sensor is missing
    flex2:  second finger bend, same scale, or None
    flex_raw, flex2_raw:
            uncalibrated ADC counts straight off the glove, or None. Only
            for diagnostics: it is the difference between "the sensor is
            dead" and "the sensor works but has not swung far enough for
            the auto-calibration to trust it yet".
    """

    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    motion: float = 0.0
    flex: float | None = None
    flex2: float | None = None
    flex_raw: float | None = None
    flex2_raw: float | None = None
    t: float = field(default_factory=time.monotonic)


class GloveSource:
    """Base class: call start(), then read .latest whenever you want.

    Continuous state (orientation) lives in .latest and can safely be
    sampled or skipped. Discrete gestures (punch, button presses) are
    queued as events so none are ever missed or double-handled:
    "punch", "record", "loop", "mute".
    """

    def __init__(self) -> None:
        self.latest = SensorFrame()
        self.events: deque[str] = deque()
        # A recording captured on the glove itself, as (samples, rate), waiting
        # to be collected. Same idea as the event queue: produced here, taken
        # exactly once by whoever is driving the synth.
        self.audio: tuple = None
        self._running = False

    def drain_events(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.events.popleft())
            except IndexError:
                return out

    def take_audio(self):
        """Collect a pending glove recording, or None. Clears it."""
        audio, self.audio = self.audio, None
        return audio

    def _smooth_toward_target(self, rate: int) -> None:
        """Chase self._target with a first-order lag (real hands don't
        teleport) and derive motion intensity from how fast we're moving.
        Shared by the keyboard and web simulators."""
        dt = 1.0 / rate
        prev = self.latest
        while self._running:
            tgt = self._target
            k = 1.0 - math.exp(-dt / 0.08)
            f = SensorFrame(
                roll=prev.roll + (tgt.roll - prev.roll) * k,
                pitch=prev.pitch + (tgt.pitch - prev.pitch) * k,
                yaw=prev.yaw + (tgt.yaw - prev.yaw) * k,
                # Finger bend passes straight through: it is set directly
                # rather than nudged, and the postures built on it need the
                # value the player actually asked for.
                flex=tgt.flex,
                flex2=tgt.flex2,
            )
            speed = (
                abs(f.roll - prev.roll)
                + abs(f.pitch - prev.pitch)
                + abs(f.yaw - prev.yaw)
            ) / dt
            f.motion = min(speed / 400.0, 1.5)
            self.latest = f
            prev = f
            time.sleep(dt)

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        raise NotImplementedError


class MergedGloveSource(GloveSource):
    """Hand pose from one source, control events from both.

    Lets the real glove drive orientation while the browser UI supplies the
    buttons the hardware doesn't have yet (instruments, record, modes).
    Both children keep their own interface, so this stays a pure composition.
    """

    RATE = 100

    def __init__(self, pose: GloveSource, control: GloveSource) -> None:
        super().__init__()
        self.pose = pose
        self.control = control

    @property
    def quit_requested(self) -> bool:
        return getattr(self.pose, "quit_requested", False) or getattr(
            self.control, "quit_requested", False
        )

    def publish(self, state: dict) -> None:
        publish = getattr(self.control, "publish", None)
        if publish is not None:
            publish(state)

    def start(self) -> None:
        self.pose.start()
        self.control.start()
        super().start()

    def stop(self) -> None:
        self.pose.stop()
        self.control.stop()
        super().stop()

    def _run(self) -> None:
        dt = 1.0 / self.RATE
        while self._running:
            self.latest = self.pose.latest
            for source in (self.pose, self.control):
                self.events.extend(source.drain_events())
                got = source.take_audio()
                if got is not None:
                    self.audio = got
            time.sleep(dt)


class SimulatedGloveSource(GloveSource):
    """Keyboard-driven fake glove.

    a/d = roll (brightness)    w/s = tilt up/down (musical pitch)
    q/e = yaw left/right       space = wrist flick (drum hit)
    [/] = index finger bend    ;/' = middle finger bend
    n   = next scene           v = record/stop voice
    o   = overdub a layer      p = play/pause loop
    g   = granular mode        b = slice mode on/off
    m   = mute/unmute drone    r = reset to neutral
    x   = quit

    Bend both fingers for a fist (sound on), straighten both for an open
    hand (sound off), index straight + middle bent to change instrument.
    Keys nudge the hand; it also drifts slowly back toward neutral,
    which feels a bit like a real hand relaxing.
    """

    RATE = 100  # Hz, matches what a real IMU stream would give us

    def __init__(self) -> None:
        super().__init__()
        self._target = SensorFrame()
        self.quit_requested = False

    def _run(self) -> None:
        threading.Thread(target=self._read_keys, daemon=True).start()
        self._smooth_toward_target(self.RATE)

    def _read_keys(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    continue
                c = sys.stdin.read(1).lower()
                t = self._target
                step = 15.0
                if c == "a":
                    t.roll = max(t.roll - step, -90)
                elif c == "d":
                    t.roll = min(t.roll + step, 90)
                elif c == "w":
                    t.pitch = min(t.pitch + step, 90)
                elif c == "s":
                    t.pitch = max(t.pitch - step, -90)
                elif c == "q":
                    t.yaw = max(t.yaw - step, -90)
                elif c == "e":
                    t.yaw = min(t.yaw + step, 90)
                elif c == " ":
                    self.events.append("punch")
                elif c == "v":
                    self.events.append("record")
                elif c == "o":
                    self.events.append("overdub")
                elif c == "p":
                    self.events.append("loop")
                elif c == "g":
                    self.events.append("granular")
                elif c == "b":
                    self.events.append("slices")
                elif c == "m":
                    self.events.append("mute")
                elif c == "l":
                    self.events.append("live")
                elif c == "n":
                    self.events.append("scene")
                elif c in "[]":
                    # index finger bend: volume, and half of every posture
                    t.flex = min(max((t.flex or 0.0) + (0.2 if c == "]" else -0.2), 0.0), 1.0)
                elif c in ";'":
                    t.flex2 = min(max((t.flex2 or 0.0) + (0.2 if c == "'" else -0.2), 0.0), 1.0)
                elif c in "1234567":
                    names = ["saw", "organ", "strings", "bell", "flute",
                             "pluck", "guitar"]
                    self.events.append("instrument:" + names[int(c) - 1])
                elif c == "r":
                    t.roll = t.pitch = t.yaw = 0.0
                elif c == "x":
                    self.quit_requested = True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


class DemoGloveSource(GloveSource):
    """Hands-free demo: plays a scripted sequence of hand gestures."""

    RATE = 100

    # (description, seconds, function of local time -> (roll, pitch, yaw, punch))
    SCRIPT = [
        ("neutral hand, steady tone", 2.0,
         lambda u: (0, 0, 0, False)),
        ("tilt hand up -> pitch rises", 3.0,
         lambda u: (0, 80 * u, 0, False)),
        ("tilt hand down -> pitch falls", 3.0,
         lambda u: (0, 80 - 160 * u, 0, False)),
        ("rotate wrist -> sound brightens", 3.0,
         lambda u: (80 * u, -80 + 80 * u, 0, False)),
        ("sweep yaw -> sound pans left/right", 4.0,
         lambda u: (70, 0, 70 * math.sin(2 * math.pi * u), False)),
        ("wrist flick! percussive hit", 1.5,
         lambda u: (20, 0, 0, u < 0.05)),
        ("flick again", 1.5,
         lambda u: (-20, 30, 0, u < 0.05)),
        ("wave goodbye (fast rolls)", 4.0,
         lambda u: (60 * math.sin(6 * math.pi * u), 30, 0, False)),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.quit_requested = False

    def _run(self) -> None:
        dt = 1.0 / self.RATE
        prev_roll = prev_pitch = prev_yaw = 0.0
        prev_punch = False
        for desc, dur, fn in self.SCRIPT:
            if not self._running:
                return
            print(f"\n>> {desc}")
            steps = int(dur / dt)
            for i in range(steps):
                if not self._running:
                    return
                roll, pitch, yaw, punch = fn(i / steps)
                if punch and not prev_punch:
                    self.events.append("punch")
                prev_punch = bool(punch)
                speed = (
                    abs(roll - prev_roll)
                    + abs(pitch - prev_pitch)
                    + abs(yaw - prev_yaw)
                ) / dt
                self.latest = SensorFrame(
                    roll=roll, pitch=pitch, yaw=yaw,
                    motion=min(speed / 400.0, 1.5),
                )
                prev_roll, prev_pitch, prev_yaw = roll, pitch, yaw
                time.sleep(dt)
        print("\nDemo finished.")
        self.quit_requested = True
