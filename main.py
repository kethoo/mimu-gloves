"""MiMu-style glove instrument — simulation entry point.

Usage:
    python main.py             interactive keyboard "glove"
    python main.py --web       browser frontend: XY pad, instruments, buttons
    python main.py --demo      scripted gesture demo, no keys needed

With the hardware glove:
    python main.py --ble --web glove plays; browser picks instruments/modes
    python main.py --ble       glove only
    python main.py --scan      list BLE devices; check the glove is advertising

Add --midi to any of the above to also stream notes and CCs to a DAW (see
midi_out.py). The built-in synth keeps playing; --midi is an extra output,
not a replacement, so you can A/B the two.
"""

from __future__ import annotations

import pathlib
import sys
import time
import webbrowser

import mapping
from sensors import DemoGloveSource, SimulatedGloveSource
from synth import GloveSynth

CONTROL_RATE = 100  # Hz


def _flag_value(flag: str) -> str | None:
    """Value of a `--flag VALUE` argument, or None if absent."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def scan() -> None:
    """List nearby BLE devices. The glove never appears in macOS Bluetooth
    settings — BLE peripherals don't pair with the OS — so this is how you
    check that it is powered and advertising."""
    import asyncio

    from bleak import BleakScanner

    async def run() -> None:
        found = await BleakScanner.discover(timeout=8, return_adv=True)
        glove = None
        for addr, (dev, adv) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
            name = adv.local_name or dev.name or "(unnamed)"
            if name == "MIMU-GLOVE":
                glove = (addr, adv.rssi)
            print(f"  {name:<22} {addr}  rssi {adv.rssi}")
        print()
        if glove:
            print(f"GLOVE FOUND at {glove[0]} (signal {glove[1]} dBm) — run: python main.py --ble")
        else:
            print("GLOVE NOT FOUND — is the ESP32 powered? It advertises as 'MIMU-GLOVE'.")

    print("Scanning for BLE devices (8s)...\n")
    asyncio.run(run())


def main() -> None:
    if "--scan" in sys.argv:
        scan()
        return
    if "--ble" in sys.argv:
        from ble_receiver import BleGloveSource

        glove = BleGloveSource()
        print("Connecting to ESP32 glove over BLE...")
        if "--web" in sys.argv:
            # Glove drives the hand; the browser supplies the buttons the
            # hardware doesn't have yet (instruments, record, modes).
            from sensors import MergedGloveSource
            from web_source import WebGloveSource

            source = MergedGloveSource(glove, WebGloveSource())
            webbrowser.open((pathlib.Path(__file__).parent / "ui.html").as_uri())
            print("Browser panel open: pick instruments and modes while you play.")
        else:
            source = glove
            print("Tip: add --web for instrument switching and voice controls.")
    elif "--web" in sys.argv:
        from web_source import WebGloveSource

        source = WebGloveSource()
        page = pathlib.Path(__file__).parent / "ui.html"
        webbrowser.open(page.as_uri())
        print("Web frontend mode — control the glove from the browser tab.")
    elif "--demo" in sys.argv:
        source = DemoGloveSource()
        print("Demo mode: sit back and listen.")
    else:
        source = SimulatedGloveSource()
        print(
            "Keyboard glove:\n"
            "  a/d roll (brightness + voice scrub)      w/s tilt (musical pitch)\n"
            "  q/e yaw (pan)                            space wrist flick (drum)\n"
            "  [/] index bend (volume)                  ;/' middle bend (vibrato)\n"
            "  n next scene                             m mute drone\n"
            "  v record/stop mic (becomes voice loop)   o overdub a layer on top\n"
            "  p voice loop on/off                      g granular (roll = scrub)\n"
            "  b slice mode (punch fires chunks)        l live voice (headphones!)\n"
            "  1-7 instrument (saw/organ/strings/bell/flute/pluck/guitar)\n"
            "  r reset                                  x quit\n"
            "Postures: both fingers bent = fist (sound on), both straight =\n"
            "open hand (sound off), index straight + middle bent = next\n"
            "instrument. Hold one ~0.35s for it to register.\n"
            "Tip: python main.py --web gives you a visual frontend instead.\n"
        )

    midi = None
    if "--midi" in sys.argv:
        from midi_out import MidiOut

        midi = MidiOut(_flag_value("--midi-port"))

    synth = GloveSynth()
    mapping.apply_scene(synth)  # scene 1 sets instrument, scale and modes
    source.start()
    synth.start()
    try:
        while not getattr(source, "quit_requested", False):
            frame = source.latest
            mapping.apply(frame, synth)
            for event in source.drain_events():
                mapping.handle_event(event, synth)
                if midi is not None:
                    midi.handle_event(event)
            if midi is not None:
                # Reads the targets mapping.apply just set, so the DAW hears
                # the same gesture the local synth does.
                midi.update(synth)
            take = source.take_audio()
            if take is not None:
                synth.set_loop(*take)
            publish = getattr(source, "publish", None)
            if publish is not None:
                publish({
                    "roll": frame.roll, "pitch": frame.pitch,
                    "yaw": frame.yaw, "motion": frame.motion,
                    "instrument": synth.instrument,
                    "drone": synth.drone_on, "loop": synth.loop_on,
                    "granular": synth.granular_on, "slices": synth.slices_on,
                    "recording": synth.is_recording,
                    "live": synth.live_on,
                    "loop_secs": synth.loop_seconds,
                    "flex": frame.flex, "flex2": frame.flex2,
                    # Tells the UI the pose comes from the real glove, so it
                    # mirrors the hand instead of waiting to be dragged.
                    "hardware": "--ble" in sys.argv,
                })
            if not isinstance(source, DemoGloveSource):
                def _f(v):
                    return f"{v:4.2f}" if v is not None else "  - "

                # Raw ADC and calibration span alongside the calibrated value.
                # A flex channel reads "-" until it has seen FLEX_MIN_SPAN of
                # swing, and without the raw number there is no way to tell a
                # dead sensor from one that simply has not been bent far
                # enough yet — the two look identical.
                spans = getattr(source, "flex_spans", None)
                if spans is None:
                    spans = getattr(getattr(source, "pose", None), "flex_spans", None)

                def _raw(v, span):
                    if v is None:
                        return ""
                    return f"[raw {v:4.0f} span {span:4.0f}]"

                extra = ""
                if frame.flex_raw is not None or frame.flex2_raw is not None:
                    s1, s2 = spans if spans else (0.0, 0.0)
                    extra = (f"  {_raw(frame.flex_raw, s1)}"
                             f" {_raw(frame.flex2_raw, s2)}")

                print(
                    f"\rroll {frame.roll:+6.1f}  pitch {frame.pitch:+6.1f}  "
                    f"yaw {frame.yaw:+6.1f}  motion {frame.motion:4.2f}  "
                    f"flex1 {_f(frame.flex)} (volume)  "
                    f"flex2 {_f(frame.flex2)} (vibrato){extra}   ",
                    end="",
                    flush=True,
                )
            time.sleep(1.0 / CONTROL_RATE)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        synth.stop()
        if midi is not None:
            midi.close()
        print("\nBye.")


if __name__ == "__main__":
    main()
