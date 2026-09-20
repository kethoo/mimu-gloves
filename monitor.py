"""Serial monitor for the glove's ESP32.

macOS renames the port every time the board re-enumerates
(/dev/cu.usbserial-10, -110, ...), so this finds it instead of hardcoding it,
and waits for the board if it is unplugged.

    python monitor.py           watch forever (Ctrl+C to stop)
    python monitor.py 20        watch for 20 seconds
    python monitor.py --identify   which glove is on which port
"""

from __future__ import annotations

import glob
import sys
import time

import serial

BAUD = 115200


def find_port(wait: bool = True) -> str | None:
    announced = False
    while True:
        ports = sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.wchusbserial*")
                       + glob.glob("/dev/cu.SLAB_USBtoUART*"))
        if ports:
            return ports[0]
        if not wait:
            return None
        if not announced:
            print("waiting for the board... (plug in USB)")
            announced = True
        time.sleep(1)


def open_port(port: str) -> serial.Serial:
    """Open without driving DTR/RTS.

    Those lines are wired to EN and IO0 on the auto-reset circuit, so opening
    the port the default way can hold the ESP32 in reset or drop it into the
    bootloader — which looks exactly like a dead board printing nothing.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    ser.open()
    ser.dtr = False
    ser.rts = False
    return ser


def identify() -> None:
    """Say which glove is on which serial port.

    With two boards plugged in, `ls /dev/cu.usbserial-* | head -1` picks one
    arbitrarily — so flashing without checking can put the voice firmware on
    the instrument board. Each board announces its own DEVICE_NAME at boot,
    so resetting it and reading the banner is the reliable way to tell.
    """
    ports = sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.wchusbserial*"))
    if not ports:
        print("No ESP32 serial ports found.")
        return
    print(f"{len(ports)} board(s) connected:\n")
    for port in ports:
        ser = open_port(port)
        ser.reset_input_buffer()
        ser.dtr = True          # pulse EN to force a reboot
        time.sleep(0.15)
        ser.dtr = False
        name, deadline = None, time.time() + 8
        while time.time() < deadline and name is None:
            line = ser.readline().decode(errors="replace").strip()
            if line.startswith("Advertising as "):
                name = line.split("Advertising as ", 1)[1].strip()
        ser.close()
        hand = {"MIMU-GLOVE-I": "instrument hand",
                "MIMU-GLOVE-V": "voice hand"}.get(
                    name, "older un-suffixed firmware" if name else "no banner seen")
        print(f"  {port}")
        print(f"      advertises as: {name or '(unknown)'}   -> {hand}")
        print(f"      flash it with: -p {port}\n")


def main() -> None:
    if "--identify" in sys.argv:
        identify()
        return
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else None
    port = find_port()
    print(f"--- {port} @ {BAUD} ---")
    ser = open_port(port)
    t0 = time.time()
    try:
        while limit is None or time.time() - t0 < limit:
            try:
                line = ser.readline().decode(errors="replace").strip()
            except serial.SerialException:
                # The board reset and dropped off the USB bus; reattach to it.
                print("\n[board disconnected — waiting for it to come back]")
                ser.close()
                port = find_port()
                print(f"--- reconnected on {port} ---")
                ser = open_port(port)
                continue
            if line:
                print(line)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
