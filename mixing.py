#!/usr/bin/env python3
"""
Shared mixing library — imported by both CLI (raw TCP) and Hermes (MCP).

Contains:
  - Role tagging (120+ words, 7 categories)
  - Device matching (find_device, find_param_in_device)
  - SMART_RECOMMENDATIONS mapping
  - Greedy single-shot optimization (find_biggest_deviation)
  - Direct track presets (10 presets, find_preset)
  - Safety: gain ceilings, floors, deadband, red-line
  - Audio validation (validate_audio_signal)
  - Snapshot/rollback data structures

Does NOT contain:
  - Any transport (no TCP, no MCP, no subprocess)
  - Any CLI (no argparse, no main)
"""

import json
import os
import time

# ─── Safety limits ───
GAIN_CEILINGS = {
    "Utility/Gain": 0.75,
    "EQ Eight/Gain": 0.60,
    "Drum Buss/Boom": 0.60,
    "Drum Buss/Drive": 0.55,
    "Saturator/Drive": 0.60,
    "Compressor/Makeup": 0.50,
    "Operator/Level": 0.75,
}
GAIN_FLOORS = {
    "Utility/Gain": 0.0,
    "EQ Eight/Gain": 0.0,
    "Drum Buss/Boom": 0.0,
    "Drum Buss/Drive": 0.0,
    "Saturator/Drive": 0.0,
    "Operator/Level": 0.10,
}
REDLINE_PEAK_DB = -0.5
DEADBAND_SIGMAS = 0.8
PROPORTIONAL_GAIN = 0.5
LOOP_TIME_BUDGET = 120
ITER_TIME_BUDGET = 20

# ─── Role tagging (first word of track name) ───

ROLE_TO_CATEGORY = {
    # LOW END
    "kick": "low_end", "kicks": "low_end",
    "bass": "low_end", "basses": "low_end", "bassline": "low_end",
    "sub": "low_end", "subs": "low_end",
    "rumble": "low_end", "rumbles": "low_end",
    "808": "low_end", "909": "low_end",
    "lowend": "low_end", "low": "low_end",
    "thump": "low_end", "weight": "low_end", "body": "low_end",
    # HIGH FREQ
    "hats": "hi_freq", "hat": "hi_freq",
    "ride": "hi_freq", "rides": "hi_freq",
    "cymbal": "hi_freq", "cymbals": "hi_freq", "crash": "hi_freq",
    "hihat": "hi_freq", "hihats": "hi_freq", "hh": "hi_freq",
    "openhat": "hi_freq", "closedhat": "hi_freq",
    "shimmer": "hi_freq", "sparkle": "hi_freq",
    "top": "hi_freq", "tops": "hi_freq",
    # SYNTH
    "synth": "synth", "synths": "synth",
    "pad": "synth", "pads": "synth",
    "chord": "synth", "chords": "synth",
    "lead": "synth", "leads": "synth",
    "hook": "synth", "hooks": "synth",
    "melody": "synth", "melodies": "synth",
    "arp": "synth", "arps": "synth", "arpeggio": "synth",
    "stab": "synth", "stabs": "synth",
    "pluck": "synth", "plucks": "synth",
    "drone": "synth", "drones": "synth",
    "texture": "synth", "textures": "synth",
    "keys": "synth", "key": "synth",
    "organ": "synth", "piano": "synth",
    # PERCUSSION
    "perc": "percussion", "percs": "percussion", "percussion": "percussion",
    "toms": "percussion", "tom": "percussion",
    "conga": "percussion", "congas": "percussion",
    "clap": "percussion", "claps": "percussion",
    "snare": "percussion", "snares": "percussion",
    "shaker": "percussion", "shakers": "percussion",
    "rim": "percussion", "rims": "percussion", "rimshot": "percussion",
    "cowbell": "percussion", "tambourine": "percussion",
    "maraca": "percussion", "maracas": "percussion",
    "triangle": "percussion",
    "woodblock": "percussion", "block": "percussion",
    "drum": "percussion", "drums": "percussion",
    "click": "percussion", "clicks": "percussion",
    "transient": "percussion",
    # SPATIAL
    "fx": "spatial", "fxs": "spatial", "effect": "spatial", "effects": "spatial",
    "reverb": "spatial", "reverbs": "spatial",
    "delay": "spatial", "delays": "spatial",
    "echo": "spatial", "echoes": "spatial",
    "noise": "spatial", "noises": "spatial",
    "riser": "spatial", "risers": "spatial",
    "sweep": "spatial", "sweeps": "spatial",
    "wash": "spatial", "washes": "spatial",
    "atmosphere": "spatial", "ambience": "spatial", "ambient": "spatial",
    "space": "spatial",
    # MIX BUS
    "group": "mix_bus", "groups": "mix_bus",
    "bus": "mix_bus", "busses": "mix_bus", "buses": "mix_bus",
    "master": "mix_bus", "masters": "mix_bus",
    "mix": "mix_bus", "main": "mix_bus", "sum": "mix_bus",
    # MID
    "vox": "mid", "vocal": "mid", "vocals": "mid",
    "voice": "mid", "voices": "mid",
    "sample": "mid", "samples": "mid",
    "chop": "mid", "chops": "mid",
    "phrase": "mid", "phrases": "mid",
}

CATEGORY_TARGETS = {
    "low_end": ["sub frequencies", "bass (60-120hz)"],
    "hi_freq": ["presence (2-6khz)", "air (6-16khz)"],
    "synth": ["low-mids", "presence (2-6khz)", "narrow", "widen"],
    "percussion": ["presence (2-6khz)"],
    "spatial": ["air (6-16khz)", "narrow"],
    "mix_bus": ["master is too quiet", "master is loud", "reduce master", "raise master",
                "over-compressed", "very dynamic", "more compression", "limited dynamic range"],
    "mid": ["low-mids", "presence (2-6khz)"],
}

DEVICE_TYPES = [
    "EQ Eight", "Compressor", "Glue Compressor", "Saturator",
    "Utility", "Drum Buss", "Auto Filter", "Operator",
    "Wavetable", "Analog",
]


def parse_track_role(track_name):
    if not track_name:
        return None
    first_word = track_name.strip().split()[0].lower()
    for suffix in ["-", "_", ".", ":"]:
        if first_word.endswith(suffix):
            first_word = first_word[:-1]
    return first_word if first_word in ROLE_TO_CATEGORY else None


def get_track_category(track_name):
    role = parse_track_role(track_name)
    return ROLE_TO_CATEGORY.get(role) if role else None


def category_matches_recommendation(category, rec_text):
    if not category or category not in CATEGORY_TARGETS:
        return False
    rec_lower = rec_text.lower()
    for target in CATEGORY_TARGETS[category]:
        target_lower = target.lower()
        # Bidirectional: target in rec OR rec terms in target
        if target_lower in rec_lower:
            return True
        # Check if any word from rec_text appears in target
        rec_words = set(rec_lower.replace('(', '').replace(')', '').replace('-', ' ').split())
        target_words = set(target_lower.replace('(', '').replace(')', '').replace('-', ' ').split())
        if rec_words & target_words:
            return True
    return False


# ─── Device matching ───

def find_device(session_devices, device_types, rec_text=None, exclude_muted=True):
    if rec_text is None:
        for d in session_devices:
            if exclude_muted and d.get("track_muted"):
                continue
            if any(dt.lower() in d["device_name"].lower() for dt in device_types):
                return (d["track_idx"], d["device_idx"], d["device_name"], d["track_name"])
        return None

    matches = []
    for d in session_devices:
        if exclude_muted and d.get("track_muted"):
            continue
        if any(dt.lower() in d["device_name"].lower() for dt in device_types):
            category = get_track_category(d["track_name"])
            if category and category_matches_recommendation(category, rec_text):
                score = 2
            elif category is None:
                score = 1
            else:
                score = 0
            matches.append((score, d))

    if not matches:
        return None
    matches.sort(key=lambda x: (-x[0], x[1]["track_idx"]))
    best = matches[0][1]
    return (best["track_idx"], best["device_idx"], best["device_name"], best["track_name"])


def find_param_in_device(params, param_hints):
    for pname_hint in param_hints:
        for pname, pinfo in params.items():
            if pname_hint.lower() in pname:
                return (pname, pinfo["index"], pinfo["value"])
    return None


# ─── SMART_RECOMMENDATIONS ───

SMART_RECOMMENDATIONS = [
    {"match": ["sub frequencies", "sub is hot", "sub is overwhelming"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain", "low"], "delta_base": -0.08, "ceiling": "EQ Eight/Gain"},
        {"devices": ["Drum Buss"], "params": ["boom"], "delta_base": -0.04, "ceiling": "Drum Buss/Boom"},
    ]},
    {"match": ["sub frequencies", "sub is weak"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.06, "ceiling": "EQ Eight/Gain"},
        {"devices": ["Drum Buss"], "params": ["boom"], "delta_base": 0.03, "ceiling": "Drum Buss/Boom"},
    ]},
    {"match": ["bass (60-120hz) is hot"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain", "level"], "delta_base": -0.05, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["bass (60-120hz) is weak"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.05, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["low-mids", "muddy"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": -0.05, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["thin"], "fix": [
        {"devices": ["Saturator"], "params": ["drive"], "delta_base": 0.03, "ceiling": "Saturator/Drive"},
    ]},
    {"match": ["presence (2-6khz) is harsh"], "fix": [
        {"devices": ["Auto Filter"], "params": ["frequency"], "delta_base": -0.03, "ceiling": None},
        {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta_base": -0.06, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["presence (2-6khz) is weak", "presence is dull"], "fix": [
        {"devices": ["Auto Filter"], "params": ["frequency"], "delta_base": 0.03, "ceiling": None},
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.04, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["air (6-16khz) is harsh"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain", "high", "freq"], "delta_base": -0.04, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["air (6-16khz) is weak", "air is missing"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.04, "ceiling": "EQ Eight/Gain"},
    ]},
    {"match": ["over-compressed", "over compressed"], "fix": [
        {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta_base": 0.03, "ceiling": None},
    ]},
    {"match": ["very dynamic", "more compression"], "fix": [
        {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta_base": -0.02, "ceiling": None},
    ]},
    {"match": ["narrow", "widen"], "fix": [
        {"devices": ["Utility"], "params": ["width"], "delta_base": 0.03, "ceiling": None},
    ]},
    {"match": ["master is too quiet", "raise master"], "fix": [
        {"devices": ["Utility"], "params": ["gain"], "delta_base": 0.03, "ceiling": "Utility/Gain"},
    ]},
    {"match": ["master is loud", "reduce master"], "fix": [
        {"devices": ["Utility"], "params": ["gain"], "delta_base": -0.03, "ceiling": None},
    ]},
]


# ─── Greedy single-shot optimization ───

def find_biggest_deviation(band_issues):
    if not band_issues:
        return None
    significant = [b for b in band_issues if b.get("sigmas", 0) > 0.8]
    if not significant:
        return None
    biggest = max(significant, key=lambda b: b["sigmas"])
    return (biggest["band"], biggest["direction"], biggest["sigmas"])


def map_band_to_fix(band, direction, iteration=0):
    """Map a band+direction to a SMART_RECOMMENDATIONS fix action.
    Returns (fix_dict, rec_text, lower_band) or (None, None, None)."""
    prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)

    if "/" in band:
        lower_band = band.split("/")[0]
        bd = "hot" if direction == "wide" else "weak"
    else:
        lower_band = band
        bd = direction

    band_to_rec = {
        "sub": "sub frequencies" if bd == "weak" else "sub is hot",
        "bass": "bass (60-120hz) is weak" if bd == "weak" else "bass (60-120hz) is hot",
        "low_mid": "low-mids are thin" if bd == "weak" else "low-mids are muddy",
        "mid": "mid range is weak" if bd == "weak" else "mid range is hot",
        "high_mid": "high-mids are weak" if bd == "weak" else "high-mids are hot",
        "presence": "presence is dull" if bd == "weak" else "presence is harsh",
        "air": "air is missing" if bd == "weak" else "air is harsh",
    }
    rec_text = band_to_rec.get(lower_band)
    if not rec_text:
        return None, None, None

    for smart in SMART_RECOMMENDATIONS:
        if any(m.lower() in rec_text.lower() for m in smart["match"]):
            return smart["fix"][0], rec_text, lower_band

    return None, None, None


# ─── Direct track presets ───

PRESETS = {
    "aggressive": {
        "description": "Add grit and intensity — boost drive, tighten attack",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": +0.15, "ceiling": "Saturator/Drive"},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": -0.08, "ceiling": None},
            {"devices": ["Drum Buss"], "params": ["drive"], "delta": +0.10, "ceiling": "Drum Buss/Drive"},
        ]
    },
    "wider": {
        "description": "Increase stereo width and spatial presence",
        "actions": [
            {"devices": ["Utility"], "params": ["width", "stereo width"], "delta": +0.15, "ceiling": None},
            {"devices": ["Reverb"], "params": ["dry/wet", "wet dry", "mix"], "delta": +0.08, "ceiling": None},
        ]
    },
    "darker": {
        "description": "Reduce highs — darker, warmer tone",
        "actions": [
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta": -0.08, "ceiling": None},
            {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta": -0.06, "ceiling": "EQ Eight/Gain"},
        ]
    },
    "brighter": {
        "description": "Boost highs — more presence and air",
        "actions": [
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta": +0.08, "ceiling": None},
            {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta": +0.06, "ceiling": "EQ Eight/Gain"},
        ]
    },
    "punchier": {
        "description": "Faster attack, more transient emphasis",
        "actions": [
            {"devices": ["Compressor", "Glue Compressor"], "params": ["attack"], "delta": -0.10, "ceiling": None},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": -0.05, "ceiling": None},
            {"devices": ["Drum Buss"], "params": ["boom"], "delta": +0.05, "ceiling": "Drum Buss/Boom"},
        ]
    },
    "softer": {
        "description": "Reduce grit — less drive, more dynamic",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": -0.10, "ceiling": None},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": +0.08, "ceiling": None},
        ]
    },
    "bigger": {
        "description": "More reverb, wider, slightly louder",
        "actions": [
            {"devices": ["Reverb"], "params": ["dry/wet", "wet dry", "mix"], "delta": +0.12, "ceiling": None},
            {"devices": ["Reverb"], "params": ["decay", "decay time"], "delta": +0.08, "ceiling": None},
            {"devices": ["Utility"], "params": ["width", "stereo width"], "delta": +0.08, "ceiling": None},
            {"devices": ["Utility"], "params": ["gain"], "delta": +0.03, "ceiling": "Utility/Gain"},
        ]
    },
    "tighter": {
        "description": "Less reverb, shorter decay, more focused",
        "actions": [
            {"devices": ["Reverb"], "params": ["dry/wet", "wet dry", "mix"], "delta": -0.10, "ceiling": None},
            {"devices": ["Reverb"], "params": ["decay", "decay time"], "delta": -0.08, "ceiling": None},
            {"devices": ["Delay", "Echo"], "params": ["dry/wet", "wet dry", "mix"], "delta": -0.08, "ceiling": None},
        ]
    },
    "warmer": {
        "description": "Add saturation, reduce highs, emphasize mids",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": +0.08, "ceiling": "Saturator/Drive"},
            {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta": -0.04, "ceiling": "EQ Eight/Gain"},
        ]
    },
    "clean": {
        "description": "Remove effects — zero saturation, dry reverb, flat EQ",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": -0.30, "ceiling": None},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": +0.15, "ceiling": None},
            {"devices": ["Reverb"], "params": ["dry/wet", "wet dry", "mix"], "delta": -0.30, "ceiling": None},
        ]
    },
}


def find_preset(name):
    name_lower = name.lower().strip()
    if name_lower in PRESETS:
        return name_lower, PRESETS[name_lower]
    for pname, preset in PRESETS.items():
        if pname in name_lower or name_lower in pname:
            return pname, preset
    return None, None


# ─── Audio validation ───

def validate_audio_signal(analysis):
    if not analysis:
        return False, "Analysis failed — cannot validate audio."
    rms = analysis.get("rms_db", -200)
    peak = analysis.get("peak_db", -200)
    lufs = analysis.get("lufs_integrated")

    if rms < -80:
        return False, (
            "No audio detected. Check:\n"
            "  1. Ableton Preferences → Audio → Output Device = BlackHole 2ch\n"
            "  2. Playback is running (press space in Ableton)\n"
            "  3. Master channel is not muted\n"
            "  4. Tracks are not all muted"
        )
    if rms < -35:
        return True, (
            f"Audio is very quiet (RMS {rms:.1f} dB, peak {peak:.1f} dB).\n"
            "Check Ableton master fader and track volumes."
        )
    if peak > -0.3:
        return True, (
            f"WARNING: Peak at {peak:.1f} dBFS — near clipping.\n"
            "Loop will block gain increases (red-line protection active)."
        )
    if peak >= 0.0:
        return True, (
            f"WARNING: Audio is clipping (peak {peak:.1f} dBFS).\n"
            "Reduce master level or track volumes."
        )
    return True, f"Audio OK — RMS {rms:.1f} dB, peak {peak:.1f} dB, LUFS {lufs}"


# ─── Snapshot helpers ───

SNAPSHOT_FORMAT_VERSION = 1


def build_snapshot_dict(session_devices, params_fetcher):
    """Build a snapshot dict from session devices.
    params_fetcher: callable(track_idx, device_idx) -> {param_name: {index, value}}"""
    snap = {"_ts": time.time(), "_count": 0, "_version": SNAPSHOT_FORMAT_VERSION, "params": {}}
    for d in session_devices:
        params = params_fetcher(d["track_idx"], d["device_idx"])
        for pname, pinfo in params.items():
            key = f"{d['track_name']}/{d['device_name']}/{pname}"
            snap["params"][key] = {
                "track_idx": d["track_idx"],
                "device_idx": d["device_idx"],
                "param_index": pinfo["index"],
                "value": pinfo["value"],
            }
            snap["_count"] += 1
    return snap
