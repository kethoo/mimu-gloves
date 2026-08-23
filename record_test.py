"""Receive a recording from the glove and play it back.

Flash esp32/rec_test first, then run this, hold the glove's button and speak.
The take is transferred over serial, saved as a WAV and played, so you can
hear exactly what the microphone captured.

    python record_test.py
"""

from __future__ import annotations

import base64
import glob
import sys
import time
import wave

import numpy as np
import serial
import sounddevice as sd

BAUD = 115200
OUT = "glove_take.wav"


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.wchusbserial*"))
    if not ports:
        sys.exit("No board found — is the ESP32 plugged in?")
    return ports[0]


def main() -> None:
    port = find_port()
    ser = serial.Serial(port, BAUD, timeout=1)
    print(f"--- {port} ---")
    print("HOLD the glove's button and speak, then release.\n")

    chunks: list[str] = []
    collecting = False
    n_samples = rate = 0
    t0 = time.time()

    while time.time() - t0 < 120:
        try:
            line = ser.readline().decode(errors="replace").strip()
        except serial.SerialException:
            print("[board disconnected]")
            break
        if not line:
            continue

        if line.startswith("AUDIO_BEGIN"):
            _, n, r = line.split()
            n_samples, rate = int(n), int(r)
            collecting, chunks = True, []
            print(f"receiving {n_samples} samples at {rate} Hz...")
            continue
        if line == "AUDIO_END":
            break
        if collecting:
            chunks.append(line)
        else:
            print(line)
    ser.close()

    if not chunks:
        sys.exit("No audio received — did you hold the button?")

    raw = base64.b64decode("".join(chunks))
    audio = np.frombuffer(raw, dtype="<i2")
    print(f"decoded {len(audio)} samples ({len(audio)/rate:.2f} s)")

    peak = int(np.abs(audio).max())
    print(f"peak amplitude: {peak} of 32767")
    if peak < 300:
        print("  -> nearly silent. The mic captured almost nothing.")
    else:
        print("  -> real signal captured.")

    with wave.open(OUT, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    print(f"saved {OUT}")

    # Normalise for playback so a quiet take is still audible.
    play = audio.astype(np.float32) / 32768.0
    if peak > 0:
        play *= min(0.9 / (peak / 32768.0), 20.0)
    print("playing back...")
    sd.play(play, rate)
    sd.wait()
    print("done — that is what the glove heard.")


if __name__ == "__main__":
    main()
