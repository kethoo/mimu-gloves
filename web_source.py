"""Browser-driven glove source: the frontend (ui.html) is the glove.

Runs a WebSocket server on localhost. The page sends hand poses (from the
XY pad / yaw slider) and button events; we send back a state snapshot 10x a
second so the UI can light up modes, show the loop length, etc.

Same GloveSource interface as the keyboard simulator and the BLE receiver.
Requires:  pip install websockets
"""

from __future__ import annotations

import asyncio
import json
import threading

from sensors import GloveSource, SensorFrame

PORT = 8765

ALLOWED_EVENTS = {"punch", "record", "overdub", "loop", "granular", "slices", "mute", "live"}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


class WebGloveSource(GloveSource):
    RATE = 100

    def __init__(self, port: int = PORT) -> None:
        super().__init__()
        self.port = port
        self._target = SensorFrame()
        self.state: dict = {}
        self.quit_requested = False

    def publish(self, state: dict) -> None:
        """Called by the main loop with the current synth state for the UI."""
        self.state = state

    def _run(self) -> None:
        threading.Thread(target=self._serve, daemon=True).start()
        self._smooth_toward_target(self.RATE)

    def _serve(self) -> None:
        asyncio.run(self._serve_async())

    async def _serve_async(self) -> None:
        import websockets

        async def handler(ws) -> None:
            async def send_state() -> None:
                while True:
                    await ws.send(json.dumps(self.state))
                    await asyncio.sleep(0.1)

            sender = asyncio.create_task(send_state())
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = msg.get("type")
                    if kind == "pose":
                        t = self._target
                        t.roll = _clamp(msg.get("roll", t.roll), -90, 90)
                        t.pitch = _clamp(msg.get("pitch", t.pitch), -90, 90)
                        t.yaw = _clamp(msg.get("yaw", t.yaw), -90, 90)
                    elif kind == "event":
                        name = msg.get("name", "")
                        if name in ALLOWED_EVENTS or name.startswith("instrument:"):
                            self.events.append(name)
            finally:
                sender.cancel()

        async with websockets.serve(handler, "127.0.0.1", self.port):
            print(f"UI socket listening on ws://127.0.0.1:{self.port}")
            await asyncio.Future()  # serve forever
