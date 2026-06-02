#!/usr/bin/env python3
"""
Hermes ↔ Ableton Mixing Loop v2

Key fixes from v1:
  - Project-agnostic: discovers devices by type, not hardcoded track indices
  - Gain ceiling + floor per parameter type
  - Red-line protection: block gain increases if peak near 0dBFS
  - Deadband: skip adjustments within tolerance
  - Proportional control: deltas shrink each iteration
  - Loop mode with convergence tracking and iteration history
  - 6s captures at 22050Hz for speed

USAGE:
  python3 mix_loop.py capture [duration]  — analyze only, no apply
  python3 mix_loop.py fix [duration]      — one capture→analyze→apply cycle
  python3 mix_loop.py loop [iters] [dur]  — run N iterations, track convergence
  python3 mix_loop.py analyze <file>      — compare file against profile
  python3 mix_loop.py test                — test with reference track
  python3 mix_loop.py history             — show iteration history
  python3 mix_loop.py clear-history       — reset history
"""

import json
import os
import subprocess
import socket
import sys
import tempfile
import time
from datetime import datetime

# Use mlx-env Python (has numpy, librosa, scipy, soundfile, pyloudnorm)
PYTHON = "/Users/warrenhayes/mlx-env/bin/python"
ANALYZER = os.path.expanduser("~/.hermes/scripts/audio_analyzer.py")
PROFILE = os.path.expanduser("~/.hermes/data/deepspace_reference_profile.json")
HISTORY = os.path.expanduser("~/.hermes/data/mix_loop_history.json")
SNAPSHOT = os.path.expanduser("~/.hermes/data/mix_loop_snapshot.json")
LP_HOST = "127.0.0.1"
LP_PORT = 9878

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
MAX_ITERATIONS = 8
CONVERGENCE_STREAK = 2


def lp_call(cmd_type, params=None, timeout=10):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((LP_HOST, LP_PORT))
        msg = json.dumps({
            "id": f"hml-{int(time.time()*1000)}",
            "type": cmd_type,
            "params": params or {}
        }) + "\n"
        sock.sendall(msg.encode())
        response = b""
        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk: break
                response += chunk
            except socket.timeout: break
        for line in response.decode().strip().split("\n"):
            if line.strip():
                try: return json.loads(line)
                except: pass
        return {"ok": False, "error": "no valid json"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "LivePilot not running"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        sock.close()


def capture_blackhole(duration=6):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        output_path = tf.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":2",
        "-t", str(duration), "-ar", "22050", "-ac", "2",
        "-c:a", "pcm_s16le", output_path
    ], capture_output=True, timeout=duration + 10)
    return output_path


def validate_audio_signal(analysis):
    """Check if capture contains actual audio (not silence).
    Returns (ok: bool, message: str).
    Call before proceeding with fix/loop to prevent running against silence."""
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
            "Reduce master level or track volumes. Loop will block all gain increases."
        )

    return True, f"Audio OK — RMS {rms:.1f} dB, peak {peak:.1f} dB, LUFS {lufs}"


def check_playback_state():
    """Check if Ableton is playing. Returns (playing: bool, message: str)."""
    r = lp_call("get_session_info", timeout=3)
    if not r.get("ok"):
        return None, "Cannot reach LivePilot to check playback state."
    is_playing = r["result"].get("is_playing", False)
    if is_playing:
        return True, "Playback is running."
    else:
        return False, "Playback is STOPPED. Press space in Ableton to start."


def preflight_check(capture_duration=2):
    """Run pre-flight validation before fix/loop.
    Returns (ok: bool, analysis: dict or None, message: str)."""
    # 1. Check playback
    playing, play_msg = check_playback_state()
    print(f"  Playback: {play_msg}", file=sys.stderr)
    if playing is False:
        return False, None, play_msg

    # 2. Quick capture
    print(f"  Capturing {capture_duration}s to validate audio routing...", file=sys.stderr)
    audio_path = capture_blackhole(capture_duration)

    # 3. Analyze
    analysis = analyze_file(audio_path)
    os.unlink(audio_path)

    if not analysis:
        return False, None, "Audio analysis failed — check BlackHole and ffmpeg."

    # 4. Validate signal
    ok, msg = validate_audio_signal(analysis)
    return ok, analysis, msg


def analyze_file(filepath):
    r = subprocess.run(
        [PYTHON, ANALYZER, "analyze", filepath],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0: return None
    return json.loads(r.stdout)


def compare_file(filepath, profile_path=PROFILE):
    r = subprocess.run(
        [PYTHON, ANALYZER, "compare", filepath, profile_path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0: return None
    return json.loads(r.stdout)


# ─── Role-based track targeting ───
# Convention: first word of track name = role tag
# e.g., "kick punchy 808" → role=kick, "bass FM dark" → role=bass

ROLE_TO_CATEGORY = {
    # ─── LOW END — sub/bass adjustments ───
    "kick": "low_end", "kicks": "low_end",
    "bass": "low_end", "basses": "low_end", "bassline": "low_end",
    "sub": "low_end", "subs": "low_end",
    "rumble": "low_end", "rumbles": "low_end",
    "808": "low_end", "909": "low_end",
    "lowend": "low_end", "low": "low_end",
    "thump": "low_end", "weight": "low_end", "body": "low_end",

    # ─── HIGH FREQ — presence/air adjustments ───
    "hats": "hi_freq", "hat": "hi_freq",
    "ride": "hi_freq", "rides": "hi_freq",
    "cymbal": "hi_freq", "cymbals": "hi_freq", "crash": "hi_freq",
    "hihat": "hi_freq", "hihats": "hi_freq", "hh": "hi_freq",
    "openhat": "hi_freq", "closedhat": "hi_freq",
    "shimmer": "hi_freq", "sparkle": "hi_freq",
    "top": "hi_freq", "tops": "hi_freq",

    # ─── SYNTH — midrange/presence/width ───
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

    # ─── PERCUSSION — presence adjustments ───
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

    # ─── SPATIAL — air/width ───
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

    # ─── MIX BUS — master level/compression/dynamics ───
    "group": "mix_bus", "groups": "mix_bus",
    "bus": "mix_bus", "busses": "mix_bus", "buses": "mix_bus",
    "master": "mix_bus", "masters": "mix_bus",
    "mix": "mix_bus",
    "main": "mix_bus",
    "sum": "mix_bus",

    # ─── MID — low-mids/presence ───
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
                "over-compressed", "very dynamic", "more compression",
                "limited dynamic range"],
    "mid": ["low-mids", "presence (2-6khz)"],
}


def parse_track_role(track_name):
    """Extract role tag from first word of track name. Returns role or None."""
    if not track_name:
        return None
    first_word = track_name.strip().split()[0].lower()
    for suffix in ["-", "_", ".", ":"]:
        if first_word.endswith(suffix):
            first_word = first_word[:-1]
    return first_word if first_word in ROLE_TO_CATEGORY else None


def get_track_category(track_name):
    """Get target category for a track based on its role tag."""
    role = parse_track_role(track_name)
    return ROLE_TO_CATEGORY.get(role) if role else None


def category_matches_recommendation(category, rec_text):
    """Does this category target this recommendation text?"""
    if not category or category not in CATEGORY_TARGETS:
        return False
    rec_lower = rec_text.lower()
    return any(target.lower() in rec_lower for target in CATEGORY_TARGETS[category])


# ─── Device discovery (project-agnostic) ───

# Device types we care about for mixing adjustments
INTERESTING_DEVICES = [
    "EQ Eight", "Compressor", "Glue Compressor", "Saturator",
    "Utility", "Drum Buss", "Auto Filter", "Operator",
    "Wavetable", "Analog",
]

SCAN_CACHE = os.path.expanduser("~/.hermes/data/mix_loop_scan_cache.json")


def scan_session(fast=True):
    """Scan session for devices we care about (fast: names only).
    Only scans tracks with interesting device types, skips muted/return tracks.
    Caches to disk for reuse across iterations."""
    session = lp_call("get_session_info", timeout=5)
    if not session.get("ok"):
        # LivePilot not available — try cache
        if os.path.exists(SCAN_CACHE):
            with open(SCAN_CACHE) as f:
                cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            print(f"  (Using cached scan from {age:.0f}s ago — LivePilot unavailable)",
                  file=sys.stderr)
            return cached
        return None

    # Use cache if it's fresh (< 5 min old)
    if os.path.exists(SCAN_CACHE):
        with open(SCAN_CACHE) as f:
            cached = json.load(f)
        age = time.time() - cached.get("_ts", 0)
        if age < 300:  # 5 minutes
            print(f"  (Using cached scan from {age:.0f}s ago)", file=sys.stderr)
            return cached

    # Fresh scan needed

    info = session["result"]
    track_count = info.get("track_count", 0)
    start = time.time()
    devices = []

    for ti in range(track_count):
        t = lp_call("get_track_info", {"track_index": ti}, timeout=4)
        if not t.get("ok"):
            continue
        tr = t["result"]
        track_name = tr.get("name", f"Track_{ti}")
        track_muted = tr.get("mute", False)
        dev_list = tr.get("devices", [])

        # Skip tracks with no interesting devices
        has_interesting = any(
            any(idt.lower() in d.get("name", "").lower() for idt in INTERESTING_DEVICES)
            for d in dev_list
        )
        if not has_interesting:
            continue

        for di, dev in enumerate(dev_list):
            dev_name = dev.get("name", f"dev_{di}")
            # Only keep interesting devices
            if not any(idt.lower() in dev_name.lower() for idt in INTERESTING_DEVICES):
                continue

            entry = {
                "track_idx": ti,
                "device_idx": di,
                "track_name": track_name,
                "track_muted": track_muted,
                "device_name": dev_name,
            }
            if not fast:
                p = lp_call("get_device_parameters", {
                    "track_index": ti, "device_index": di
                }, timeout=5)
                params = {}
                if p.get("ok"):
                    for param in p["result"].get("parameters", []):
                        pname = param.get("name", "").lower()
                        params[pname] = {
                            "index": param.get("index"),
                            "value": param.get("value", 0.5),
                        }
                entry["params"] = params
            devices.append(entry)

    elapsed = time.time() - start
    result = {
        "devices": devices,
        "track_count": track_count,
        "tempo": info.get("tempo"),
        "_ts": time.time(),
    }

    # Cache for reuse
    os.makedirs(os.path.dirname(SCAN_CACHE), exist_ok=True)
    with open(SCAN_CACHE, "w") as f:
        json.dump(result, f)

    print(f"  Scanned {track_count} tracks → {len(devices)} interesting devices ({elapsed:.0f}s)",
          file=sys.stderr)
    return result


def fetch_device_params(track_idx, device_idx):
    """Fetch parameters for a specific device (lazy, single call)."""
    p = lp_call("get_device_parameters", {
        "track_index": track_idx,
        "device_index": device_idx,
    }, timeout=5)
    if not p.get("ok"):
        return {}
    params = {}
    for param in p["result"].get("parameters", []):
        pname = param.get("name", "").lower()
        params[pname] = {
            "index": param.get("index"),
            "value": param.get("value", 0.5),
        }
    return params


# ─── Smart recommendation → device matching ───

def find_device(session_devices, device_types, rec_text=None, exclude_muted=True):
    """Find best device matching device_types, preferring tracks whose role
    category matches the recommendation text.
    - rec_text: the recommendation text (e.g., "Sub frequencies are weak")
    - If rec_text is None: fall back to first match (backwards compatible)
    """
    # Backwards compat: no rec_text → return first match
    if rec_text is None:
        for d in session_devices:
            if exclude_muted and d.get("track_muted"):
                continue
            dname_lower = d["device_name"].lower()
            if any(dt.lower() in dname_lower for dt in device_types):
                return (d["track_idx"], d["device_idx"],
                        d["device_name"], d["track_name"])
        return None

    matches = []
    for d in session_devices:
        if exclude_muted and d.get("track_muted"):
            continue
        dname_lower = d["device_name"].lower()
        if any(dt.lower() in dname_lower for dt in device_types):
            category = get_track_category(d["track_name"])
            if category and category_matches_recommendation(category, rec_text):
                score = 2
            elif category is None:
                score = 1  # untagged — neutral, use as fallback
            else:
                score = 0  # tagged but wrong category
            matches.append((score, d))

    if not matches:
        return None

    matches.sort(key=lambda x: (-x[0], x[1]["track_idx"]))
    best = matches[0][1]
    return (best["track_idx"], best["device_idx"],
            best["device_name"], best["track_name"])


def find_param_in_device(params, param_hints):
    """Find matching parameter in a params dict. Returns (name, index, value) or None."""
    for pname_hint in param_hints:
        for pname, pinfo in params.items():
            if pname_hint.lower() in pname:
                return (pname, pinfo["index"], pinfo["value"])
    return None


# Maps recommendation keywords → device types to search for and param adjustments
# Now project-agnostic: searches session for matching device types

SMART_RECOMMENDATIONS = [
    {
        "match": ["sub frequencies", "sub is hot", "sub is overwhelming"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain", "low"], "delta_base": -0.08, "ceiling": "EQ Eight/Gain"},
            {"devices": ["Drum Buss"], "params": ["boom"], "delta_base": -0.04, "ceiling": "Drum Buss/Boom"},
        ]
    },
    {
        "match": ["sub frequencies", "sub is weak"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.06, "ceiling": "EQ Eight/Gain"},
            {"devices": ["Drum Buss"], "params": ["boom"], "delta_base": 0.03, "ceiling": "Drum Buss/Boom"},
        ]
    },
    {
        "match": ["bass (60-120hz) is hot"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain", "level"], "delta_base": -0.05, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["bass (60-120hz) is weak"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.05, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["low-mids", "muddy"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": -0.05, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["thin"],
        "fix": [
            {"devices": ["Saturator"], "params": ["drive"], "delta_base": 0.03, "ceiling": "Saturator/Drive"},
        ]
    },
    {
        "match": ["presence (2-6khz) is harsh"],
        "fix": [
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta_base": -0.03, "ceiling": None},
            {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta_base": -0.06, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["presence (2-6khz) is weak", "presence is dull"],
        "fix": [
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta_base": 0.03, "ceiling": None},
            {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.04, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["air (6-16khz) is harsh"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain", "high", "freq"], "delta_base": -0.04, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["air (6-16khz) is weak", "air is missing"],
        "fix": [
            {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.04, "ceiling": "EQ Eight/Gain"},
        ]
    },
    {
        "match": ["over-compressed", "over compressed"],
        "fix": [
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta_base": 0.03, "ceiling": None},
        ]
    },
    {
        "match": ["very dynamic", "more compression"],
        "fix": [
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta_base": -0.02, "ceiling": None},
        ]
    },
    {
        "match": ["narrow", "widen"],
        "fix": [
            {"devices": ["Utility"], "params": ["width"], "delta_base": 0.03, "ceiling": None},
        ]
    },
    {
        "match": ["master is too quiet", "raise master"],
        "fix": [
            {"devices": ["Utility"], "params": ["gain"], "delta_base": 0.03, "ceiling": "Utility/Gain"},
        ]
    },
    {
        "match": ["master is loud", "reduce master"],
        "fix": [
            {"devices": ["Utility"], "params": ["gain"], "delta_base": -0.03, "ceiling": None},
        ]
    },
]


def apply_smart(session_devices, recommendations, iteration=0, peak_db=None):
    """Apply recommendations by scanning session for matching device types.
    Uses lazy parameter fetching — only fetches params for matched devices.
    Deduplicates: multiple fixes targeting same device fetch params once."""
    results = []
    prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)
    redline_active = peak_db is not None and peak_db > REDLINE_PEAK_DB

    applied, ceiling, redline, deadband, unmapped = 0, 0, 0, 0, 0
    param_cache = {}  # (track_idx, device_idx) → params dict

    def get_cached_params(ti, di):
        key = (ti, di)
        if key not in param_cache:
            param_cache[key] = fetch_device_params(ti, di)
        return param_cache[key]

    for rec_text in recommendations:
        rec_lower = rec_text.lower()
        found = False

        for smart in SMART_RECOMMENDATIONS:
            if not any(m.lower() in rec_lower for m in smart["match"]):
                continue
            found = True

            for fix in smart["fix"]:
                match = find_device(
                    session_devices,
                    fix["devices"],
                    rec_text=rec_text,
                    exclude_muted=True
                )
                if not match:
                    continue

                ti, di, dname, tname = match

                # Lazy: fetch params only for this device (cached)
                params = get_cached_params(ti, di)
                p_match = find_param_in_device(params, fix["params"])
                if not p_match:
                    continue

                pname, pidx, current = p_match
                delta = fix["delta_base"] * prop_factor

                # Red-line: block gain increases
                if delta > 0 and redline_active:
                    redline += 1
                    results.append(f"BLOCKED[{ti}] {dname}({tname}): redline {peak_db:.1f}dB")
                    continue

                new_val = current + delta

                # Ceiling
                ck = fix.get("ceiling")
                if ck and delta > 0 and current >= GAIN_CEILINGS.get(ck, 1.0):
                    ceiling += 1
                    results.append(f"AT-CEIL[{ti}] {dname}({tname}) {pname}: {current:.3f}")
                    continue

                # Floor
                if ck and delta < 0:
                    new_val = max(new_val, GAIN_FLOORS.get(ck, 0.0))

                # Deadband
                if abs(delta) < 0.005:
                    deadband += 1
                    continue

                new_val = max(0.0, min(1.0, new_val))

                r = lp_call("set_device_parameter", {
                    "track_index": ti,
                    "device_index": di,
                    "parameter_index": pidx,
                    "value": new_val,
                })

                status = "OK" if r.get("ok") else "FAIL"
                applied += 1
                results.append(
                    f"{status}[{ti}] {dname}({tname}) "
                    f"{pname}: {current:.3f}→{new_val:.3f} (Δ{delta:+.3f})"
                )
            break

        if not found:
            unmapped += 1
            results.append(f"UNMAPPED: {rec_text}")

    summary = (
        f"Iter[{iteration}]: {applied} applied, {deadband} deadband, "
        f"{ceiling} at-ceiling, {redline} redlined, {unmapped} unmapped"
    )
    return [summary] + results


# ─── Greedy single-shot optimization ───
# Instead of applying 7+ recommendations that fight each other,
# find the SINGLE biggest band deviation and apply ONE fix per iteration.
# This eliminates oscillation and reduces LivePilot calls from 7×3s to 1×3s.

def find_biggest_deviation(band_issues):
    """From per-band issues, return the single biggest deviation.
    Returns (band_name, direction, sigmas) or None if all within deadband."""
    if not band_issues:
        return None
    significant = [b for b in band_issues if b.get("sigmas", 0) > 0.8]
    if not significant:
        return None
    biggest = max(significant, key=lambda b: b["sigmas"])
    return (biggest["band"], biggest["direction"], biggest["sigmas"])


def apply_greedy(session_devices, band_issues, iteration=0, peak_db=None):
    """Find the biggest band deviation and apply ONE parameter fix.
    Returns [summary_line, result_line] or [summary_line] if nothing to fix."""
    biggest = find_biggest_deviation(band_issues)
    if not biggest:
        return [f"Iter[{iteration}]: All bands within deadband — nothing to fix"]

    band, direction, sigmas = biggest
    prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)
    redline_active = peak_db is not None and peak_db > REDLINE_PEAK_DB

    # For ratio issues ("sub/bass"), map to the lower band's weak/hot
    if "/" in band:
        lower_band = band.split("/")[0]
        band_direction = "hot" if direction == "wide" else "weak"
    else:
        lower_band = band
        band_direction = direction

    # Map band → recommendation text for device matching
    band_to_rec = {
        "sub": "sub frequencies" if band_direction == "weak" else "sub is hot",
        "bass": "bass (60-120hz) is weak" if band_direction == "weak" else "bass (60-120hz) is hot",
        "low_mid": "low-mids are thin" if band_direction == "weak" else "low-mids are muddy",
        "mid": "mid range is weak" if band_direction == "weak" else "mid range is hot",
        "high_mid": "high-mids are weak" if band_direction == "weak" else "high-mids are hot",
        "presence": "presence is dull" if band_direction == "weak" else "presence is harsh",
        "air": "air is missing" if band_direction == "weak" else "air is harsh",
    }
    rec_text = band_to_rec.get(lower_band)
    if not rec_text:
        return [f"Iter[{iteration}]: Unknown band '{band}' — cannot fix"]

    # Find matching recommendation and first fix action
    fix = None
    for smart in SMART_RECOMMENDATIONS:
        if any(m.lower() in rec_text.lower() for m in smart["match"]):
            fix = smart["fix"][0]  # greedy: first fix action only
            break

    if not fix:
        return [f"Iter[{iteration}]: No fix mapping for '{rec_text}'"]

    # Find device — role-targeted
    match = find_device(session_devices, fix["devices"], rec_text=rec_text)
    if not match:
        return [f"Iter[{iteration}]: No device found for '{rec_text}'"]

    ti, di, dname, tname = match
    params = fetch_device_params(ti, di)
    p_match = find_param_in_device(params, fix["params"])
    if not p_match:
        return [f"Iter[{iteration}]: No matching param on {dname}({tname})"]

    pname, pidx, current = p_match
    delta = fix["delta_base"] * prop_factor * min(1.0, sigmas / 3.0)

    # Red-line
    if delta > 0 and redline_active:
        return [f"Iter[{iteration}]: BLOCKED — redline ({peak_db:.1f} dBFS)"]

    new_val = current + delta

    # Ceiling
    ck = fix.get("ceiling")
    if ck and delta > 0 and current >= GAIN_CEILINGS.get(ck, 1.0):
        return [f"Iter[{iteration}]: AT-CEIL[{ti}] {dname}({tname}) {pname}: {current:.3f}"]

    if ck and delta < 0:
        new_val = max(new_val, GAIN_FLOORS.get(ck, 0.0))

    # Deadband
    if abs(delta) < 0.005:
        return [f"Iter[{iteration}]: Deadband — {band} delta too small ({delta:+.4f})"]

    new_val = max(0.0, min(1.0, new_val))

    r = lp_call("set_device_parameter", {
        "track_index": ti, "device_index": di,
        "parameter_index": pidx, "value": new_val,
    })

    status = "OK" if r.get("ok") else "FAIL"

    # NASA Rule 7: verify the value actually changed
    if status == "OK":
        verify_params = fetch_device_params(ti, di)
        v_match = find_param_in_device(verify_params, [pname])
        if v_match:
            _, _, actual_val = v_match
            if abs(actual_val - new_val) > 0.01:
                status = "REJECTED"
                new_val = actual_val

    ratio_info = f" [{band} {direction}]" if "/" in band else ""
    return [
        f"Iter[{iteration}]: {band} {direction} ({sigmas:.1f}σ) → "
        f"{dname}({tname}) {pname}: {current:.3f}→{new_val:.3f} "
        f"(Δ{delta:+.3f}){ratio_info} [{status}]",
    ]


# ─── Direct track targeting (preset commands) ───

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
    """Find preset by name (case-insensitive, partial match). Returns (name, preset) or (None, None)."""
    name_lower = name.lower().strip()
    if name_lower in PRESETS:
        return name_lower, PRESETS[name_lower]
    for pname, preset in PRESETS.items():
        if pname in name_lower or name_lower in pname:
            return pname, preset
    return None, None


def apply_target(session_devices, track_identifier, preset_name):
    """Apply a preset to matching track(s). Returns (changes_list, message)."""
    pname, preset = find_preset(preset_name)
    if not preset:
        return [], f"Unknown preset '{preset_name}'. Available: {', '.join(sorted(PRESETS.keys()))}"

    # Find matching tracks by index, name, or role
    tid_lower = str(track_identifier).lower()
    matches = []
    for d in session_devices:
        if d.get("track_muted"):
            continue
        if str(d["track_idx"]) == str(track_identifier):
            matches.append(d)
        elif tid_lower in d["track_name"].lower():
            matches.append(d)

    if not matches:
        return [], f"No tracks found matching '{track_identifier}'"

    results = []
    for action in preset["actions"]:
        for dm in matches:
            dname = dm["device_name"].lower()
            if any(adt.lower() in dname for adt in action["devices"]):
                params = fetch_device_params(dm["track_idx"], dm["device_idx"])
                for ph in action["params"]:
                    for pname, pinfo in params.items():
                        if ph.lower() in pname:
                            new_val = max(0.0, min(1.0, pinfo["value"] + action["delta"]))
                            ck = action.get("ceiling")
                            if ck and action["delta"] > 0 and pinfo["value"] >= GAIN_CEILINGS.get(ck, 1.0):
                                results.append(f"AT-CEIL: {dm['track_name']}/{dm['device_name']}/{pname}")
                                continue
                            r = lp_call("set_device_parameter", {
                                "track_index": dm["track_idx"],
                                "device_index": dm["device_idx"],
                                "parameter_index": pinfo["index"],
                                "value": new_val,
                            }, timeout=5)
                            status = "OK" if r.get("ok") else "FAIL"
                            results.append(
                                f"{status}: {dm['track_name']}/{dm['device_name']}/{pname}: "
                                f"{pinfo['value']:.3f}→{new_val:.3f} (Δ{action['delta']:+.2f})"
                            )
                            break  # one param per action per device
                break  # one device per action

    if not results:
        return [], f"Preset '{pname}' applied — no matching devices on '{track_identifier}'"

    msg = f"Preset '{pname}': {preset['description']} → {len(results)} changes"
    return results, msg


# ─── Loop mode ───

# NASA Rule 2: hard time budgets — every operation has bounded completion
LOOP_TIME_BUDGET = 120   # total seconds for entire loop
ITER_TIME_BUDGET = 20    # per-iteration max (capture + analyze + apply)


def load_history():
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            return json.load(f)
    return {"iterations": [], "started": None}


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w") as f:
        json.dump(history, f, indent=2)


def take_snapshot(session_devices):
    """Save all device parameter values to JSON. Returns count of params saved."""
    snap = {"_ts": time.time(), "_count": 0, "params": {}}
    for d in session_devices:
        params = fetch_device_params(d["track_idx"], d["device_idx"])
        for pname, pinfo in params.items():
            key = f"{d['track_name']}/{d['device_name']}/{pname}"
            snap["params"][key] = {
                "track_idx": d["track_idx"],
                "device_idx": d["device_idx"],
                "param_index": pinfo["index"],
                "value": pinfo["value"],
            }
            snap["_count"] += 1
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w") as f:
        json.dump(snap, f, indent=2)
    return snap["_count"]


def rollback_snapshot():
    """Restore all parameters from last snapshot. Returns list of changes."""
    if not os.path.exists(SNAPSHOT):
        return ["No snapshot found. Run 'snapshot' first."]

    with open(SNAPSHOT) as f:
        snap = json.load(f)

    results = []
    restored = 0
    for key, info in snap.get("params", {}).items():
        r = lp_call("set_device_parameter", {
            "track_index": info["track_idx"],
            "device_index": info["device_idx"],
            "parameter_index": info["param_index"],
            "value": info["value"],
        }, timeout=5)
        if r.get("ok"):
            restored += 1
        else:
            results.append(f"FAIL: {key}")

    age = time.time() - snap.get("_ts", 0)
    results.insert(0, f"Rollback: {restored}/{snap['_count']} params restored "
                   f"(snapshot from {age:.0f}s ago)")
    return results


def auto_snapshot(session_devices):
    """Take snapshot before fix/loop if none exists. Returns True if new snapshot taken."""
    if not os.path.exists(SNAPSHOT):
        count = take_snapshot(session_devices)
        print(f"  Auto-snapshot: saved {count} params to {SNAPSHOT}", file=sys.stderr)
        return True
    return False


def run_loop(iterations=5, duration=4):
    """Run capture→analyze→apply loop with convergence tracking.
    NASA Rule 2: hard time budgets prevent unbounded execution.
    NASA Rule 3: scan frozen at loop start — no re-scanning mid-loop."""
    history = load_history()
    if not history.get("started"):
        history["started"] = datetime.now().isoformat()
    run_id = len(history["iterations"])

    loop_start = time.time()
    results = []
    prev_recs = set()
    streak = 0

    # Frozen scan — do it once, reuse across all iterations
    print(f"Scanning session once (frozen for entire loop)...", file=sys.stderr)
    session = scan_session(fast=True)
    if not session:
        print("ERROR: Cannot reach LivePilot", file=sys.stderr)
        return json.dumps({"error": "LivePilot not available"})
    session_devices = session["devices"]
    print(f"  {len(session_devices)} devices frozen across {session['track_count']} tracks\n",
          file=sys.stderr)

    auto_snapshot(session_devices)

    for i in range(iterations):
        # NASA Rule 2: total time budget
        if time.time() - loop_start > LOOP_TIME_BUDGET:
            print(f"\n  ⏰ Loop time budget ({LOOP_TIME_BUDGET}s) exceeded. Stopping gracefully.",
                  file=sys.stderr)
            results.append({"iteration": i, "stopped": "time_budget", "time": datetime.now().isoformat()})
            break

        iter_start = time.time()
        report = {"iteration": i, "time": datetime.now().isoformat()}

        print(f"\n═══ Iteration {i+1}/{iterations} ═══", file=sys.stderr)

        # NASA Rule 2: per-iteration time budget
        # Don't start a capture if we're already close to the iter budget
        if time.time() - loop_start > LOOP_TIME_BUDGET - ITER_TIME_BUDGET:
            print(f"  ⏰ Not enough time remaining for another iteration. Stopping.",
                  file=sys.stderr)
            break

        # 1. Capture
        print(f"  Capturing {duration}s from BlackHole...", file=sys.stderr)
        audio_path = capture_blackhole(duration)

        # 2. Analyze + compare
        print(f"  Analyzing...", file=sys.stderr)
        comparison = compare_file(audio_path)
        if not comparison:
            report["error"] = "comparison failed"
            results.append(report)
            os.unlink(audio_path)
            break

        analysis = comparison.get("analysis", {})
        recs = comparison.get("recommendations", [])
        peak = analysis.get("peak_db")
        band_issues = comparison.get("band_issues", [])

        report["peak_db"] = peak
        report["lufs"] = analysis.get("lufs_integrated")
        report["crest"] = analysis.get("crest_factor_db")
        report["stereo"] = analysis.get("stereo_width")
        report["centroid"] = analysis.get("spectral_centroid_mean")
        report["recommendations"] = recs
        report["capture_time"] = round(time.time() - iter_start, 1)

        print(f"  Peak: {peak:.1f}dB  LUFS: {analysis.get('lufs_integrated')}  "
              f"Crest: {analysis.get('crest_factor_db')}dB  Stereo: {analysis.get('stereo_width')}",
              file=sys.stderr)

        if not recs:
            print(f"  ✓ No issues — converged!", file=sys.stderr)
            report["converged"] = True
            results.append(report)
            streak += 1
            if streak >= CONVERGENCE_STREAK:
                break
        else:
            print(f"  Issues: {len(recs)}", file=sys.stderr)
            for r in recs:
                print(f"    • {r}", file=sys.stderr)

            # Check convergence
            rec_set = set(r.lower() for r in recs)
            if rec_set == prev_recs:
                streak += 1
                print(f"  Same recommendations ({streak} streak)", file=sys.stderr)
                if streak >= CONVERGENCE_STREAK:
                    print(f"  ⚠ Stalled. Stopping.", file=sys.stderr)
                    report["stalled"] = True
            else:
                streak = 0
            prev_recs = rec_set

            # 3. Apply (using frozen scan — no re-scanning)
            print(f"  Greedy single-shot...", file=sys.stderr)
            applied = apply_greedy(session_devices, band_issues, i, peak)
            report["applied"] = applied
            for line in applied:
                print(f"    {line}", file=sys.stderr)

        report["total_time"] = round(time.time() - iter_start, 1)
        results.append(report)
        os.unlink(audio_path)

        # NASA Rule 2: per-iteration budget check
        iter_elapsed = time.time() - iter_start
        print(f"  ⏱ {iter_elapsed:.1f}s", file=sys.stderr)
        if iter_elapsed > ITER_TIME_BUDGET:
            print(f"  ⚠ Iteration exceeded budget ({ITER_TIME_BUDGET}s). Continuing.",
                  file=sys.stderr)

    # Save history
    history["iterations"].append({
        "run_id": run_id,
        "total_time": round(time.time() - loop_start, 1),
        "results": results,
    })
    save_history(history)

    # Final report
    loop_elapsed = time.time() - loop_start
    print(f"\n───── LOOP COMPLETE ({loop_elapsed:.0f}s) ─────", file=sys.stderr)
    print(f"  Iterations: {len(results)}", file=sys.stderr)
    if len(results) >= 2:
        first = results[0]
        last = results[-1]
        if first.get("peak_db") and last.get("peak_db"):
            print(f"  Peak: {first['peak_db']:.1f} → {last['peak_db']:.1f} dB", file=sys.stderr)
        if first.get("crest") and last.get("crest"):
            print(f"  Crest: {first['crest']} → {last['crest']} dB", file=sys.stderr)
        if first.get("stereo") is not None and last.get("stereo") is not None:
            print(f"  Stereo: {first['stereo']} → {last['stereo']}", file=sys.stderr)
        final_recs = last.get("recommendations", [])
        print(f"  Remaining issues: {len(final_recs)}", file=sys.stderr)
    print(f"  History: {HISTORY}", file=sys.stderr)

    return json.dumps(results, indent=2)


# ─── Main ───

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"

    if cmd == "capture":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        print(f"Capturing {duration}s from BlackHole 2ch...", file=sys.stderr)
        audio_path = capture_blackhole(duration)

        comparison = compare_file(audio_path)
        if not comparison:
            print(json.dumps({"error": "comparison failed"}))
            sys.exit(1)

        output = {
            "file": audio_path,
            "analysis": comparison.get("analysis", {}),
            "issues": comparison.get("issues", []),
            "recommendations": comparison.get("recommendations", []),
        }
        print(json.dumps(output, indent=2))
        os.unlink(audio_path)

    elif cmd == "fix":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        print(f"Capturing {duration}s from BlackHole...", file=sys.stderr)
        audio_path = capture_blackhole(duration)

        comparison = compare_file(audio_path)
        if not comparison:
            print(json.dumps({"error": "comparison failed"}))
            sys.exit(1)

        analysis = comparison.get("analysis", {})
        band_issues = comparison.get("band_issues", [])
        recs = comparison.get("recommendations", [])
        peak = analysis.get("peak_db")

        # Quick silence check (NASA Rule 7: check every return)
        if analysis.get("rms_db", -200) < -80:
            print("ERROR: No audio detected. Check BlackHole routing and playback.", file=sys.stderr)
            sys.exit(1)

        print(f"Peak: {peak:.1f} dBFS", file=sys.stderr)
        print(f"Recommendations ({len(recs)}):", file=sys.stderr)
        for r in recs:
            print(f"  • {r}", file=sys.stderr)

        if recs:
            print(f"\nScanning session (fast)...", file=sys.stderr)
            session = scan_session(fast=True)
            if session:
                auto_snapshot(session["devices"])
                print(f"Found {len(session['devices'])} devices on {session['track_count']} tracks", file=sys.stderr)
                results = apply_greedy(session["devices"], band_issues, 0, peak)
            else:
                results = ["ERROR: LivePilot not connected"]
            print(json.dumps({
                "recommendations": recs,
                "applied": results,
                "analysis": analysis,
            }, indent=2))

        os.unlink(audio_path)

    elif cmd == "loop":
        iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        iterations = min(iterations, MAX_ITERATIONS)
        duration = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        print(run_loop(iterations, duration))

    elif cmd == "analyze":
        if len(sys.argv) < 3:
            print("Usage: mix_loop.py analyze <file>")
            sys.exit(1)
        comparison = compare_file(sys.argv[2])
        print(json.dumps(comparison, indent=2))

    elif cmd == "test":
        ref_dir = os.path.expanduser("~/Desktop/deepspace reference tracks")
        files = sorted([
            f for f in os.listdir(ref_dir)
            if f.endswith(('.mp3', '.flac', '.aiff', '.aif', '.wav'))
            and not f.startswith('.')
        ])
        if files:
            test_file = os.path.join(ref_dir, files[0])
            print(f"Testing: {files[0]}", file=sys.stderr)
            analysis = analyze_file(test_file)
            print(json.dumps(analysis, indent=2))

    elif cmd == "scan":
        """Show all devices in current session with role classification."""
        session = scan_session(fast=True)
        if not session:
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        print(f"Tempo: {session['tempo']} BPM, {session['track_count']} tracks, {len(session['devices'])} devices\n")
        for d in session["devices"]:
            muted = " (MUTED)" if d["track_muted"] else ""
            role = parse_track_role(d["track_name"])
            cat = get_track_category(d["track_name"]) or "-"
            role_str = f" role={role}" if role else ""
            print(f"  [{d['track_idx']:2d}] {d['track_name']}{muted}{role_str} [{cat}] → {d['device_name']}")

    elif cmd == "roles":
        """Show all tracks with their role classification."""
        r = lp_call("get_session_info", timeout=3)
        if not r.get("ok"):
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        tc = r["result"]["track_count"]
        print(f"{'Idx':4s} {'Track Name':30s} {'Role':10s} {'Category':12s} {'Tagged?'}")
        print("-" * 70)
        tagged = untagged = 0
        for ti in range(tc):
            t = lp_call("get_track_info", {"track_index": ti}, timeout=3)
            if not t.get("ok"):
                continue
            name = t["result"].get("name", "?")
            role = parse_track_role(name)
            cat = get_track_category(name) or "-"
            status = "✓" if role else "✗ (add role prefix)"
            if role:
                tagged += 1
            else:
                untagged += 1
            print(f"  [{ti:2d}] {name:30s} {role or '-':10s} {cat:12s} {status}")
        print("-" * 70)
        print(f"  {tagged} tagged, {untagged} untagged")
        if untagged > 0:
            print("\n  Convention: first word of track name = role tag.")
            print("  Valid roles: " + ", ".join(sorted(ROLE_TO_CATEGORY.keys())))

    elif cmd == "history":
        history = load_history()
        print(json.dumps(history, indent=2))

    elif cmd == "clear-history":
        if os.path.exists(HISTORY):
            os.unlink(HISTORY)
            print("History cleared.")
        else:
            print("No history file.")

    elif cmd == "snapshot":
        session = scan_session(fast=True)
        if not session:
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        count = take_snapshot(session["devices"])
        print(f"Snapshot saved: {count} parameters from {len(session['devices'])} devices → {SNAPSHOT}")

    elif cmd == "rollback":
        results = rollback_snapshot()
        for line in results:
            print(line)

    elif cmd == "target":
        if len(sys.argv) < 3:
            print("Usage: mix_loop.py target <track_id|name> <preset>")
            print("Presets: " + ", ".join(sorted(PRESETS.keys())))
            sys.exit(1)
        track_id = sys.argv[2]
        preset_name = sys.argv[3] if len(sys.argv) > 3 else "aggressive"
        session = scan_session(fast=True)
        if not session:
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        auto_snapshot(session["devices"])
        changes, msg = apply_target(session["devices"], track_id, preset_name)
        print(msg)
        for line in changes:
            print(f"  {line}")

    elif cmd == "presets":
        for name in sorted(PRESETS.keys()):
            p = PRESETS[name]
            print(f"  {name:12s} — {p['description']}")

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: mix_loop.py [capture|fix|loop|analyze|test|scan|roles|snapshot|rollback|target|presets|history|clear-history]")
        sys.exit(1)


if __name__ == "__main__":
    main()
