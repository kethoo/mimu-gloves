"""Instruments you design yourself, saved between sessions.

The seven built-in instruments are hardcoded branches in Synth._render_tone,
but underneath they are only four recipes with different numbers in them: a
stack of partials (organ, flute), detuned saws (strings), two-operator FM
(bell), and a plucked string (pluck, guitar). A custom instrument is those
same engines with the numbers exposed, so designing one needs no new DSP —
it reuses the code that already makes the built-ins sound like themselves.

Stored as JSON next to the project, one object per instrument, so a patch
you like survives a restart and can be committed or shared.
"""

from __future__ import annotations

import json
import pathlib

STORE = pathlib.Path(__file__).parent / "instruments.json"

# Engine -> the parameters it reads, with (default, low, high) for each. The
# UI builds its sliders from this table rather than from a hardcoded form,
# so adding a knob here is enough to make it appear and be saved.
ENGINES: dict[str, dict] = {
    "additive": {
        "label": "Additive — partials stacked over the fundamental",
        "params": {
            "p1": (1.00, 0.0, 1.0), "p2": (0.40, 0.0, 1.0),
            "p3": (0.15, 0.0, 1.0), "p4": (0.30, 0.0, 1.0),
            "p5": (0.05, 0.0, 1.0), "p6": (0.10, 0.0, 1.0),
            "noise": (0.00, 0.0, 0.3),
        },
    },
    "saws": {
        "label": "Detuned saws — an ensemble, like the string pad",
        "params": {
            # Spread is in cents between adjacent voices. Kept away from
            # zero because identical saws are just a louder single saw.
            "voices": (4, 2, 6),
            "spread": (18.0, 2.0, 60.0),
        },
    },
    "fm": {
        "label": "2-op FM — ratio decides metallic vs. warm",
        "params": {
            # Non-integer ratios are inharmonic, which is what makes a bell
            # sound struck rather than played.
            "ratio": (2.76, 0.5, 8.0),
            "index": (7.0, 0.0, 14.0),
            # How fast the modulation decays relative to the amplitude. The
            # bright strike settling to a pure tone is this number.
            "index_decay": (2.0, 0.5, 6.0),
        },
    },
    "string": {
        "label": "Plucked string — Karplus-Strong, like pluck and guitar",
        "params": {
            "t60": (2.0, 0.2, 8.0),        # seconds to fall 60 dB
            "damping": (0.50, 0.0, 0.95),  # loop filter: dull vs. bright
            "pickup": (0.0, 0.0, 0.5),     # 0 = none, else tap along string
            "drive": (0.0, 0.0, 20.0),     # overdrive into a speaker model
        },
    },
}

ENV_RANGES = {
    "attack": (0.01, 0.001, 1.0),
    "decay": (0.30, 0.005, 4.0),
    "sustain": (0.72, 0.0, 1.0),
}


def defaults(engine: str) -> dict:
    """A fresh patch for an engine, with every parameter at its default."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    patch = {k: v[0] for k, v in ENV_RANGES.items()}
    patch["struck"] = engine in ("fm", "string")
    patch["engine"] = engine
    patch["params"] = {k: v[0] for k, v in ENGINES[engine]["params"].items()}
    return patch


def _clamp_patch(patch: dict) -> dict:
    """Bring a patch inside its declared ranges.

    Everything here can arrive from a JSON file a person edited by hand or
    from a browser, so nothing is trusted: an out-of-range decay is a
    denormal storm in the audio callback, and a missing key is a KeyError
    at 44100 Hz.
    """
    engine = patch.get("engine")
    if engine not in ENGINES:
        engine = "additive"
    out = defaults(engine)
    for k, (_d, lo, hi) in ENV_RANGES.items():
        try:
            out[k] = min(max(float(patch.get(k, out[k])), lo), hi)
        except (TypeError, ValueError):
            pass
    out["struck"] = bool(patch.get("struck", out["struck"]))
    src = patch.get("params") or {}
    for k, (_d, lo, hi) in ENGINES[engine]["params"].items():
        try:
            out["params"][k] = min(max(float(src.get(k, out["params"][k])), lo), hi)
        except (TypeError, ValueError):
            pass
    if engine == "saws":
        out["params"]["voices"] = int(round(out["params"]["voices"]))
    return out


def load() -> dict[str, dict]:
    """Every saved instrument, by name. Never raises: a broken store must
    not stop the instrument starting."""
    if not STORE.exists():
        return {}
    try:
        raw = json.loads(STORE.read_text())
    except Exception as exc:
        print(f"[instruments] ignoring unreadable {STORE.name}: {exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, patch in raw.items():
        if isinstance(patch, dict):
            out[str(name)[:32]] = _clamp_patch(patch)
    return out


def save(name: str, patch: dict) -> dict:
    """Add or replace one instrument and write the store back."""
    name = str(name).strip()[:32]
    if not name:
        raise ValueError("an instrument needs a name")
    all_ = load()
    all_[name] = _clamp_patch(patch)
    STORE.write_text(json.dumps(all_, indent=2, sort_keys=True) + "\n")
    print(f"[instruments] saved '{name}' ({all_[name]['engine']})")
    return all_[name]


def delete(name: str) -> bool:
    all_ = load()
    if name not in all_:
        return False
    del all_[name]
    STORE.write_text(json.dumps(all_, indent=2, sort_keys=True) + "\n")
    print(f"[instruments] deleted '{name}'")
    return True
