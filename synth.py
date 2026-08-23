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
ECHO_DELAY = int(0.30 * SAMPLE_RATE) # live voice echo time
ECHO_LEN = ECHO_DELAY + BLOCK

INSTRUMENTS = {
    "saw": "Saw Lead",
    "organ": "Organ",
    "strings": "String Pad",
    "bell": "FM Bell",
    "flute": "Flute",
}


class GloveSynth:
    def __init__(self) -> None:
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
        self._lp = 0.0            # one-pole lowpass state
        self._pluck_env = 0.0     # decaying envelope for percussive hits
        self._pluck_lp = 0.0
        self._rng = np.random.default_rng()

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
        self._echo_buf = np.zeros(ECHO_LEN)
        self._echo_w = 0
        self._echo_fb = 0.0

        # Duplex stream (mic in + speakers out in one callback) so live
        # mode works; fall back to output-only if there's no input device.
        try:
            self._stream = sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK,
                channels=(1, 2),
                dtype="float32",
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
                callback=self._callback,
            )
            self._has_input = False

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()

    def pluck(self) -> None:
        """Trigger a percussive hit (punch gesture)."""
        self._pluck_env = 1.0

    def set_instrument(self, name: str) -> None:
        if name in INSTRUMENTS:
            self.instrument = name
            print(f"\n[instrument: {INSTRUMENTS[name]}]")

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
                # Solid base level so the voice is always clearly audible;
                # motion still adds a swell on top.
                saw = saw + voice * (0.6 + self._amp)

        # Live voice: mic -> pitch shifter -> echo, mixed pre-filter so
        # tilt (brightness) and yaw (pan) shape it like everything else.
        live_in = self._live_in
        if self.live_on and live_in is not None and len(live_in) == frames:
            saw = saw + self._process_live(live_in, frames) * 0.9

        # One-pole lowpass, coefficient from cutoff frequency.
        a = 1.0 - np.exp(-2.0 * np.pi * self._cutoff / SAMPLE_RATE)
        lp = np.empty(frames)
        y = self._lp
        for i in range(frames):
            y += a * (saw[i] - y)
            lp[i] = y
        self._lp = y

        mono = lp

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

        # Equal-power stereo pan.
        angle = (self._pan + 1.0) * (np.pi / 4.0)
        out[:, 0] = (mono * np.cos(angle)).astype(np.float32)
        out[:, 1] = (mono * np.sin(angle)).astype(np.float32)

    def _process_live(self, x: np.ndarray, frames: int) -> np.ndarray:
        """Real-time voice manipulation: noise gate -> granular pitch
        shifter -> feedback echo.

        The pitch shifter is the classic delay-line design: the input goes
        into a ring buffer, and two read taps chase the write head at
        `pitch ratio` speed, each faded by a half-offset sine window and
        crossfaded so the periodic tap-wrap is inaudible. ~25 ms latency.
        """
        n = np.arange(frames)

        # Noise gate: door opens for speech, stays shut for room hiss
        # (which would otherwise be robotized into an annoying drone).
        rms = float(np.sqrt((x ** 2).mean()))
        self._gate = 0.85 * self._gate + 0.15 * rms
        x = x * np.clip((self._gate - 0.004) / 0.012, 0.0, 1.0)

        # Write the block into the ring buffer.
        w = self._ps_w
        self._ps_buf[(w + n) % PS_BUF] = x
        self._ps_w = (w + frames) % PS_BUF

        # Two pitch-shift taps, half a window apart.
        self._pshift += (self.target_rate - self._pshift) * 0.15
        dphi = (1.0 - self._pshift) / PS_WIN
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
        shifted /= gain_sum + 1e-6

        # Feedback echo; hand motion controls how long the trails ring.
        self._echo_fb += (self.target_echo - self._echo_fb) * 0.15
        ew = self._echo_w
        y = shifted + self._echo_buf[(ew + n - ECHO_DELAY) % ECHO_LEN] * self._echo_fb
        self._echo_buf[(ew + n) % ECHO_LEN] = y
        self._echo_w = (ew + frames) % ECHO_LEN
        return y

    def _render_tone(self, frames: int) -> np.ndarray:
        """One block of the selected instrument at the current frequency.

        Each instrument is a cheap vectorized recipe; self._ph holds phase
        accumulators for the fundamental and extra partials so pitch changes
        stay click-free.
        """
        # Vibrato: the second finger wobbles the pitch, like a singer or a
        # string player. Applied to the increment so every instrument gets it.
        self._vibDepth += (self.target_vibrato - self._vibDepth) * 0.1
        n = np.arange(frames)
        two_pi = 2.0 * np.pi
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

        ins = self.instrument
        if ins == "organ":
            # Additive: fundamental + 3 harmonics, drawbar-style.
            return (
                np.sin(two_pi * ph(0))
                + 0.5 * np.sin(two_pi * ph(1, 2.0))
                + 0.35 * np.sin(two_pi * ph(2, 3.0))
                + 0.2 * np.sin(two_pi * ph(3, 4.0))
            ) / 1.6
        if ins == "strings":
            # Three slightly detuned saws beat against each other: pad.
            return (
                2.0 * ph(0, 0.995) + 2.0 * ph(1) + 2.0 * ph(2, 1.005) - 3.0
            ) / 2.2
        if ins == "bell":
            # 2-op FM with an inharmonic ratio: metallic.
            return 0.9 * np.sin(
                two_pi * ph(0) + 2.0 * np.sin(two_pi * ph(1, 2.76))
            )
        if ins == "flute":
            # Sine with 5 Hz vibrato and a whisper of breath noise.
            lfo = np.sin(two_pi * (self._lfo + 5.0 / SAMPLE_RATE * n))
            self._lfo = float((self._lfo + 5.0 * frames / SAMPLE_RATE) % 1.0)
            return np.sin(two_pi * ph(0) + 0.3 * lfo) + 0.03 * self._rng.standard_normal(frames)
        # default: classic bright sawtooth
        return 2.0 * ph(0) - 1.0

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
