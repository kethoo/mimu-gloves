"""MiMu-style glove instrument — simulation entry point.

Usage:
    python main.py             interactive keyboard "glove"
    python main.py --web       browser frontend: XY pad, instruments, buttons
    python main.py --demo      scripted gesture demo, no keys needed

With the hardware glove:
    python main.py --ble --web glove plays; browser picks instruments/modes
    python main.py --ble       glove only
    python main.py --ble --voice-glove   both hands: live + loop
    --live-name / --loop-name            point a hand at a specific glove,
                                         e.g. a board on older firmware
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
"""

from __future__ import annotations

import math
import pathlib
import sys
import time
import webbrowser

import mapping
import loop_mapping
from sensors import DemoGloveSource, SimulatedGloveSource
from synth import GloveSynth

CONTROL_RATE = 100  # Hz


def _request_song(src: str, synth, state: dict) -> None:
    """Fetch and decode a song off the control loop.

    A download is seconds of network, and this loop also reads the gloves
    and feeds the UI at 100 Hz — doing it inline freezes the hands and the
    page until it finishes. The thread only decodes; the finished buffer is
    handed back through `state` and installed by the caller, because
    set_loop swaps the array the audio callback is reading.
    """
    import threading

    if state.get("busy"):
        state["status"] = "already loading something — wait for it"
        return
    state["busy"] = True
    state["status"] = f"loading {src.split('/')[-1][:40]}..."

    def work() -> None:
        import song as song_loader

        try:
            # One box for both, because the file says which it is. A .mid
            # holds notes and plays through the instruments; anything else
            # is a waveform and goes in the loop buffer.
            if src.split("#")[0].lower().endswith((".mid", ".midi")):
                import melody as melody_mod

                mel = melody_mod.load(src.split("#")[0])
                state["melody"] = mel
                lo, hi = mel.span
                state["status"] = (f"{mel.name} — {len(mel)} notes, "
                                   f"{mel.length:.0f}s")
            else:
                samples, rate, name = song_loader.load(src)
                state["loaded"] = (samples, rate, name)
                state["status"] = f"{name} — {len(samples) / rate:.0f}s"
        except Exception as exc:
            # Shown in the page rather than only the terminal: the player
            # is looking at the browser, and a typo in a path is the most
            # likely thing to go wrong here.
            state["status"] = f"failed: {exc}"
            print(f"[song] could not load {src}: {exc}")
        finally:
            state["busy"] = False

    threading.Thread(target=work, daemon=True).start()


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


# Combination gestures. These live here because they are the only things
# that need BOTH hands at once — neither mapping module can see the other's
# frame, and neither should.
WIDTH_RANGE = 60.0     # degrees of roll difference for the full sweep,
                       # measured from level in either direction
_combo_state = {"both_fist": False, "both_open": False}


def combo(live, loop, synth) -> None:
    """What the two hands do together, as opposed to each on its own."""
    # Relative roll -> stereo width. The one thing two IMUs give that one
    # cannot. Signed, and centred on normal: hands level is ordinary stereo,
    # twisting one way widens and the other collapses toward mono.
    #
    # It used to be the unsigned difference scaled from zero, which meant a
    # resting pose — both hands level, the most common position there is —
    # played the whole instrument in mono.
    d = max(-1.0, min(1.0, (live.roll - loop.roll) / WIDTH_RANGE))
    synth.target_width = 1.0 + d * 0.85

    # Both hands in the same posture at once: deliberate, unmistakable, and
    # impossible to hit by accident while playing. Edge-triggered so holding
    # them does not re-fire.
    from mapping import _read_posture

    a = _read_posture(live.flex, live.flex2)
    b = _read_posture(loop.flex, loop.flex2)

    both_fist = a == "fist" and b == "fist"
    if both_fist and not _combo_state["both_fist"]:
        synth.set_freeze(True)
        print("\n[BOTH FISTS — everything held]")
    _combo_state["both_fist"] = both_fist

    both_open = a == "open" and b == "open"
    if both_open and not _combo_state["both_open"]:
        synth.set_gate(False)
        synth.set_freeze(False)
        print("\n[BOTH HANDS OPEN — silence]")
    _combo_state["both_open"] = both_open


NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _note_name(hz: float) -> str:
    """Frequency back to a note name for the panel. mapping.py built it from
    a MIDI number, so this round-trips exactly for every note in a scale."""
    if hz <= 0:
        return "—"
    n = int(round(69 + 12 * math.log2(hz / 440.0)))
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def _pending(st, now):
    """Which posture is being held, and how far toward firing. Glover's panel
    just lights a posture once it commits; showing the dwell filling means you
    can see a gesture arming and correct before it triggers."""
    if not st.pending:
        return None, 0.0
    hold = mapping.POSTURE_HOLD.get(st.pending, 0.35)
    return st.pending, min((now - st.pending_since) / hold, 1.0)


def _hand_state(frame, src, posture, extra, st=None) -> dict:
    """One glove's row in the panel. `src` is the BLE source when there is
    one, so the panel can tell 'no link' from 'linked but sensor stalled' —
    a distinction that cost a whole afternoon to make by hand."""
    # Three states, not two. With no BLE source this hand is being driven by
    # the keyboard or browser — that is not a glove link, and calling it one
    # made the panel claim "linked" with nothing plugged in at all.
    if src is None:
        link = "simulated"
    elif getattr(src, "connected", False):
        link = "glove"
    else:
        link = "searching"
    linked = link != "searching"
    pend, pend_frac = _pending(st, frame.t) if st else (None, 0.0)
    state = {
        "linked": linked,
        "link": link,
        "pending": pend,
        "pending_frac": pend_frac,
        "stalled": bool(getattr(src, "stalled", False)) if src else False,
        "roll": frame.roll, "pitch": frame.pitch, "yaw": frame.yaw,
        "motion": frame.motion,
        "flex": frame.flex, "flex2": frame.flex2,
        "posture": posture,
    }
    state.update(extra)
    return state


def scan() -> None:
    """List nearby BLE devices. The glove never appears in macOS Bluetooth
    settings — BLE peripherals don't pair with the OS — so this is how you
    check that it is powered and advertising."""
    import asyncio

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
    asyncio.run(run())


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
        # Names are overridable so a board on older, un-suffixed firmware can
        # still take a role without being reflashed — matching is by prefix,
        # so "MIMU-GLOVE" finds it. Applies with one glove too: --live-name
        # used to be accepted and silently ignored outside two-hand mode.
        live_name = _flag_value("--live-name") or (
            "MIMU-GLOVE-I" if two else BleGloveSource.__init__.__defaults__[0])
        loop_name = _flag_value("--loop-name") or "MIMU-GLOVE-V"
        if two:
            glove = BleGloveSource(
                live_name, label="live glove", managed=True
            )
            voice_source = BleGloveSource(
                loop_name, tag="V:", label="loop glove", managed=True
            )
            print("Connecting to BOTH gloves over BLE...")
        else:
            glove = BleGloveSource(live_name, label="glove")
            print(f"Connecting to '{live_name}' over BLE...")
            # Without this the loop hand silently falls back to the browser
            # and the panel just says "simulated", which reads as a fault
            # rather than as a flag nobody passed.
            print("Loop hand: simulated (browser). Add --voice-glove to use "
                  "the second board.")
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
            "Keyboard glove — 'h' switches which hand the keys drive.\n"
            "\n"
            "LIVE hand (makes sound):\n"
            "  w/s tilt = musical pitch          a/d roll = brightness\n"
            "  q/e yaw = pan                     [/] index bend = volume\n"
            "  ;/' middle bend = vibrato         space = drum hit\n"
            "  n next scene                      1-7 instrument\n"
            "  v record a phrase into the loop   m mute\n"
            "\n"
            "LOOP hand (shapes what was captured):\n"
            "  a/d roll = scrub the playhead     w/s tilt = loop speed\n"
            "  q/e yaw = its stereo position     [/] index bend = live/loop blend\n"
            "  ;/' middle bend = reverb          space = slice / stutter\n"
            "  n next preset                     o overdub a layer\n"
            "  g granular  b slices  p loop on/off  l live mic (headphones!)\n"
            "\n"
            "Postures (hold ~0.35 s; open hand needs 1 s):\n"
            "  both fingers bent  = fist   live: sound on   loop: FREEZE\n"
            "  both straight      = open   live: sound off  loop: release\n"
            "  index straight,\n"
            "  middle bent        = point  live: next instrument\n"
            "                              loop: cycle normal/granular/slices\n"
            "  r reset                           x quit\n"
            "Tip: python main.py --web gives you a visual frontend instead.\n"
        )

    glove_src = glove if "--ble" in sys.argv else None

    midi = None
    if "--midi" in sys.argv:
        from midi_out import MidiOut

        midi = MidiOut(_flag_value("--midi-port"))

    # The scripted demo plays itself, so it has to start sounding.
    start_sounding = "--demo" in sys.argv

    synth = GloveSynth(
        input_device=_resolve_device(_flag_value("--audio-in"), want_output=False),
        output_device=_resolve_device(_flag_value("--audio-out"), want_output=True),
    )
    print(f"[audio  {synth.describe_devices()}]")
    synth.drone_on = start_sounding
    if not start_sounding:
        keys_here = isinstance(source, SimulatedGloveSource)
        how = "press 'm' here" if keys_here else (
            "click 'drone' in the browser, or press m with the page focused")
        print(f"[silent until you play — flick your wrist, make a fist, "
              f"bend the index finger, or {how}]")
    song_name = None
    seq = None
    mel_src = _flag_value("--melody")
    if mel_src:
        import melody as melody_mod

        try:
            seq = melody_mod.Sequencer(melody_mod.load(mel_src))
        except Exception as exc:
            print(f"[melody] could not load {mel_src}: {exc}")
    # Shared with the loader thread: status text for the page, the finished
    # buffer, and a busy flag so a second click cannot start a second fetch.
    song_state: dict = {"status": "", "busy": False}
    song_src = _flag_value("--song")
    if song_src:
        import song as song_loader

        try:
            samples, rate, song_name = song_loader.load(
                song_src,
                start=float(_flag_value("--song-start") or 0.0),
                seconds=float(_flag_value("--song-seconds") or 0.0) or None,
            )
            synth.set_loop(samples, rate, label=song_name)
            song_state["status"] = f"{song_name} — {len(samples) / rate:.0f}s"
        except Exception as exc:
            # A bad path or a dead link should not cost you the instrument:
            # the gloves still play, just without a record on the deck.
            print(f"[song] could not load {song_src}: {exc}")
            song_name = None

    hand = mapping.HandState()
    voice = loop_mapping.LoopState()
    mapping.apply_scene(synth, hand)  # scene 1 sets instrument, scale and modes
    two_hands = voice_source is not None or hasattr(source, "_target_voice")
    if two_hands:
        # Tells mapping.py to stop writing the legacy live-voice targets: the
        # voice hand owns them now, and two writers at 100 Hz would fight.
        synth.voice_hand = True
        loop_mapping.apply_preset(synth, voice)
        if voice_source is not None:
            # Default to the glove's own microphone: hold its button to
            # record a phrase, and the voice hand shapes the result. Pass
            # --voice-mic laptop for the live (lower latency) path instead.
            if _flag_value("--voice-mic") == "laptop":
                synth.live_on = True
                # Also route the chain AT the microphone. Without this the
                # loop still takes priority whenever one is playing, so the
                # flag silently did nothing once you had recorded anything.
                synth.voice_from_loop = False
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
                  "between the LIVE and LOOP hands. 'l' starts the mic.")

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
            if seq is not None:
                # Before the mapping, so the hand's transpose applies to the
                # note this tick plays rather than the one before it.
                seq.update(synth, 1.0 / CONTROL_RATE)
            mapping.apply(frame, synth, hand, seq=seq)
            # The voice hand comes either from a second glove or, with no
            # second board yet, from the same keyboard/browser source driving
            # its own set of targets.
            # The physical loop glove if it is actually connected, otherwise
            # the browser/keyboard second hand. That means one real glove plus
            # the UI for the other works without any extra flag — useful when
            # you only want to test one hand, or only have one glove built.
            if voice_source is not None and getattr(voice_source, "connected", False):
                vframe = voice_source.latest
            else:
                vframe = getattr(source, "latest_voice", None)
            if vframe is not None:
                loop_mapping.apply(vframe, synth, voice)
                if seq is not None:
                    # The loop hand's speed gesture has nothing to stretch
                    # when a tune is playing, so it pulls the tempo instead.
                    seq.rate = synth.target_rate
                combo(frame, vframe, synth)
            events = list(source.drain_events())
            if voice_source is not None:
                events += voice_source.drain_events()
            for event in events:
                if event.endswith("song_clear"):
                    synth.clear_loop()
                    seq = None
                    song_state["status"] = ""
                    song_name = None
                    continue
                if event.endswith("recalibrate"):
                    # Only a real glove has flex calibration to reset; from
                    # the keyboard or the browser the bend is already a
                    # 0..1 value and there is nothing to relearn.
                    tgt = voice_source if event.startswith("V:") else glove_src
                    reset = getattr(tgt, "recalibrate_flex", None)
                    if reset is not None:
                        reset()
                        print("\n[flex calibration reset - bend each finger "
                              "fully once to relearn its range]")
                    continue
                # "V:" marks the voice hand; anything else is the instrument.
                if event.startswith("V:"):
                    loop_mapping.handle_event(event[2:], synth, voice)
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
                song_state["status"] = ""   # a glove take replaces the song
                seq = None
            want = source.take_song()
            if want is not None:
                _request_song(want, synth, song_state)
            new_mel = song_state.pop("melody", None)
            if new_mel is not None:
                import melody as melody_mod

                seq = melody_mod.Sequencer(new_mel)
                synth.clear_loop()   # a tune and a record at once is mud
            loaded = song_state.pop("loaded", None)
            if loaded is not None:
                # Installed here, on the thread that owns the synth, rather
                # than from the loader thread: set_loop swaps the buffer the
                # audio callback is reading.
                synth.set_loop(loaded[0], loaded[1], label=loaded[2])
                song_name = loaded[2]
            publish = getattr(source, "publish", None)
            if publish is not None:
                live_p = hand.posture
                loop_p = voice.posture
                publish({
                    "roll": frame.roll, "pitch": frame.pitch,
                    "yaw": frame.yaw, "motion": frame.motion,
                    "instrument": synth.instrument,
                    "drone": synth.drone_on, "loop": synth.loop_on,
                    "granular": synth.granular_on, "slices": synth.slices_on,
                    "recording": synth.is_recording,
                    "live": synth.live_on,
                    "loop_secs": synth.loop_seconds,
                    "loop_label": synth.loop_label,
                    "song_status": song_state["status"],
                    "song_busy": song_state["busy"],
                    "melody": None if seq is None else {
                        "name": seq.melody.name,
                        "note": seq.current,
                        "progress": seq.progress,
                        "rate": seq.rate,
                        "notes": len(seq.melody),
                    },
                    "flex": frame.flex, "flex2": frame.flex2,
                    # Tells the UI the pose comes from the real glove, so it
                    # mirrors the hand instead of waiting to be dragged.
                    "hardware": "--ble" in sys.argv,
                    # ---- the two-hand panel -------------------------------
                    "two_hands": two_hands,
                    "live_hand": _hand_state(frame, glove_src, live_p, st=hand, extra={
                        "note": _note_name(synth.target_freq),
                        # Where the note sits in the scene's scale, so the
                        # bar tracks the hand rather than sitting at half.
                        "pitch_frac": hand.last_step / max(
                            len(mapping.SCALES[mapping.SCENES[hand.scene][2]]) - 1, 1),
                        "bright": min(max(
                            (math.log(max(synth.target_cutoff, 200.0) / 200.0)
                             / math.log(30.0)), 0.0), 1.0),
                        "cutoff": synth.target_cutoff,
                        "volume": min(synth.target_amp / 0.55, 1.0),
                        "vibrato": synth.target_vibrato,
                        "scene": mapping.SCENES[hand.scene][0],
                        "scene_n": hand.scene + 1,
                        "scene_total": len(mapping.SCENES),
                        "gate": synth.drone_on,
                    }),
                    "loop_hand": _hand_state(vframe or frame, voice_source, loop_p, st=voice, extra={
                        "scrub": synth.target_scrub,
                        "speed": synth.target_rate,
                        "reverb": synth.target_voice_reverb,
                        "blend": min(synth.target_voice_volume / 0.9, 1.0),
                        "frozen": synth.freeze_on,
                        "mode": ("granular" if synth.granular_on
                                 else "slices" if synth.slices_on else "normal"),
                        "preset": loop_mapping.PRESETS[voice.preset][0],
                        "preset_n": voice.preset + 1,
                        "preset_total": len(loop_mapping.PRESETS),
                        "delay": synth.voice_delay_on,
                        "loop_secs": synth.loop_seconds,
                        "loop_label": synth.loop_label,
                        "has_song": song_name is not None,
                        "song_status": song_state["status"],
                        "song_busy": song_state["busy"],
                    }),
                    "width": synth.target_width,
                    "combo": ("both fists" if (live_p == "fist" and loop_p == "fist")
                              else "both open" if (live_p == "open" and loop_p == "open")
                              else None),
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
