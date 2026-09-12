"""MiMu-style glove instrument — simulation entry point.

Usage:
    python main.py             interactive keyboard "glove"
    python main.py --web       browser frontend: XY pad, instruments, buttons
    python main.py --demo      scripted gesture demo, no keys needed

With the hardware glove:
    python main.py --ble --web glove plays; browser picks instruments/modes
    python main.py --ble       glove only
    python main.py --ble --voice-glove   both hands: instrument + voice
    python main.py --scan      list BLE devices; check the glove is advertising

The voice glove defaults to its own microphone: hold its button to record a
phrase, then shape it with gestures. BLE cannot carry live audio, so that
take arrives as a recording; --voice-mic laptop uses the laptop mic live
instead.

Audio devices are bound when the stream opens, so connecting headphones after
startup will not move the sound. Either start the app afterwards, or pick
explicitly:
    python main.py --list-devices
    python main.py --audio-out AirPods --audio-in "MacBook Air Microphone"

Add --midi to any of the above to also stream notes and CCs to a DAW (see
midi_out.py). The built-in synth keeps playing; --midi is an extra output,
not a replacement, so you can A/B the two.

Playing live voice out loud speakers (rather than headphones) doubles your
voice: the direct air-borne copy plus the processed one, delayed ~25ms by
the pitch-shifter, beat together into an audible echo. Add --voice-dry to
skip the pitch-shifter/delay/reverb chain (gate + volume only) for the
lowest-latency passthrough when you don't need the effects.
"""

from __future__ import annotations

import pathlib
import sys
import time
import webbrowser

import mapping
import voice_mapping
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


def list_audio_devices() -> None:
    """Print every audio device PortAudio can see, with its index."""
    import sounddevice as sd

    for i, d in enumerate(sd.query_devices()):
        io = []
        if d["max_input_channels"]:
            io.append(f"in:{d['max_input_channels']}")
        if d["max_output_channels"]:
            io.append(f"out:{d['max_output_channels']}")
        print(f"  [{i}] {d['name'][:44]:44s} {','.join(io):12s} "
              f"{d['default_samplerate']:.0f} Hz")
    di, do = sd.default.device
    print(f"\ndefault in = [{di}]   default out = [{do}]")
    print("\nPick with:  python main.py --audio-out AirPods --audio-in MacBook")


def _resolve_device(spec: str | None, want_output: bool):
    """Turn an index or a name fragment into a PortAudio device index."""
    if spec is None:
        return None
    import sounddevice as sd

    if spec.isdigit():
        return int(spec)
    key = "max_output_channels" if want_output else "max_input_channels"
    devices = sd.query_devices()
    matches = [i for i, d in enumerate(devices)
               if spec.lower() in d["name"].lower() and d[key] > 0]
    role = "output" if want_output else "input"
    if not matches:
        raise SystemExit(
            f"No audio {role} matching {spec!r}. "
            "Run: python main.py --list-devices"
        )
    if len(matches) > 1:
        # Two pairs of AirPods match "AirPods". Silently taking the first
        # would send the sound to whichever happened to enumerate first,
        # which is exactly the confusion this flag exists to remove.
        listing = "\n".join(
            f"    [{i}] {devices[i]['name']}" for i in matches
        )
        raise SystemExit(
            f"{spec!r} matches {len(matches)} audio {role}s:\n{listing}\n"
            "  Be more specific, or pass the index."
        )
    return matches[0]


def scan() -> None:
    """List nearby BLE devices. The glove never appears in macOS Bluetooth
    settings — BLE peripherals don't pair with the OS — so this is how you
    check that it is powered and advertising."""
    import asyncio
    import threading

    from bleak import BleakScanner

    async def run() -> None:
        found = await BleakScanner.discover(timeout=8, return_adv=True)
        gloves = {}
        for addr, (dev, adv) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
            name = adv.local_name or dev.name or "(unnamed)"
            if name.startswith("MIMU-GLOVE"):
                gloves[name] = (addr, adv.rssi)
            print(f"  {name:<22} {addr}  rssi {adv.rssi}")
        print()
        if not gloves:
            print("GLOVE NOT FOUND — is the ESP32 powered? It advertises as "
                  "'MIMU-GLOVE', 'MIMU-GLOVE-I' or 'MIMU-GLOVE-V'.")
            return
        for name, (addr, rssi) in sorted(gloves.items()):
            hand = {"MIMU-GLOVE-I": "instrument hand",
                    "MIMU-GLOVE-V": "voice hand"}.get(name, "single-hand firmware")
            print(f"FOUND {name} at {addr} ({rssi} dBm) — {hand}")
        if "MIMU-GLOVE-V" in gloves:
            print("\nBoth hands: python main.py --ble --voice-glove --web")
        else:
            print("\nRun: python main.py --ble")

    print("Scanning for BLE devices (8s)...\n")
    # Importing sounddevice earlier (for --list-devices/the synth) leaves the
    # main thread's COM apartment as MAIN_STA — a PortAudio/WASAPI side
    # effect that persists for the process and that bleak's WinRT backend
    # then refuses to use ("Thread is configured for Windows GUI but
    # callbacks are not working"). COM apartments are per-thread, so running
    # the scan on a fresh thread sidesteps it instead of fighting it.
    thread = threading.Thread(target=lambda: asyncio.run(run()))
    thread.start()
    thread.join()


def main() -> None:
    if "--scan" in sys.argv:
        scan()
        return
    if "--list-devices" in sys.argv:
        list_audio_devices()
        return
    voice_source = None
    if "--ble" in sys.argv:
        from ble_receiver import BleGloveSource, run_gloves

        two = "--voice-glove" in sys.argv
        if two:
            glove = BleGloveSource(
                "MIMU-GLOVE-I", label="instrument glove", managed=True
            )
            voice_source = BleGloveSource(
                "MIMU-GLOVE-V", tag="V:", label="voice glove", managed=True
            )
            print("Connecting to BOTH gloves over BLE...")
        else:
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

    synth = GloveSynth(
        input_device=_resolve_device(_flag_value("--audio-in"), want_output=False),
        output_device=_resolve_device(_flag_value("--audio-out"), want_output=True),
    )
    print(f"[audio  {synth.describe_devices()}]")
    if "--voice-dry" in sys.argv:
        # Skips the pitch-shifter/delay/reverb chain entirely (just gate +
        # volume survive), avoiding the shifter's ~25ms tap delay. That delay
        # is what turns into an audible echo when live voice plays out loud
        # speakers instead of headphones, since the ear then gets the direct
        # air-borne voice and the delayed processed copy at once.
        synth.voice_fx_on = False
        print("[voice FX bypassed — dry passthrough for lowest latency]")
    hand = mapping.HandState()
    voice = voice_mapping.VoiceState()
    mapping.apply_scene(synth, hand)  # scene 1 sets instrument, scale and modes
    two_hands = voice_source is not None or hasattr(source, "_target_voice")
    if two_hands:
        # Tells mapping.py to stop writing the legacy live-voice targets: the
        # voice hand owns them now, and two writers at 100 Hz would fight.
        synth.voice_hand = True
        voice_mapping.apply_preset(synth, voice)
        if voice_source is not None:
            # Default to the glove's own microphone: hold its button to
            # record a phrase, and the voice hand shapes the result. Pass
            # --voice-mic laptop for the live (lower latency) path instead.
            if _flag_value("--voice-mic") == "laptop":
                synth.live_on = True
                print("Voice glove shapes the LAPTOP mic (live) "
                      "— wear headphones.")
            else:
                synth.voice_from_loop = True
                print("Voice glove shapes takes from ITS OWN mic — hold the "
                      "glove's button to record a phrase, then play it with "
                      "gestures. ('p' on the voice hand switches to the "
                      "laptop mic.)")
        else:
            print("Second hand available: press 'h' to switch the keys/UI "
                  "between the instrument and voice hands. 'l' starts the mic.")

    if voice_source is not None:
        # One thread, one event loop for both gloves. Starting each source
        # separately would give bleak two concurrent loops and two scanners,
        # which is unreliable on CoreBluetooth. Both are `managed`, so their
        # own start() is a no-op and this owns them.
        from ble_receiver import run_gloves

        run_gloves([glove, voice_source])
    source.start()
    synth.start()
    try:
        while not getattr(source, "quit_requested", False):
            frame = source.latest
            mapping.apply(frame, synth, hand)
            # The voice hand comes either from a second glove or, with no
            # second board yet, from the same keyboard/browser source driving
            # its own set of targets.
            vframe = (voice_source.latest if voice_source is not None
                      else getattr(source, "latest_voice", None))
            if vframe is not None:
                voice_mapping.apply(vframe, synth, voice)
            events = list(source.drain_events())
            if voice_source is not None:
                events += voice_source.drain_events()
            for event in events:
                # "V:" marks the voice hand; anything else is the instrument.
                if event.startswith("V:"):
                    voice_mapping.handle_event(event[2:], synth, voice)
                    continue
                mapping.handle_event(event, synth, hand)
                if midi is not None:
                    midi.handle_event(event)
            if midi is not None:
                # Reads the targets mapping.apply just set, so the DAW hears
                # the same gesture the local synth does.
                midi.update(synth)
            take = source.take_audio()
            if take is None and voice_source is not None:
                take = voice_source.take_audio()
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

                if two_hands and vframe is not None:
                    # Both hands, compactly. Showing only hand 1 made a voice
                    # hand that was moving perfectly look completely frozen.
                    line = (
                        f"\rI roll {frame.roll:+6.1f} tilt {frame.pitch:+6.1f} "
                        f"yaw {frame.yaw:+6.1f} flex {_f(frame.flex)}/{_f(frame.flex2)}"
                        f"   V roll {vframe.roll:+6.1f} tilt {vframe.pitch:+6.1f} "
                        f"yaw {vframe.yaw:+6.1f} flex {_f(vframe.flex)}/{_f(vframe.flex2)}"
                        f"{extra}   "
                    )
                else:
                    line = (
                        f"\rroll {frame.roll:+6.1f}  pitch {frame.pitch:+6.1f}  "
                        f"yaw {frame.yaw:+6.1f}  motion {frame.motion:4.2f}  "
                        f"flex1 {_f(frame.flex)} (volume)  "
                        f"flex2 {_f(frame.flex2)} (vibrato){extra}   "
                    )
                print(line, end="", flush=True)
            time.sleep(1.0 / CONTROL_RATE)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        if voice_source is not None:
            voice_source.stop()
        synth.stop()
        if midi is not None:
            midi.close()
        print("\nBye.")


if __name__ == "__main__":
    main()
