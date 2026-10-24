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

# The filter, as part of the patch. This is the big one: every instrument
# used to share one filter at one cutoff with one fixed resonance, so
# whatever the oscillators did, the thing that shapes a sound most was
# identical for all of them — measured, nothing above the 5th harmonic
# survived for any instrument, which is exactly where they differ. In a
# real synth the filter belongs to the patch, and that is most of why two
# patches on the same oscillator can sound nothing alike.
FILTER_RANGES = {
    # Where this patch's filter sits. The hand still sweeps it; this is the
    # centre that sweep moves around, so a dark pad and a bright lead stay
    # dark and bright wherever the wrist is.
    "cutoff": (2000.0, 150.0, 18000.0),
    # 0 is a clean lowpass; past about 2.5 the peak starts to sing, which is
    # the whole character of an acid or a vocal-ish patch.
    "resonance": (1.1, 0.0, 3.6),
    # How far the envelope opens the filter on each note, in octaves. This
    # is the classic filter "wow" and no instrument here had it.
    "env_amount": (0.0, -3.0, 5.0),
    "env_decay": (0.25, 0.01, 3.0),
}

ENV_RANGES = {
    "attack": (0.01, 0.001, 1.0),
    "decay": (0.30, 0.005, 4.0),
    "sustain": (0.72, 0.0, 1.0),
}


# Starting points, the way every synth opens on a preset rather than on a
# blank patch. They are deliberately NOT the built-ins' own numbers: a first
# attempt used those as the defaults, and every instrument you could make
# came out a near-clone — measured 0.98 spectral similarity between a
# default additive patch and the built-in saw, with custom patches ending up
# more alike the built-ins than the built-ins are to each other.
#
# Each one below is measured against all seven built-ins; the comment is its
# similarity to the nearest. Anything above about 0.85 is a clone and does
# not belong here.
STARTERS: dict[str, dict] = {
    "Clang": {   # 0.24 from flute, its nearest
        "engine": "fm", "attack": 0.004, "decay": 1.2, "sustain": 0.2,
        "struck": True,
        "cutoff": 9000.0, "resonance": 0.3, "env_amount": -2.0, "env_decay": 0.5,
        "params": {"ratio": 1.41, "index": 12.0, "index_decay": 0.7},
    },
    "Growl": {   # 0.66 from organ
        "engine": "fm", "attack": 0.05, "decay": 0.4, "sustain": 0.9,
        "struck": False,
        "cutoff": 700.0, "resonance": 3.2, "env_amount": 2.5, "env_decay": 0.35,
        "params": {"ratio": 0.5, "index": 3.0, "index_decay": 0.5},
    },
    "Air": {     # 0.38 from pluck
        "engine": "additive", "attack": 0.25, "decay": 0.6, "sustain": 0.8,
        "struck": False,
        "cutoff": 6000.0, "resonance": 0.1, "env_amount": 1.5, "env_decay": 2.0,
        "params": {"p1": 0.1, "p2": 0.2, "p3": 0.4, "p4": 0.9, "p5": 1.0,
                   "p6": 0.8, "noise": 0.12},
    },
    "Hollow": {  # 0.80 from saw — odd partials only, a squarish clarinet
        "engine": "additive", "attack": 0.02, "decay": 0.4, "sustain": 0.9,
        "struck": False,
        "cutoff": 1400.0, "resonance": 2.8, "env_amount": 3.0, "env_decay": 0.12,
        "params": {"p1": 1.0, "p2": 0.0, "p3": 0.7, "p4": 0.0, "p5": 0.5,
                   "p6": 0.0, "noise": 0.0},
    },
    "Huge": {    # 0.75 from saw
        "engine": "saws", "attack": 0.4, "decay": 0.9, "sustain": 0.9,
        "struck": False,
        "cutoff": 2600.0, "resonance": 1.6, "env_amount": 1.2, "env_decay": 1.4,
        "params": {"voices": 6, "spread": 60.0},
    },
    "Tick": {    # 0.38 from strings
        "engine": "string", "attack": 0.001, "decay": 0.01, "sustain": 1.0,
        "struck": True,
        "cutoff": 12000.0, "resonance": 0.0, "env_amount": -4.0, "env_decay": 0.06,
        "params": {"t60": 0.25, "damping": 0.05, "pickup": 0.45, "drive": 0.0},
    },
    "Howl": {    # 0.58 from guitar
        "engine": "string", "attack": 0.001, "decay": 0.01, "sustain": 1.0,
        "struck": True,
        "cutoff": 900.0, "resonance": 3.4, "env_amount": 4.0, "env_decay": 1.8,
        "params": {"t60": 8.0, "damping": 0.9, "pickup": 0.1, "drive": 18.0},
    },
}


def starter(name: str) -> dict:
    """A named starting point, validated like anything else."""
    return _clamp_patch(STARTERS[name])


# Six knobs per engine, each writing several of the raw parameters at once.
# p1..p6, index_decay and t60 are what the synthesis needs; they are not what
# anyone reaching for a sound is thinking about. Four of the six mean the
# same thing on every engine. The last two cannot: "detune" is meaningless
# on a single FM pair and "pickup" is meaningless without a string, so those
# two are named for what they actually do on the engine you picked rather
# than given a label that lies.
#
# Each target is (param, low, high) and optionally "exp" for a ratio-spaced
# sweep, which is how time and frequency have to move to feel even.
MACROS = {
    "additive": [
        ("bright", [("cutoff", 350.0, 14000.0, "exp"), ("p4", 0.05, 0.95),
                    ("p5", 0.0, 0.9), ("p6", 0.0, 0.8)]),
        ("body", [("p1", 0.25, 1.0), ("p2", 0.1, 0.85)]),
        ("attack", [("attack", 0.001, 0.8, "exp")]),
        ("release", [("decay", 0.03, 3.5, "exp")]),
        ("air", [("noise", 0.0, 0.25)]),
        ("reso", [("resonance", 0.0, 3.4)]),
    ],
    "saws": [
        ("bright", [("cutoff", 350.0, 14000.0, "exp")]),
        ("body", [("voices", 2, 6)]),
        ("attack", [("attack", 0.001, 0.8, "exp")]),
        ("release", [("decay", 0.03, 3.5, "exp")]),
        ("detune", [("spread", 2.0, 60.0)]),
        ("reso", [("resonance", 0.0, 3.4)]),
    ],
    "fm": [
        ("bright", [("cutoff", 350.0, 14000.0, "exp"), ("index", 0.5, 14.0)]),
        ("body", [("index_decay", 0.5, 5.0)]),
        ("attack", [("attack", 0.001, 0.8, "exp")]),
        ("release", [("decay", 0.03, 3.5, "exp")]),
        ("ratio", [("ratio", 0.5, 8.0)]),
        ("reso", [("resonance", 0.0, 3.4)]),
    ],
    "string": [
        ("bright", [("cutoff", 350.0, 14000.0, "exp"), ("damping", 0.95, 0.05)]),
        ("body", [("t60", 0.25, 8.0, "exp")]),
        ("attack", [("attack", 0.001, 0.8, "exp")]),
        ("release", [("decay", 0.03, 3.5, "exp")]),
        ("pickup", [("pickup", 0.0, 0.5)]),
        ("drive", [("drive", 0.0, 20.0)]),
    ],
}


def defaults(engine: str) -> dict:
    """A fresh patch for an engine, with every parameter at its default."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}")
    patch = {k: v[0] for k, v in ENV_RANGES.items()}
    patch.update({k: v[0] for k, v in FILTER_RANGES.items()})
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
    for k, (_d, lo, hi) in {**ENV_RANGES, **FILTER_RANGES}.items():
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
