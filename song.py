"""Load a song into the loop buffer, so the gloves can play a record.

The loop hand already knows how to scrub, stretch, freeze, slice and
pitch-shift whatever is in `Synth._loop_buf` — it just never had anything in
there but your own microphone. This puts a finished track there instead, and
every gesture that worked on a mic take works on it unchanged.

    python main.py --ble --voice-glove --web --song track.mp3
    python main.py --web --song "https://..." --song-start 60 --song-seconds 30

Decoding goes through libsndfile (WAV, FLAC, OGG, MP3) and falls back to
ffmpeg for anything it does not know, which is most of what a download
gives you. Links need yt-dlp; both are optional and imported lazily, so
nothing here is required to run the instrument.

A note on links: downloading audio from YouTube is against their terms of
service. Fine for trying this out on your own machine, not something to
demo publicly or lean on in the writeup — use a file you own for that.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import subprocess
import tempfile

import numpy as np

CACHE = pathlib.Path(__file__).parent / ".song-cache"


def _is_url(src: str) -> bool:
    return src.startswith(("http://", "https://", "www."))


# A trailing "#t=90", "#90" or "#90-110" picks a section, so the whole
# request fits in one box in the UI. Only a numeric fragment counts: a file
# really can be called "mix #2.wav", and that must keep working.
_FRAGMENT = re.compile(r"#(?:t=)?(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?$")


def _split_fragment(src: str) -> tuple[str, float, float | None]:
    """Pull a time range off the end of a path or URL."""
    m = _FRAGMENT.search(src)
    if not m:
        return src, 0.0, None
    start = float(m.group(1))
    end = float(m.group(2)) if m.group(2) else None
    if end is not None and end <= start:
        return src[: m.start()], start, None
    return src[: m.start()], start, (end - start) if end else None


def _ffmpeg_decode(path: pathlib.Path, rate: int = 44100) -> tuple[np.ndarray, int]:
    """Decode anything ffmpeg can read, as mono float32 on stdout.

    Asking ffmpeg for raw PCM avoids a temp WAV and avoids parsing its
    output: the format is fixed by the flags, so what arrives is exactly
    one float per sample.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            f"cannot decode {path.name}: libsndfile does not know this format "
            "and ffmpeg is not installed. `brew install ffmpeg`"
        )
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "f32le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg could not decode {path.name}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).astype(np.float64), rate


def _decode(path: pathlib.Path) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf
    except ImportError:
        return _ffmpeg_decode(path)
    try:
        data, rate = sf.read(str(path), dtype="float64", always_2d=True)
    except Exception:
        # Not a failure worth reporting: libsndfile simply does not do AAC
        # or Opus, which is most of what a download hands you.
        return _ffmpeg_decode(path)
    # Mono, because the loop buffer is one channel. Averaging rather than
    # taking the left channel keeps anything panned hard right audible.
    return data.mean(axis=1), rate


def _tidy_error(exc: Exception, url: str) -> str:
    """One readable line out of a yt-dlp failure.

    Its errors arrive as a paragraph with the extractor name, a stack of
    'caused by' clauses and ANSI colour. That is fine in a terminal and
    useless in the status line of a web page, which is where this one is
    going to be read.
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    text = text.replace("ERROR: ", "")
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)        # drop "[generic] "
    text = text.split(" (caused by")[0].strip()
    if "404" in text or "Not Found" in text:
        return "not found (404) — check the link"
    if "Unable to download webpage" in text or "Failed to resolve" in text:
        return "could not reach that link"
    # Still cap it: some extractor messages run to several lines.
    return text.split("\n")[0][:120] or f"could not download {url}"


def _download(url: str) -> tuple[pathlib.Path, str]:
    """Fetch a link's audio, cached by URL so a second run is instant.

    Returns the file and the track's title. The file is named after a hash
    of the URL — titles are not safe filenames — so the title is kept
    beside it, or the page would show a line of hex where a song name
    should be.
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "links need yt-dlp: pip install yt-dlp (and `brew install ffmpeg`)"
        ) from exc

    CACHE.mkdir(exist_ok=True)
    tag = hashlib.sha256(url.encode()).hexdigest()[:16]
    title_file = CACHE / (tag + ".title")

    def _audio_files():
        # Excludes the .title sidecar, which would otherwise look like the
        # download itself and be handed to the decoder.
        return [p for p in CACHE.glob(tag + ".*") if p.suffix != ".title"]

    cached = _audio_files()
    if cached:
        title = title_file.read_text().strip() if title_file.exists() else cached[0].stem
        print(f"[song] using cached download: {title}")
        return cached[0], title

    print(f"[song] downloading {url} ...")

    class _Silent:
        """`quiet` does not cover errors — yt-dlp writes those to stderr on
        its own. We raise a tidied version of the same thing, so letting it
        print as well just means the raw paragraph lands next to the clean
        line that replaced it."""

        def debug(self, msg): pass
        def info(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(CACHE / (tag + ".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Silent(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise RuntimeError(_tidy_error(exc, url)) from exc
    got = _audio_files()
    if not got:
        raise RuntimeError(f"download produced no file for {url}")
    title = (info.get("title") or got[0].stem).strip()
    title_file.write_text(title)
    print(f"[song] got: {title}")
    return got[0], title


def load(src: str, start: float = 0.0, seconds: float | None = None
         ) -> tuple[np.ndarray, int, str]:
    """Return (mono samples, sample rate, a name to show).

    `start` and `seconds` pick a section, which is usually what you want:
    a whole track makes every scrub gesture cover four minutes at once, so
    a chorus or a break is far more playable than the lot.
    """
    # An explicit start/seconds argument wins over one typed into the text,
    # so --song-start still does what it says alongside a "#90" in the path.
    src, frag_start, frag_seconds = _split_fragment(src.strip())
    start = start or frag_start
    seconds = seconds or frag_seconds

    if _is_url(src):
        url = src if src.startswith("http") else "https://" + src
        path, name = _download(url)
    else:
        path = pathlib.Path(src).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"no such file: {path}")
        name = path.stem

    samples, rate = _decode(path)
    if len(samples) < 2:
        raise RuntimeError(f"{path.name} decoded to nothing")

    total = len(samples) / rate
    if start:
        samples = samples[int(start * rate):]
    if seconds:
        samples = samples[: int(seconds * rate)]
    if len(samples) < 2:
        raise RuntimeError(
            f"{path.name} is {total:.1f}s long; --song-start {start:g} "
            "leaves nothing to play"
        )
    span = len(samples) / rate
    where = f" ({start:g}s..{start + span:g}s of {total:.0f}s)" if start or seconds else ""
    print(f"[song] {name}: {span:.1f}s at {rate} Hz{where}")
    return samples, rate, name


def _temp_wav(samples: np.ndarray, rate: int) -> pathlib.Path:
    """Only for tests: write samples somewhere a decoder can read them."""
    import wave

    path = pathlib.Path(tempfile.mkstemp(suffix=".wav")[1])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path
