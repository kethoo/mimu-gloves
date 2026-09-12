"""Real-time synth engine.

Runs a PortAudio callback at audio rate (44.1 kHz). The rest of the program
only sets high-level targets (frequency, brightness, pan, volume, pluck);
this module smooths them per-sample so parameter changes never click.
"""

from __future__ import annotations

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100
BLOCK = 256  # ~6 ms of audio per callback

GRAIN_LEN = int(0.09 * SAMPLE_RATE)   # granular mode: 90 ms grains...
GRAIN_HOP = int(0.045 * SAMPLE_RATE)  # ...spawned every 45 ms (2x overlap)
N_SLICES = 4                          # slice mode: recording split into 4 pads
VIBRATO_HZ = 5.5                      # wobble speed, roughly a singer's
VIBRATO_DEPTH = 0.02                  # max pitch swing (+/-2%, ~a third of
                                      # a semitone). Raise for a seasick
                                      # effect, lower for a subtle one.

PS_BUF = 1 << 16                     # live voice: ring buffer (~1.5 s)
PS_WIN = 2048                        # pitch-shifter tap window (~46 ms)

# ---- voice hand (second glove) ----------------------------------------
# Schroeder reverb: four parallel combs summed into two series allpasses.
# Every delay here is longer than BLOCK on purpose — that is what lets the
# whole reverb stay vectorized. A delay shorter than one block would need the
# previous output *within* the same block and force a per-sample loop, which
# is why the two short Freeverb allpasses (341, 225) are not used.
REVERB_COMBS = (1116, 1188, 1277, 1356)
REVERB_APS = (556, 441)
REVERB_FB = 0.90          # comb feedback. Measured T60 ~1.45 s, a hall
                          # rather than a room; roll dials the wet amount.
REVERB_AP_G = 0.5         # allpass coefficient: diffusion

VOICE_DELAY = int(0.28 * SAMPLE_RATE)   # "point" delay time. Its feedback is
                                        # target_echo, i.e. hand motion — wave
                                        # and the repeats ring on longer.
STUTTER_LEN = int(0.15 * SAMPLE_RATE)   # wrist flick grabs this much
STUTTER_HOLD = int(0.70 * SAMPLE_RATE)  # ...and repeats it for this long


def _ring_read(buf: np.ndarray, i: int, frames: int) -> np.ndarray:
    """Read `frames` samples from a circular buffer, handling one wrap."""
    n = len(buf)
    if i + frames <= n:
        return buf[i:i + frames]
    k = n - i
    return np.concatenate((buf[i:], buf[:frames - k]))


def _ring_write(buf: np.ndarray, i: int, data: np.ndarray) -> None:
    n = len(buf)
    frames = len(data)
    if i + frames <= n:
        buf[i:i + frames] = data
    else:
        k = n - i
        buf[i:] = data[:k]
        buf[:frames - k] = data[k:]

FILTER_RES = 1.1     # ladder feedback; higher = more resonant peak at cutoff

KS_MAX = 2048        # plucked string: longest delay line (lowest note)
KS_GAIN = 2.4        # the string sits quieter than the oscillators; match it

# Karplus-Strong voices: (T60 seconds, loop-filter weight).
# The weight is how much of the *current* sample the loop keeps versus the
# previous one. The classic 0.5 is a plain two-point average, which dulls the
# string fast — right for an acoustic pluck. An electric string is driven by a
# magnetic pickup rather than radiating into a soundboard, so it keeps its
# highs and rings far longer: a higher weight, and a much longer T60.
KS_VOICES = {
    "pluck":  (2.0, 0.50),
    "guitar": (5.0, 0.86),
}

GUITAR_DRIVE = 11.0     # preamp gain into the clipper
GUITAR_PICKUP = 0.21    # tap position along the string (bridge-ish, nasal)
GUITAR_CAB_LO = 90.0    # speaker rolls off below this...
GUITAR_CAB_HI = 4200.0  # ...and hard above this. Without it: fizz.

INSTRUMENTS = {
    "saw": "Saw Lead",
    "organ": "Organ",
    "strings": "String Pad",
    "bell": "FM Bell",
    "flute": "Flute",
    "pluck": "Plucked String",
    "guitar": "Electric Guitar",
}

# Per-instrument amplitude envelope: (attack s, decay s, sustain 0..1, struck).
#
# This table matters more than the harmonic recipes below. Without it every
# instrument is a constant-amplitude drone, and a bell that never rings out
# is not a bell — the ear identifies an instrument mostly by its attack
# transient and decay shape, not by its steady-state spectrum.
#
# "struck" means the note is re-articulated rather than glided into: the
# pitch snaps instead of sliding, and a punch re-strikes it.
ENVELOPES = {
    #            attack  decay  sustain  struck
    "saw":     (  0.010,  0.30,    0.72,  False),
    "organ":   (  0.004,  0.02,    1.00,  False),  # organs are on/off
    "strings": (  0.350,  0.50,    0.85,  False),  # slow bow
    "bell":    (  0.002,  2.20,    0.00,   True),  # struck, rings out
    "flute":   (  0.100,  0.20,    0.88,  False),  # breath takes time
    "pluck":   (  0.001,  0.01,    1.00,   True),  # string decays on its own
    "guitar":  (  0.001,  0.01,    1.00,   True),  # ditto, plus the amp
}


def _poly_saw(p: np.ndarray, dt: float) -> np.ndarray:
    """Band-limited sawtooth from a phase ramp (PolyBLEP).

    A raw `2*p - 1` ramp has unlimited harmonics, and every one above Nyquist
    folds back down as inharmonic grit — measured at 55% of total energy for
    the four-saw string pad at the top of the scale, which is most of what
    made it sound harsh. Smoothing the wrap discontinuity with a two-sample
    polynomial removes the bulk of it for a couple of array ops.
    """
    s = 2.0 * p - 1.0
    if dt <= 0.0:
        return s
    lo = p < dt
    if lo.any():
        x = p[lo] / dt
        s[lo] -= x + x - x * x - 1.0
    hi = p > 1.0 - dt
    if hi.any():
        x = (p[hi] - 1.0) / dt
        s[hi] -= x * x + x + x + 1.0
    return s


class GloveSynth:
    def __init__(self, input_device=None, output_device=None) -> None:
        # PortAudio binds a stream to whatever device was default when the
        # stream was *created*. Switching the system default afterwards (say,
        # connecting AirPods mid-session) does not move an already-open
        # stream, which is why audio can keep coming out of the laptop.
        # Passing devices explicitly also lets output and input be different
        # hardware — AirPods out, laptop mic in — which matters because using
        # AirPods as an input forces them into a 24 kHz hands-free mode that
        # noticeably degrades what you hear.
        self._devices = (input_device, output_device)
        # Targets, written by the control thread at ~100 Hz.
        self.target_freq = 220.0     # Hz
        self.target_cutoff = 1000.0  # Hz, lowpass brightness
        self.target_pan = 0.0        # -1 left .. +1 right
        self.target_amp = 0.25       # 0..1
        self.target_rate = 1.0       # voice-loop playback speed (0.5..2)
        self.target_scrub = 0.5      # granular position / slice selector, 0..1
        self.target_vibrato = 0.0    # pitch wobble depth, 0..1 (second finger)

        # Smoothed values owned by the audio callback.
        self._freq = self.target_freq
        self._cutoff = self.target_cutoff
        self._pan = self.target_pan
        self._amp = 0.0
        self._rate = 1.0

        self.instrument = "saw"
        self._ph = [0.0, 0.0, 0.0, 0.0]  # phase accumulators (osc + partials)
        self._lfo = 0.0           # vibrato LFO phase (flute)
        self._vib = 0.0           # vibrato LFO phase (finger-controlled)
        self._vibDepth = 0.0      # smoothed depth
        self._lp = [0.0, 0.0, 0.0, 0.0]  # four-pole ladder filter state
        self._pluck_env = 0.0     # decaying envelope for percussive hits
        self._pluck_lp = 0.0
        self._rng = np.random.default_rng()

        # Note articulation. mapping.py quantizes roll to a scale, so
        # target_freq moves in discrete steps and each step is a note-on.
        self._note_freq = 0.0     # 0 => the first block articulates a note
        self._env = 0.0           # amplitude envelope level
        self._env_stage = 0       # 0 = attack, 1 = decay/sustain
        self._chiff = 0.0         # flute breath-onset burst

        # Karplus-Strong string: a delay line one wavelength long, fed back
        # through a two-point average. The averaging is a lowpass, so every
        # pass round the loop dulls the tone a little — which is what a real
        # string does as it rings.
        self._ks_buf = np.zeros(KS_MAX)
        self._ks_n = 200          # current delay length, in samples
        self._ks_i = 0            # read/write position
        self._ks_prev = 0.0       # previous sample, for the averaging filter
        self._ks_decay = 1.0      # per-pass loop gain
        self._ks_b = 0.5          # loop-filter weight (see KS_VOICES)
        self._cab = [0.0, 0.0, 0.0]  # guitar speaker filter state

        # Voice loop: recorded from the mic, replayed through the same
        # gesture-controlled filter/pan chain as the oscillator.
        self.drone_on = True
        self.loop_on = False
        self._loop_buf: np.ndarray | None = None
        self._loop_pos = 0.0
        self._rec_stream: sd.InputStream | None = None
        self._rec_chunks: list[np.ndarray] = []
        self._overdub = False
        self._overdub_from = 0.0  # loop position when the overdub started

        # Granular mode: the loop becomes a cloud of short windowed grains
        # drawn from wherever the hand is "pointing" in the recording.
        self.granular_on = False
        self._scrub = 0.5
        self._grains: list[list[int]] = []  # [start_sample, age] per grain
        self._grain_timer = 0
        self._grain_win = np.hanning(GRAIN_LEN)

        # Slice mode: punches fire one-shot chunks of the recording.
        self.slices_on = False
        self._shot: list[int] | None = None  # [slice_start, play_pos, end]

        # Live voice mode: mic streams straight through a granular pitch
        # shifter + echo, warped by gestures as you speak.
        self.live_on = False
        self.target_echo = 0.2       # echo feedback, driven by hand motion
        self._live_in: np.ndarray | None = None
        self._ps_buf = np.zeros(PS_BUF)
        self._ps_w = 0               # ring-buffer write position
        self._ps_phase = 0.0         # tap-window phase, 0..1
        self._pshift = 1.0           # smoothed pitch ratio
        self._gate = 0.0             # noise-gate envelope follower
        self._echo_fb = 0.2   # smoothed delay feedback, driven by motion

        # ---- voice hand (second glove) --------------------------------
        # Disjoint from the instrument targets on purpose: two gloves control
        # two parameter sets, so the voice never passes through the
        # instrument's ladder filter or pan.
        self.voice_hand = False       # True once a voice glove is driving us
        # Where the voice chain gets its audio. BLE cannot carry live audio
        # (16 kHz mono needs 32 kB/s against a link measured at 30 kB/s with
        # the sensor stream already switched off, and the latency would be
        # 100 ms+), so the glove's own mic reaches us as recorded takes. With
        # this set, those takes feed the voice effects instead of the
        # instrument bus — the source is the board's microphone, the control
        # is the voice hand, they just are not simultaneous.
        self.voice_from_loop = False
        self.voice_fx_on = True       # fist on / open hand = dry
        self.voice_delay_on = False   # "point" toggles it
        self.target_voice_pitch = 1.0     # 0.5 (octave down) .. 2.0 (up)
        self.target_voice_reverb = 0.25   # 0..1 wet
        self.target_voice_pan = 0.0       # -1..+1
        self.target_voice_volume = 0.6    # 0..1
        self._v_pitch = 1.0
        self._v_reverb = 0.25
        self._v_pan = 0.0
        self._v_vol = 0.6

        # Reverb: parallel combs -> series allpasses.
        self._rv_comb = [np.zeros(d) for d in REVERB_COMBS]
        self._rv_comb_i = [0] * len(REVERB_COMBS)
        self._rv_comb_prev = [0.0] * len(REVERB_COMBS)
        self._rv_ap = [np.zeros(d) for d in REVERB_APS]
        self._rv_ap_i = [0] * len(REVERB_APS)

        # "Point" delay, separate from the legacy live-mode echo.
        self._vd_buf = np.zeros(VOICE_DELAY + BLOCK)
        self._vd_w = 0

        # Wrist-flick stutter: a rolling history to grab from, the grabbed
        # slice itself, and how much longer to keep repeating it.
        self._st_hist = np.zeros(STUTTER_LEN)
        self._st_hw = 0
        self._st_buf = np.zeros(STUTTER_LEN)
        self._st_left = 0
        self._st_pos = 0

        # Duplex stream (mic in + speakers out in one callback) so live
        # mode works; fall back to output-only if there's no input device.
        try:
            self._stream = sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK,
                channels=(1, 2),
                dtype="float32",
                device=self._devices,
                callback=self._duplex_callback,
            )
            self._has_input = True
        except Exception as exc:
            print(f"[no mic available, live voice mode disabled: {exc}]")
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK,
                channels=2,
                dtype="float32",
                device=output_device,
                callback=self._callback,
            )
            self._has_input = False

    def describe_devices(self) -> str:
        """Which hardware the stream actually bound to. Printed at startup
        because 'why is the sound coming out of the wrong thing' is otherwise
        invisible."""
        try:
            dev = self._stream.device
            names = []
            for d, role in zip(
                dev if isinstance(dev, (list, tuple)) else [dev],
                ("in", "out") if self._has_input else ("out",),
            ):
                names.append(f"{role}: {sd.query_devices(d)['name']}")
            return "  |  ".join(names)
        except Exception:
            return "unknown"

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()

    def pluck(self) -> None:
        """Trigger a percussive hit (punch gesture). On a struck instrument
        it also re-articulates the note, so punching re-rings the bell or
        re-plucks the string instead of only firing the drum layer."""
        self._pluck_env = 1.0
        if ENVELOPES[self.instrument][3]:
            self._note_on()

    def _note_on(self) -> None:
        """Articulate a new note.

        Struck instruments restart from silence. The sustained ones re-enter
        the attack from wherever the envelope already is, so a run of notes
        is legato — resetting them to zero would gate the sound off on every
        step, and a 350 ms pad would never get to full level while the hand
        was moving.
        """
        self._env_stage = 0
        self._chiff = 1.0
        if ENVELOPES[self.instrument][3]:
            self._env = 0.0
            if self.instrument in KS_VOICES:
                self._ks_pluck()

    def set_instrument(self, name: str, announce: bool = True) -> None:
        if name in INSTRUMENTS:
            self.instrument = name
            self._note_on()  # so you hear the new instrument's attack
            if announce:
                print(f"\n[instrument: {INSTRUMENTS[name]}]")

    def next_instrument(self) -> None:
        """Step to the next instrument (the 'point' posture)."""
        names = list(INSTRUMENTS)
        self.set_instrument(names[(names.index(self.instrument) + 1) % len(names)])

    def set_gate(self, on: bool) -> None:
        """Fist activates the sound, an open hand deactivates it. Distinct
        from toggle_drone only in being absolute rather than a toggle, which
        is what a posture needs: holding a fist must always mean 'on'."""
        if on != self.drone_on:
            self.drone_on = on
            print("\n[sound ON]" if on else "\n[sound OFF — make a fist, or just play]")

    @property
    def is_recording(self) -> bool:
        return self._rec_stream is not None

    @property
    def loop_seconds(self) -> float:
        return 0.0 if self._loop_buf is None else len(self._loop_buf) / SAMPLE_RATE

    def toggle_record(self) -> None:
        """Start/stop recording the mic; the take replaces the voice loop.
        Later this is the glove button.
        """
        if self._rec_stream is None:
            self._start_recording(overdub=False)
        else:
            self._stop_recording()

    def toggle_overdub(self) -> None:
        """Like record, but the take is layered ON TOP of the playing loop,
        aligned to where the loop was when the overdub started.
        """
        if self._rec_stream is not None:
            self._stop_recording()
        elif self._loop_buf is None:
            self.toggle_record()  # nothing to layer onto yet: plain record
        else:
            self._start_recording(overdub=True)

    def _start_recording(self, overdub: bool) -> None:
        self._rec_chunks = []
        self._overdub = overdub
        self._overdub_from = self._loop_pos
        self._rec_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=lambda indata, *_: self._rec_chunks.append(indata.copy()),
        )
        self._rec_stream.start()
        print(
            "\n[overdubbing on top of the loop... press o again to stop]"
            if overdub
            else "\n[recording mic... press v again to stop]"
        )

    def _stop_recording(self) -> None:
        self._rec_stream.stop()
        self._rec_stream.close()
        self._rec_stream = None
        if not self._rec_chunks:
            print("\n[nothing recorded]")
            return
        take = np.concatenate(self._rec_chunks)[:, 0]
        peak = float(np.abs(take).max())
        if peak < 1e-3:
            print(
                "\n[take was almost silent (peak "
                f"{peak:.5f}) — discarded. Check mic permission in "
                "System Settings > Privacy & Security > Microphone]"
            )
            return
        # Normalize to 0.5 peak, but cap the boost so a faint take doesn't
        # become amplified room hiss.
        take *= min(0.5 / peak, 40.0)

        if self._overdub and self._loop_buf is not None:
            # Mix into a copy starting where the loop was at record start,
            # wrapping around, then swap the copy in.
            buf = self._loop_buf.copy()
            ofs = int(self._overdub_from) % len(buf)
            idx = (ofs + np.arange(len(take))) % len(buf)
            np.add.at(buf, idx, take)
            peak = float(np.abs(buf).max())
            if peak > 0.9:
                buf *= 0.9 / peak
            self._loop_buf = buf
            print(f"\n[layer added — loop is {len(buf) / SAMPLE_RATE:.1f}s]")
        else:
            self.loop_on = False  # keep the callback off the buffer while we swap it
            self._loop_buf = take
            self._loop_pos = 0.0
            self._grains = []
            self._shot = None
            self.loop_on = True
            print(f"\n[{len(take) / SAMPLE_RATE:.1f}s recorded — looping; move your hand to shape it]")

    def set_loop(self, samples, rate: int) -> None:
        """Install a recording made elsewhere (the glove's own mic) as the
        voice loop, resampled to the audio engine's rate. Everything
        downstream — pitch, granular, slices, overdub — then works on it
        exactly as it does on a laptop-recorded take."""
        samples = np.asarray(samples, dtype=np.float64)
        if len(samples) < 2:
            return
        if rate != SAMPLE_RATE:
            n_out = int(len(samples) * SAMPLE_RATE / rate)
            samples = np.interp(
                np.linspace(0.0, len(samples) - 1, n_out),
                np.arange(len(samples)),
                samples,
            )
        peak = float(np.abs(samples).max())
        if peak > 1e-4:
            samples = samples * min(0.5 / peak, 40.0)
        self.loop_on = False           # keep the callback off the buffer
        self._loop_buf = samples
        self._loop_pos = 0.0
        self._grains = []
        self._shot = None
        self.loop_on = True
        print(f"\n[glove take loaded — {len(samples) / SAMPLE_RATE:.1f}s looping]")

    def toggle_granular(self) -> None:
        self.granular_on = not self.granular_on
        self._grains = []
        print(
            "\n[granular mode: roll scrubs through the frozen voice]"
            if self.granular_on
            else "\n[normal loop mode]"
        )

    def toggle_slices(self) -> None:
        self.slices_on = not self.slices_on
        print(
            f"\n[slice mode: punch fires 1 of {N_SLICES} chunks, roll picks which]"
            if self.slices_on
            else "\n[slice mode off: punch is a drum hit again]"
        )

    def trigger_slice(self) -> None:
        buf = self._loop_buf
        if buf is None or len(buf) < N_SLICES:
            print("\n[no recording to slice — record one with v]")
            return
        i = min(int(self.target_scrub * N_SLICES), N_SLICES - 1)
        step = len(buf) // N_SLICES
        self._shot = [i * step, i * step, min((i + 1) * step, len(buf))]

    def toggle_loop(self) -> None:
        if self._loop_buf is None:
            print("\n[no voice loop yet — record one with v]")
            return
        self.loop_on = not self.loop_on
        print("\n[voice loop on]" if self.loop_on else "\n[voice loop off]")

    def toggle_drone(self) -> None:
        self.drone_on = not self.drone_on
        print("\n[drone on]" if self.drone_on else "\n[drone muted]")

    def toggle_live(self) -> None:
        if not self._has_input:
            print("\n[live voice unavailable: no mic]")
            return
        self.live_on = not self.live_on
        print(
            "\n[live voice ON — speak! roll = pitch, motion = echo. "
            "Use headphones or it will feed back]"
            if self.live_on
            else "\n[live voice off]"
        )

    def _duplex_callback(
        self, indata: np.ndarray, out: np.ndarray, frames: int, time_info, status
    ) -> None:
        self._live_in = indata[:, 0].astype(np.float64)
        self._callback(out, frames, time_info, status)

    def _callback(self, out: np.ndarray, frames: int, time_info, status) -> None:
        # Per-block smoothing of control parameters (~50 ms time constant).
        k = 0.15
        self._freq += (self.target_freq - self._freq) * k
        self._cutoff += (self.target_cutoff - self._cutoff) * k
        self._pan += (self.target_pan - self._pan) * k
        self._amp += (self.target_amp - self._amp) * k

        # Drone oscillator (selected instrument). Volume applied here,
        # pre-filter, so the voice loop below has its own independent level.
        if self.drone_on:
            saw = self._render_tone(frames) * self._amp
        else:
            saw = np.zeros(frames)

        # Voice: mixed in before the filter so tilt brightens/darkens it too.
        loop_block = None
        buf = self._loop_buf
        if buf is not None and len(buf) > 1:
            voice = None
            if self.loop_on:
                if self.granular_on:
                    voice = self._render_grains(buf, frames)
                else:
                    # Variable-rate loop (roll = speed & pitch), linear interp.
                    self._rate += (self.target_rate - self._rate) * k
                    idx = (self._loop_pos + self._rate * np.arange(frames)) % len(buf)
                    i0 = idx.astype(np.intp)
                    i1 = (i0 + 1) % len(buf)
                    frac = idx - i0
                    voice = buf[i0] * (1.0 - frac) + buf[i1] * frac
                    self._loop_pos = float(
                        (self._loop_pos + self._rate * frames) % len(buf)
                    )
            shot = self._render_shot(buf, frames)
            if shot is not None:
                voice = shot if voice is None else voice + shot
            if voice is not None:
                if self.voice_from_loop:
                    # Hand it to the voice chain below instead of the
                    # instrument bus, so it is not processed twice.
                    loop_block = voice
                else:
                    # Solid base level so the voice is always clearly audible;
                    # motion still adds a swell on top.
                    saw = saw + voice * (0.6 + self._amp)

        # The live voice is deliberately NOT mixed in here any more. It runs
        # its own chain below with its own level and pan, because the voice
        # glove controls a parameter set disjoint from the instrument's —
        # dragging it through the instrument's ladder filter would mean one
        # hand's brightness gesture silently reshaped the other hand's voice.

        # Four-pole resonant lowpass (-24 dB/oct), the classic ladder shape.
        # A single pole managed only -4 dB one octave above cutoff, so almost
        # every harmonic survived it and all six instruments came out fizzy.
        # The resonant peak is also what makes a tilt sweep sound like an
        # instrument opening up rather than a tone knob being turned.
        # Four poles pull the corner down but the resonant peak lifts it back,
        # so the coefficient needs no trim: measured, a nominal 1 kHz lands
        # its -3 dB point at 1009 Hz.
        fc = min(self._cutoff, 0.45 * SAMPLE_RATE)
        a = min(1.0 - np.exp(-2.0 * np.pi * fc / SAMPLE_RATE), 0.9)
        res = FILTER_RES
        y1, y2, y3, y4 = self._lp
        lp = np.empty(frames)
        for i in range(frames):
            x = saw[i] - res * y4   # feedback around the whole ladder
            y1 += a * (x - y1)
            y2 += a * (y1 - y2)
            y3 += a * (y2 - y3)
            y4 += a * (y3 - y4)
            lp[i] = y4
        self._lp = [y1, y2, y3, y4]

        # The feedback costs (1 + res) of passband gain; put it back.
        mono = lp * (1.0 + res)

        # Percussive layer: filtered noise burst with exponential decay.
        if self._pluck_env > 1e-4:
            noise = self._rng.standard_normal(frames)
            env = self._pluck_env * np.exp(
                -np.arange(frames) / (0.12 * SAMPLE_RATE)
            )
            self._pluck_env = float(env[-1])
            pn = np.empty(frames)
            y2 = self._pluck_lp
            for i in range(frames):
                y2 += 0.25 * (noise[i] - y2)
                pn[i] = y2
            self._pluck_lp = y2
            mono = mono + pn * env * 0.8

        # Soft clip so stacked layers saturate gently instead of crackling.
        mono = np.tanh(mono)

        # Equal-power stereo pan for the instrument.
        angle = (self._pan + 1.0) * (np.pi / 4.0)
        left = mono * np.cos(angle)
        right = mono * np.sin(angle)

        # Voice hand: its own chain, its own level, its own pan. Summed at the
        # very end so nothing the instrument glove does can touch it.
        live_in = self._live_in
        v_in = loop_block
        if self.live_on and live_in is not None and len(live_in) == frames:
            v_in = live_in if v_in is None else v_in + live_in
        if v_in is not None:
            v = self._process_voice(v_in, frames)
            v = np.tanh(v)   # its own soft clip; reverb tails can stack up
            v_angle = (self._v_pan + 1.0) * (np.pi / 4.0)
            left = left + v * np.cos(v_angle)
            right = right + v * np.sin(v_angle)

        out[:, 0] = left.astype(np.float32)
        out[:, 1] = right.astype(np.float32)

    # ---- voice hand ---------------------------------------------------

    def set_voice_fx(self, on: bool) -> None:
        """Fist activates the effect chain, an open hand returns the dry
        voice. Absolute rather than a toggle, because a held posture must
        always mean the same thing."""
        if on != self.voice_fx_on:
            self.voice_fx_on = on
            print("\n[voice FX ON]" if on else "\n[voice: dry]")

    def toggle_voice_source(self) -> None:
        """Switch the voice effects between the laptop mic and the glove's own
        recorded takes."""
        self.voice_from_loop = not self.voice_from_loop
        print(
            "\n[voice source: GLOVE mic (recorded takes) — hold the glove "
            "button to record]"
            if self.voice_from_loop
            else "\n[voice source: laptop mic — press l for live]"
        )

    def toggle_voice_delay(self) -> None:
        self.voice_delay_on = not self.voice_delay_on
        print("\n[voice delay on]" if self.voice_delay_on else "\n[voice delay off]")

    def trigger_stutter(self) -> None:
        """Wrist flick: freeze the last STUTTER_LEN of voice and repeat it.

        The grab is taken from a rolling history rather than from the moment
        of the flick, so the captured audio is what you *just said* — waiting
        to fill a buffer after the gesture would repeat the silence after it.
        """
        h = self._st_hist
        w = self._st_hw
        # Unwrap the history so the grab reads oldest-to-newest.
        self._st_buf = np.concatenate((h[w:], h[:w])).copy()
        self._st_left = STUTTER_HOLD
        self._st_pos = 0

    def _comb(self, j: int, x: np.ndarray) -> np.ndarray:
        buf = self._rv_comb[j]
        i = self._rv_comb_i[j]
        delayed = _ring_read(buf, i, len(x))
        y = x + REVERB_FB * delayed
        # One-sample average damps highs on every pass round the loop, the
        # same trick the plucked string uses. Real rooms lose treble fastest;
        # an undamped comb sounds like a metal tank.
        damped = 0.5 * (y + np.concatenate(([self._rv_comb_prev[j]], y[:-1])))
        self._rv_comb_prev[j] = float(y[-1])
        _ring_write(buf, i, damped)
        self._rv_comb_i[j] = (i + len(x)) % len(buf)
        return y

    def _allpass(self, j: int, x: np.ndarray) -> np.ndarray:
        buf = self._rv_ap[j]
        i = self._rv_ap_i[j]
        delayed = _ring_read(buf, i, len(x))
        y = delayed - x
        _ring_write(buf, i, x + delayed * REVERB_AP_G)
        self._rv_ap_i[j] = (i + len(x)) % len(buf)
        return y

    def _reverb(self, x: np.ndarray, frames: int) -> np.ndarray:
        """Schroeder reverb, fully vectorized (see REVERB_COMBS)."""
        wet = np.zeros(frames)
        for j in range(len(REVERB_COMBS)):
            wet += self._comb(j, x)
        wet /= len(REVERB_COMBS)
        for j in range(len(REVERB_APS)):
            wet = self._allpass(j, wet)
        return wet

    def _voice_delay(self, x: np.ndarray, frames: int) -> np.ndarray:
        buf = self._vd_buf
        n = len(buf)
        w = self._vd_w
        k = np.arange(frames)
        self._echo_fb += (self.target_echo - self._echo_fb) * 0.15
        y = x + buf[(w + k - VOICE_DELAY) % n] * self._echo_fb
        buf[(w + k) % n] = y
        self._vd_w = (w + frames) % n
        return y

    def _stutter(self, x: np.ndarray, frames: int) -> np.ndarray:
        """Keep the rolling history fed; replace the signal while a grab is
        playing, fading out over the tail so it does not end on a click."""
        h = self._st_hist
        n = len(h)
        w = self._st_hw
        k = np.arange(frames)
        h[(w + k) % n] = x
        self._st_hw = (w + frames) % n

        if self._st_left <= 0:
            return x
        idx = (self._st_pos + k) % len(self._st_buf)
        out = self._st_buf[idx]
        self._st_pos = int((self._st_pos + frames) % len(self._st_buf))
        left = self._st_left - k
        fade = np.clip(left / (0.25 * SAMPLE_RATE), 0.0, 1.0)
        self._st_left -= frames
        return out * fade

    def _pitch_shift(self, x: np.ndarray, frames: int, ratio: float) -> np.ndarray:
        """Granular delay-line pitch shifter: two taps chase the write head at
        `ratio` speed, each windowed by a half-offset sine and crossfaded so
        the periodic tap-wrap is inaudible. ~25 ms latency."""
        n = np.arange(frames)
        w = self._ps_w
        self._ps_buf[(w + n) % PS_BUF] = x
        self._ps_w = (w + frames) % PS_BUF

        dphi = (1.0 - ratio) / PS_WIN
        phase = (self._ps_phase + dphi * n) % 1.0
        self._ps_phase = float((self._ps_phase + dphi * frames) % 1.0)
        shifted = np.zeros(frames)
        gain_sum = np.zeros(frames)
        for offset in (0.0, 0.5):
            p = (phase + offset) % 1.0
            pos = (w + n - (p * PS_WIN + 2.0)) % PS_BUF
            i0 = np.floor(pos).astype(np.intp)
            frac = pos - i0
            tap = self._ps_buf[i0] * (1.0 - frac) + self._ps_buf[(i0 + 1) % PS_BUF] * frac
            g = np.sin(np.pi * p)
            shifted += tap * g
            gain_sum += g
        return shifted / (gain_sum + 1e-6)

    def _noise_gate(self, x: np.ndarray) -> np.ndarray:
        """Door opens for speech, stays shut for room hiss — which would
        otherwise be robotized into an annoying drone."""
        rms = float(np.sqrt((x ** 2).mean()))
        self._gate = 0.85 * self._gate + 0.15 * rms
        return x * np.clip((self._gate - 0.004) / 0.012, 0.0, 1.0)

    def _process_voice(self, x: np.ndarray, frames: int) -> np.ndarray:
        """Voice-hand chain: gate -> pitch -> stutter -> delay -> reverb.

        Returns the block already at its own level; panning happens in the
        callback so the voice keeps a stereo position independent of the
        instrument's.
        """
        k = 0.15
        self._v_pitch += (self.target_voice_pitch - self._v_pitch) * k
        self._v_reverb += (self.target_voice_reverb - self._v_reverb) * k
        self._v_pan += (self.target_voice_pan - self._v_pan) * k
        self._v_vol += (self.target_voice_volume - self._v_vol) * k

        y = self._noise_gate(x)
        if not self.voice_fx_on:
            # Open hand: completely dry. Still feed the stutter history so a
            # flick right after re-activating has something to grab.
            self._stutter(y, frames)
            return y * self._v_vol

        y = self._pitch_shift(y, frames, self._v_pitch)
        y = self._stutter(y, frames)
        if self.voice_delay_on:
            y = self._voice_delay(y, frames)
        if self._v_reverb > 1e-3:
            wet = self._reverb(y, frames)
            y = y * (1.0 - 0.6 * self._v_reverb) + wet * self._v_reverb
        return y * self._v_vol

    def _render_tone(self, frames: int) -> np.ndarray:
        """One block of the selected instrument at the current frequency.

        Each instrument is a cheap vectorized recipe; self._ph holds phase
        accumulators for the fundamental and extra partials so pitch changes
        stay click-free. The per-instrument envelope from ENVELOPES is
        applied here — it does more to separate a struck bell from a bowed
        pad than the harmonic recipes do.
        """
        attack, decay, sustain, struck = ENVELOPES[self.instrument]

        # A change in target_freq is a note-on: mapping.py has already
        # quantized roll to a scale, so the target only moves in steps.
        # Struck instruments jump to the new pitch — a bell does not glide.
        if abs(self.target_freq - self._note_freq) > 1e-6:
            self._note_freq = self.target_freq
            if struck:
                self._freq = self.target_freq
            self._note_on()

        # Vibrato: the second finger wobbles the pitch, like a singer or a
        # string player. Applied to the increment so every instrument gets it.
        self._vibDepth += (self.target_vibrato - self._vibDepth) * 0.1
        n = np.arange(frames)
        two_pi = 2.0 * np.pi
        env = self._env_block(n, frames, attack, decay, sustain)
        if self._vibDepth > 1e-3:
            lfo = np.sin(two_pi * (self._vib + VIBRATO_HZ / SAMPLE_RATE * n)).mean()
            self._vib = float((self._vib + VIBRATO_HZ * frames / SAMPLE_RATE) % 1.0)
            freq = self._freq * (1.0 + VIBRATO_DEPTH * self._vibDepth * lfo)
        else:
            freq = self._freq
        inc = freq / SAMPLE_RATE

        def ph(k: int, mult: float = 1.0) -> np.ndarray:
            p = (self._ph[k] + inc * mult * n) % 1.0
            self._ph[k] = float((self._ph[k] + inc * mult * frames) % 1.0)
            return p

        def saw(k: int, mult: float = 1.0) -> np.ndarray:
            return _poly_saw(ph(k, mult), inc * mult)

        ins = self.instrument
        if ins == "pluck":
            # The string carries its own decay, so the envelope above only
            # supplies the attack.
            tone = self._render_ks(frames) * KS_GAIN
        elif ins == "guitar":
            # String -> pickup -> overdrive -> cabinet, the order a real rig
            # works in. The clipper is also what gives the note its sustain:
            # it compresses hard while the string is loud and cleans up as the
            # string decays, so the note blooms instead of just fading.
            string = self._render_ks(frames, pickup=GUITAR_PICKUP)
            tone = self._cabinet(np.tanh(string * GUITAR_DRIVE)) * 0.55
        elif ins == "organ":
            # Hammond drawbars: strong octaves (2x, 4x) over a weak third
            # partial. A 1/n series here would just *be* a sawtooth, which is
            # why the old weights measured 0.98 spectrally similar to "saw".
            tone = (
                np.sin(two_pi * ph(0))
                + 0.80 * np.sin(two_pi * ph(1, 2.0))
                + 0.18 * np.sin(two_pi * ph(2, 3.0))
                + 0.55 * np.sin(two_pi * ph(3, 4.0))
            ) / 2.1
        elif ins == "strings":
            # Bowed ensemble. The detune has to be wide and unevenly spaced:
            # the old symmetric +/-0.5% put both beat rates at 1.1 Hz, so the
            # three saws swelled as one slow lump instead of blurring into a
            # section. Four voices, and the gaps between them have to be
            # unequal too — evenly spaced detune makes every adjacent pair
            # beat at the same rate, and they reinforce into exactly the
            # pulsing this is meant to avoid. The slow attack does the rest
            # of the bowing.
            tone = (
                saw(0, 0.9865) + saw(1, 0.9971) + saw(2, 1.0032) + saw(3, 1.0148)
            ) / 3.2
        elif ins == "bell":
            # 2-op FM, inharmonic ratio. The modulation index has to decay
            # faster than the amplitude: that bright metallic strike settling
            # into a pure tone is the "ding". A fixed index is a static buzz.
            idx = 7.0 * env ** 2.0
            tone = np.sin(two_pi * ph(0) + idx * np.sin(two_pi * ph(1, 2.76)))
        elif ins == "flute":
            # Sine + a little octave, and breath noise that is loud for the
            # first ~50 ms (the player's "chiff") then drops to a whisper.
            # Constant noise just reads as hiss and swamped the harmonics.
            lfo = np.sin(two_pi * (self._lfo + 5.0 / SAMPLE_RATE * n))
            self._lfo = float((self._lfo + 5.0 * frames / SAMPLE_RATE) % 1.0)
            chiff = self._chiff * np.exp(-n / (0.05 * SAMPLE_RATE))
            self._chiff = float(chiff[-1])
            tone = 0.85 * (
                np.sin(two_pi * ph(0) + 0.3 * lfo)
                + 0.14 * np.sin(two_pi * ph(1, 2.0))
            ) + self._rng.standard_normal(frames) * (0.008 + 0.10 * chiff)
        else:
            # default: classic bright sawtooth
            tone = saw(0)
        return tone * env

    def _env_block(
        self, n: np.ndarray, frames: int, attack: float, decay: float, sustain: float
    ) -> np.ndarray:
        """One block of the amplitude envelope.

        Two exponential stages — rise to 1, then fall to the sustain level.
        Each stage is exact per sample; only the switch between them lands on
        a block boundary (~6 ms), which is well under the shortest attack
        anyone can hear as anything but instant.
        """
        if self._env_stage == 0:
            tau = max(attack, 1e-4) * SAMPLE_RATE / 3.0  # 3 tau ~= 95%
            gap = 1.0 - self._env
            env = 1.0 - gap * np.exp(-n / tau)
            self._env = float(1.0 - gap * np.exp(-frames / tau))
            if self._env > 0.99:
                self._env_stage = 1
        else:
            tau = max(decay, 1e-4) * SAMPLE_RATE / 3.0
            gap = self._env - sustain
            env = sustain + gap * np.exp(-n / tau)
            self._env = float(sustain + gap * np.exp(-frames / tau))
        return env

    def _ks_pluck(self) -> None:
        """Excite the string: fill one wavelength of the delay line with
        noise. Every pass round the loop then runs it through the two-point
        average, so the harmonics die off fastest — the same way a real
        string loses its brightness long before it goes quiet."""
        t60, self._ks_b = KS_VOICES[self.instrument]
        n = int(np.clip(SAMPLE_RATE / max(self._freq, 20.0), 8, KS_MAX))
        self._ks_n = n
        exc = self._rng.standard_normal(n)
        self._ks_buf[:n] = exc * (0.9 / max(float(np.abs(exc).max()), 1e-6))
        self._ks_i = 0
        self._ks_prev = 0.0
        # The loop gain hits any given sample once per *pass* round the delay
        # line, not once per sample — hence the factor of n. Leave it out and
        # a 2 s decay comes out around 250 s, which just sounds like a drone.
        self._ks_decay = float(np.exp(-6.9078 * n / (t60 * SAMPLE_RATE)))

    def _render_ks(self, frames: int, pickup: float = 0.0) -> np.ndarray:
        """Karplus-Strong: read the delay line, write back a weighted average
        of the last two samples. Recursive by nature, so this is the one part
        of the engine that cannot be vectorized — same per-sample shape as the
        ladder filter in _callback.

        `pickup` places a second tap that fraction of the way along the
        string and subtracts it. The delay line really is the string, so a
        second tap really is a second listening point, and the comb notches
        that fall out are the same ones that make a bridge pickup sound
        nasal. It is the cheapest honest thing in the whole engine.
        """
        buf = self._ks_buf
        n, i, prev, d = self._ks_n, self._ks_i, self._ks_prev, self._ks_decay
        b = self._ks_b
        c = 1.0 - b
        tap = int(n * pickup)
        pk = 0.62 if tap else 0.0
        out = np.empty(frames)
        for k in range(frames):
            v = buf[i]
            j = i + tap
            if j >= n:
                j -= n
            out[k] = v - pk * buf[j]
            buf[i] = (b * v + c * prev) * d
            prev = v
            i += 1
            if i >= n:
                i = 0
        self._ks_i, self._ks_prev = i, float(prev)
        return out

    def _cabinet(self, x: np.ndarray) -> np.ndarray:
        """Guitar speaker: two poles down above ~4 kHz, one pole up below
        ~90 Hz. A real cab is a narrow, lossy box, and skipping it is what
        makes an amp sim sound like a buzzsaw — the clipper generates
        harmonics all the way to Nyquist and something has to remove them."""
        a = 1.0 - np.exp(-2.0 * np.pi * GUITAR_CAB_HI / SAMPLE_RATE)
        r = float(np.exp(-2.0 * np.pi * GUITAR_CAB_LO / SAMPLE_RATE))
        y1, y2, hp = self._cab
        out = np.empty(len(x))
        for i in range(len(x)):
            y1 += a * (x[i] - y1)
            y2 += a * (y1 - y2)
            hp = r * hp + (1.0 - r) * y2   # track the lows...
            out[i] = y2 - hp               # ...then subtract them
        self._cab = [y1, y2, hp]
        return out

    def _render_grains(self, buf: np.ndarray, frames: int) -> np.ndarray:
        """Granular cloud: short Hann-windowed grains drawn from the scrub
        position. Time is frozen — the hand moves the playhead by rolling."""
        self._scrub += (self.target_scrub - self._scrub) * 0.15
        out = np.zeros(frames)
        span = len(buf) - GRAIN_LEN - 1
        self._grain_timer -= frames
        while self._grain_timer <= 0 and span > 0:
            jitter = self._rng.uniform(-0.03, 0.03) * SAMPLE_RATE
            start = int(np.clip(self._scrub * span + jitter, 0, span))
            self._grains.append([start, 0])
            self._grain_timer += GRAIN_HOP
        alive = []
        for start, age in self._grains:
            n = min(frames, GRAIN_LEN - age)
            out[:n] += buf[start + age : start + age + n] * self._grain_win[age : age + n]
            if age + n < GRAIN_LEN:
                alive.append([start, age + n])
        self._grains = alive
        return out

    def _render_shot(self, buf: np.ndarray, frames: int) -> np.ndarray | None:
        """One-shot slice playback (slice mode), with 5 ms edge fades."""
        s = self._shot
        if s is None:
            return None
        start, pos, end = s
        n = min(frames, end - pos)
        if n <= 0:
            self._shot = None
            return None
        idx = np.arange(pos, pos + n)
        fade = np.clip((idx - start) / 220.0, 0.0, 1.0)
        fade = np.minimum(fade, np.clip((end - idx) / 220.0, 0.0, 1.0))
        out = np.zeros(frames)
        out[:n] = buf[pos : pos + n] * fade
        s[1] = pos + n
        if s[1] >= end:
            self._shot = None
        return out
