#!/usr/bin/env python3
"""
Hermes ↔ Ableton Mixing Loop — CLI + Raw TCP transport.
Imports shared logic from mixing.py and audio_analyzer.py.

USAGE:
  python3 mix_loop.py capture [duration]  — analyze only (no apply)
  python3 mix_loop.py fix [duration]      — one capture→analyze→apply (raw TCP)
  python3 mix_loop.py loop [n] [duration] — n iterations (raw TCP)
  python3 mix_loop.py target <id> <preset>— apply preset to track (raw TCP)
  python3 mix_loop.py presets             — list available presets
  python3 mix_loop.py roles               — show track role classification
  python3 mix_loop.py scan                — show session device layout
  python3 mix_loop.py snapshot            — save all device params
  python3 mix_loop.py rollback            — restore from last snapshot
  python3 mix_loop.py analyze <file>      — compare file against reference
  python3 mix_loop.py history             — iteration history
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

# ─── Shared library ───
from mixing import (
    GAIN_CEILINGS, GAIN_FLOORS, REDLINE_PEAK_DB, DEADBAND_SIGMAS,
    PROPORTIONAL_GAIN, LOOP_TIME_BUDGET, ITER_TIME_BUDGET,
    ROLE_TO_CATEGORY, CATEGORY_TARGETS, DEVICE_TYPES,
    parse_track_role, get_track_category, category_matches_recommendation,
    find_device, find_param_in_device,
    SMART_RECOMMENDATIONS, PRESETS, find_preset,
    find_biggest_deviation, map_band_to_fix,
    validate_audio_signal, build_snapshot_dict,
)

# ─── Config ───
PYTHON = "/Users/warrenhayes/mlx-env/bin/python"
ANALYZER = os.path.expanduser("~/.hermes/scripts/audio_analyzer.py")
PROFILE = os.path.expanduser("~/.hermes/data/deepspace_reference_profile.json")
HISTORY = os.path.expanduser("~/.hermes/data/mix_loop_history.json")
SNAPSHOT = os.path.expanduser("~/.hermes/data/mix_loop_snapshot.json")
SCAN_CACHE = os.path.expanduser("~/.hermes/data/mix_loop_scan_cache.json")
LP_HOST = "127.0.0.1"
LP_PORT = 9878
MAX_ITERATIONS = 8
CONVERGENCE_STREAK = 2


# ═══════════════════════════════════════════
# RAW TCP TRANSPORT
# ═══════════════════════════════════════════

def lp_call(cmd_type, params=None, timeout=10):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((LP_HOST, LP_PORT))
        msg = json.dumps({"id": f"ml-{int(time.time()*1000)}", "type": cmd_type, "params": params or {}}) + "\n"
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


def fetch_device_params(track_idx, device_idx):
    p = lp_call("get_device_parameters", {"track_index": track_idx, "device_index": device_idx}, timeout=5)
    if not p.get("ok"): return {}
    params = {}
    for param in p["result"].get("parameters", []):
        pname = param.get("name", "").lower()
        params[pname] = {"index": param.get("index"), "value": param.get("value", 0.5)}
    return params


# ═══════════════════════════════════════════
# AUDIO ANALYSIS (subprocess to audio_analyzer.py)
# ═══════════════════════════════════════════

def capture_blackhole(duration=4):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        output_path = tf.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":2",
        "-t", str(duration), "-ar", "22050", "-ac", "2",
        "-c:a", "pcm_s16le", output_path
    ], capture_output=True, timeout=duration + 10)
    return output_path


def analyze_file(filepath):
    r = subprocess.run([PYTHON, ANALYZER, "analyze", filepath], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: return None
    return json.loads(r.stdout)


def compare_file(filepath, profile_path=PROFILE):
    r = subprocess.run([PYTHON, ANALYZER, "compare", filepath, profile_path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: return None
    return json.loads(r.stdout)


# ═══════════════════════════════════════════
# SESSION SCANNING (raw TCP)
# ═══════════════════════════════════════════

def scan_session(fast=True, force=False):
    if not force and os.path.exists(SCAN_CACHE):
        with open(SCAN_CACHE) as f:
            cached = json.load(f)
        age = time.time() - cached.get("_ts", 0)
        if age < 300:
            return cached

    # Try the new batched command first (LivePilot with get_all_device_parameters patch)
    session = lp_call("get_session_info", timeout=5)
    if not session.get("ok"):
        if os.path.exists(SCAN_CACHE):
            with open(SCAN_CACHE) as f: return json.load(f)
        return None

    info = session["result"]
    start = time.time()
    devices = []

    # Fast path: use get_all_device_parameters if available (1 call vs 30+)
    batch = lp_call("get_all_device_parameters", timeout=20)
    if batch.get("ok"):
        for track in batch["result"].get("tracks", []):
            ti = track["track_index"]
            track_name = track["track_name"]
            track_muted = track.get("mute", False)
            for dev in track.get("devices", []):
                entry = {
                    "track_idx": ti, "device_idx": dev["index"],
                    "track_name": track_name, "track_muted": track_muted,
                    "device_name": dev["name"],
                }
                # Always include params from batch — they're free (already read by LOM)
                params = {}
                for p in dev.get("parameters", []):
                    pname = p.get("name", "").lower()
                    params[pname] = {"index": p["index"], "value": p["value"]}
                entry["params"] = params
                devices.append(entry)
    else:
        # Fallback: per-track calls (slow)
        track_count = info.get("track_count", 0)
        for ti in range(track_count):
            t = lp_call("get_track_info", {"track_index": ti}, timeout=4)
            if not t.get("ok"): continue
            tr = t["result"]
            track_name = tr.get("name", f"Track_{ti}")
            track_muted = tr.get("mute", False)
            dev_list = tr.get("devices", [])
            has_interesting = any(
                any(idt.lower() in d.get("name", "").lower() for idt in DEVICE_TYPES)
                for d in dev_list
            )
            if not has_interesting: continue
            for di, dev in enumerate(dev_list):
                dev_name = dev.get("name", f"dev_{di}")
                if not any(idt.lower() in dev_name.lower() for idt in DEVICE_TYPES):
                    continue
                entry = {
                    "track_idx": ti, "device_idx": di,
                    "track_name": track_name, "track_muted": track_muted,
                    "device_name": dev_name,
                }
                if not fast:
                    entry["params"] = fetch_device_params(ti, di)
                devices.append(entry)

    elapsed = time.time() - start
    result = {"devices": devices, "track_count": info.get("track_count"), "tempo": info.get("tempo"), "_ts": time.time()}
    os.makedirs(os.path.dirname(SCAN_CACHE), exist_ok=True)
    with open(SCAN_CACHE, "w") as f:
        json.dump(result, f)
    print(f"  Scanned {len(devices)} devices in {elapsed:.1f}s", file=sys.stderr)
    return result


# ═══════════════════════════════════════════
# LEAN APPLY — no bulk scan, targeted RPM calls only
# ═══════════════════════════════════════════

def get_track_names():
    """Get all track names and mute states. Uses get_all_track_names if available."""
    batch = lp_call("get_all_track_names", timeout=10)
    if batch.get("ok"):
        return batch["result"].get("tracks", [])
    # Fallback
    r = lp_call("get_session_info", timeout=3)
    if not r.get("ok"): return []
    tracks = []
    for ti in range(r["result"]["track_count"]):
        t = lp_call("get_track_info", {"track_index": ti}, timeout=3)
        if t.get("ok"):
            tracks.append({"index": ti, "name": t["result"].get("name","?"), "mute": t["result"].get("mute",False)})
    return tracks



# ─── Device cache (avoids redundant get_track_info calls) ───
_track_cache = {}  # track_idx → {"name": str, "devices": [{"index": int, "name": str}]}
_param_index_cache = {}  # (track_idx, device_idx) → {param_name: param_index}


def _get_track_devices(ti):
    """Get device list for a track, cached after first call."""
    if ti in _track_cache:
        return _track_cache[ti]["devices"]
    t = lp_call("get_track_info", {"track_index": ti}, timeout=4)
    if not t.get("ok"):
        return []
    devices = [{"index": di, "name": dev.get("name", "")}
               for di, dev in enumerate(t["result"].get("devices", []))]
    _track_cache[ti] = {"name": t["result"].get("name", "?"), "devices": devices}
    return devices


def resolve_and_apply(band_issues, track_names, iteration=0, peak_db=None):
    """One-iteration lean loop: find biggest deviation, resolve target with targeted
    LivePilot calls, apply ONE fix. No bulk scan."""
    biggest = find_biggest_deviation(band_issues)
    if not biggest:
        return [f"Iter[{iteration}]: All bands within deadband — nothing to fix"]

    band, direction, sigmas = biggest
    fix, rec_text, _ = map_band_to_fix(band, direction, iteration)
    if not fix:
        return [f"Iter[{iteration}]: No fix mapping for '{band}/{direction}'"]

    prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)
    redline_active = peak_db is not None and peak_db > REDLINE_PEAK_DB

    # 1. Find candidate tracks by role tag (offline, from cached names)
    candidates = []
    for t in track_names:
        if t.get("mute"): continue
        cat = get_track_category(t["name"])
        if cat and category_matches_recommendation(cat, rec_text):
            candidates.append(t)
    if not candidates:
        # Untagged fallback: any track
        candidates = [t for t in track_names if not t.get("mute")]

    # 2. Find device on candidates (cached — only queries each track once ever)
    match = None
    for cand in candidates[:5]:
        ti = cand["index"]
        devices = _get_track_devices(ti)
        for dev in devices:
            if any(dt.lower() in dev["name"].lower() for dt in fix["devices"]):
                match = (ti, dev["index"], dev["name"], cand["name"])
                break
        if match: break

    if not match:
        return [f"Iter[{iteration}]: No device for '{rec_text}'"]

    ti, di, dname, tname = match

    # 3. Compute delta (analysis-driven, level-independent)
    delta = fix["delta_base"] * prop_factor * min(1.0, sigmas / 3.0)

    if delta > 0 and redline_active:
        return [f"Iter[{iteration}]: BLOCKED — redline ({peak_db:.1f} dBFS)"]
    if abs(delta) < 0.005:
        return [f"Iter[{iteration}]: Deadband — delta too small"]

    # 4. Get param index (cached after first read per device)
    cache_key = (ti, di)
    if cache_key in _param_index_cache:
        # Cached: write directly, no read needed
        for cand_name in fix["params"]:
            if cand_name in _param_index_cache[cache_key]:
                pidx = _param_index_cache[cache_key][cand_name]
                # Approximate new_val from midpoint
                new_val = max(0.0, min(1.0, 0.3 + delta))
                r = lp_call("set_device_parameter", {"track_index": ti, "device_index": di, "parameter_index": pidx, "value": new_val})
                status = "OK" if r.get("ok") else "FAIL"
                actual = r.get("result", {}).get("value")
                if actual is not None and abs(actual - new_val) > 0.01:
                    status = "REJECTED"
                    new_val = actual
                ratio_info = f" [{band} {direction}]" if "/" in band else ""
                return [f"Iter[{iteration}]: {band} {direction} ({sigmas:.1f}σ) → {dname}({tname}): Δ{delta:+.3f}→{new_val:.3f} [{status}] (cached)"]
        # Param hint not found in cache — fall through to full read

    # First call or cache miss: read params to build index map
    params = fetch_device_params(ti, di)
    # Build cache
    idx_map = {}
    for pn, pi in params.items():
        idx_map[pn] = pi["index"]
    _param_index_cache[cache_key] = idx_map

    p_match = find_param_in_device(params, fix["params"])
    if not p_match:
        return [f"Iter[{iteration}]: No matching param on {dname}({tname})"]

    pname, pidx, current = p_match
    new_val = current + delta
    ck = fix.get("ceiling")
    if ck and delta > 0 and current >= GAIN_CEILINGS.get(ck, 1.0):
        return [f"Iter[{iteration}]: AT-CEIL[{ti}] {dname}({tname}) {pname}: {current:.3f}"]
    if ck and delta < 0:
        new_val = max(new_val, GAIN_FLOORS.get(ck, 0.0))
    new_val = max(0.0, min(1.0, new_val))
    if abs(new_val - current) < 0.001:
        return [f"Iter[{iteration}]: SKIP {dname}({tname}) {pname}: already at {current:.3f}"]

    r = lp_call("set_device_parameter", {"track_index": ti, "device_index": di, "parameter_index": pidx, "value": new_val})
    status = "OK" if r.get("ok") else "FAIL"
    if status == "OK":
        actual = r.get("result", {}).get("value")
        if actual is not None and abs(actual - new_val) > 0.01:
            status = "REJECTED"
            new_val = actual

    ratio_info = f" [{band} {direction}]" if "/" in band else ""
    return [f"Iter[{iteration}]: {band} {direction} ({sigmas:.1f}σ) → {dname}({tname}) {pname}: {current:.3f}→{new_val:.3f} (Δ{delta:+.3f}){ratio_info} [{status}]"]


# ═══════════════════════════════════════════
# APPLY FUNCTIONS (raw TCP) — legacy, kept for scan-based commands
# ═══════════════════════════════════════════

def apply_greedy_tcp(session_devices, band_issues, iteration=0, peak_db=None):
    """Find biggest deviation and apply ONE fix via raw TCP."""
    biggest = find_biggest_deviation(band_issues)
    if not biggest:
        return [f"Iter[{iteration}]: All bands within deadband — nothing to fix"]

    band, direction, sigmas = biggest
    fix, rec_text, lower_band = map_band_to_fix(band, direction, iteration)
    if not fix:
        return [f"Iter[{iteration}]: No fix mapping for '{band}/{direction}'"]

    prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)
    redline_active = peak_db is not None and peak_db > REDLINE_PEAK_DB

    match = find_device(session_devices, fix["devices"], rec_text=rec_text)
    if not match:
        return [f"Iter[{iteration}]: No device found for '{rec_text}'"]

    ti, di, dname, tname = match
    # Use cached params from scan if available, otherwise fetch
    cached_device = next((d for d in session_devices 
                          if d["track_idx"] == ti and d["device_idx"] == di), None)
    params = cached_device.get("params") if cached_device and cached_device.get("params") else fetch_device_params(ti, di)
    p_match = find_param_in_device(params, fix["params"])
    if not p_match:
        return [f"Iter[{iteration}]: No matching param on {dname}({tname})"]

    pname, pidx, current = p_match
    delta = fix["delta_base"] * prop_factor * min(1.0, sigmas / 3.0)

    if delta > 0 and redline_active:
        return [f"Iter[{iteration}]: BLOCKED — redline ({peak_db:.1f} dBFS)"]

    new_val = current + delta
    ck = fix.get("ceiling")
    if ck and delta > 0 and current >= GAIN_CEILINGS.get(ck, 1.0):
        return [f"Iter[{iteration}]: AT-CEIL[{ti}] {dname}({tname}) {pname}: {current:.3f}"]
    if ck and delta < 0:
        new_val = max(new_val, GAIN_FLOORS.get(ck, 0.0))
    if abs(delta) < 0.005:
        return [f"Iter[{iteration}]: Deadband — {band} delta too small"]

    new_val = max(0.0, min(1.0, new_val))
    r = lp_call("set_device_parameter", {"track_index": ti, "device_index": di, "parameter_index": pidx, "value": new_val})
    status = "OK" if r.get("ok") else "FAIL"

    if status == "OK":
        # set_device_parameter returns the actual value — no separate read needed
        actual = r.get("result", {}).get("value")
        if actual is not None and abs(actual - new_val) > 0.01:
            status = "REJECTED"
            new_val = actual

    ratio_info = f" [{band} {direction}]" if "/" in band else ""
    return [f"Iter[{iteration}]: {band} {direction} ({sigmas:.1f}σ) → {dname}({tname}) {pname}: {current:.3f}→{new_val:.3f} (Δ{delta:+.3f}){ratio_info} [{status}]"]


def apply_target_tcp(session_devices, track_identifier, preset_name):
    """Apply a preset to matching track(s) via raw TCP."""
    pname, preset = find_preset(preset_name)
    if not preset:
        return [], f"Unknown preset '{preset_name}'. Available: {', '.join(sorted(PRESETS.keys()))}"

    tid_lower = str(track_identifier).lower()
    matches = []
    for d in session_devices:
        if d.get("track_muted"): continue
        if str(d["track_idx"]) == str(track_identifier) or tid_lower in d["track_name"].lower():
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
                    for param_name, pinfo in params.items():
                        if ph.lower() in param_name:
                            new_val = max(0.0, min(1.0, pinfo["value"] + action["delta"]))
                            ck = action.get("ceiling")
                            if ck and action["delta"] > 0 and pinfo["value"] >= GAIN_CEILINGS.get(ck, 1.0):
                                results.append(f"AT-CEIL: {dm['track_name']}/{dm['device_name']}/{param_name}")
                                continue
                            r = lp_call("set_device_parameter", {
                                "track_index": dm["track_idx"], "device_index": dm["device_idx"],
                                "parameter_index": pinfo["index"], "value": new_val,
                            }, timeout=5)
                            status = "OK" if r.get("ok") else "FAIL"
                            results.append(f"{status}: {dm['track_name']}/{dm['device_name']}/{param_name}: {pinfo['value']:.3f}→{new_val:.3f} (Δ{action['delta']:+.2f})")
                            break
                break

    if not results:
        return [], f"Preset '{pname}' applied — no matching devices on '{track_identifier}'"
    return results, f"Preset '{pname}': {preset['description']} → {len(results)} changes"


# ═══════════════════════════════════════════
# SNAPSHOT / ROLLBACK (raw TCP)
# ═══════════════════════════════════════════

def take_snapshot(session_devices):
    snap = build_snapshot_dict(session_devices, fetch_device_params)
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with open(SNAPSHOT, "w") as f:
        json.dump(snap, f, indent=2)
    return snap["_count"]


def rollback_snapshot():
    if not os.path.exists(SNAPSHOT):
        return ["No snapshot found. Run 'snapshot' first."]
    with open(SNAPSHOT) as f:
        snap = json.load(f)
    results = []
    restored = 0
    for key, info in snap.get("params", {}).items():
        r = lp_call("set_device_parameter", {
            "track_index": info["track_idx"], "device_index": info["device_idx"],
            "parameter_index": info["param_index"], "value": info["value"],
        }, timeout=5)
        if r.get("ok"): restored += 1
        else: results.append(f"FAIL: {key}")
    age = time.time() - snap.get("_ts", 0)
    results.insert(0, f"Rollback: {restored}/{snap['_count']} params restored (snapshot from {age:.0f}s ago)")
    return results


def auto_snapshot(session_devices):
    if not os.path.exists(SNAPSHOT):
        count = take_snapshot(session_devices)
        print(f"  Auto-snapshot: saved {count} params to {SNAPSHOT}", file=sys.stderr)
        return True
    return False


# ═══════════════════════════════════════════
# LOOP MODE (raw TCP)
# ═══════════════════════════════════════════

def load_history():
    if os.path.exists(HISTORY):
        with open(HISTORY) as f: return json.load(f)
    return {"iterations": [], "started": None}


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w") as f:
        json.dump(history, f, indent=2)


def run_loop(iterations=5, duration=4):
    history = load_history()
    if not history.get("started"):
        history["started"] = datetime.now().isoformat()
    run_id = len(history["iterations"])

    loop_start = time.time()
    results = []
    prev_recs = set()
    streak = 0

    # Lean: get track names once (1s), no bulk device scan
    print(f"Reading track names...", file=sys.stderr)
    track_names = get_track_names()
    if not track_names:
        print("ERROR: Cannot reach LivePilot", file=sys.stderr)
        return json.dumps({"error": "LivePilot not available"})
    tagged = sum(1 for t in track_names if parse_track_role(t.get("name","")))
    print(f"  {len(track_names)} tracks, {tagged} tagged\n", file=sys.stderr)

    for i in range(iterations):
        if time.time() - loop_start > LOOP_TIME_BUDGET:
            print(f"\n  ⏰ Loop time budget ({LOOP_TIME_BUDGET}s) exceeded.", file=sys.stderr)
            results.append({"iteration": i, "stopped": "time_budget"})
            break
        if time.time() - loop_start > LOOP_TIME_BUDGET - ITER_TIME_BUDGET:
            print(f"  ⏰ Not enough time for another iteration.", file=sys.stderr)
            break

        iter_start = time.time()
        report = {"iteration": i, "time": datetime.now().isoformat()}

        print(f"\n═══ Iteration {i+1}/{iterations} ═══", file=sys.stderr)
        print(f"  Capturing {duration}s from BlackHole...", file=sys.stderr)
        audio_path = capture_blackhole(duration)

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

        report.update({
            "peak_db": peak, "lufs": analysis.get("lufs_integrated"),
            "crest": analysis.get("crest_factor_db"), "stereo": analysis.get("stereo_width"),
            "centroid": analysis.get("spectral_centroid_mean"),
            "recommendations": recs, "capture_time": round(time.time() - iter_start, 1),
        })

        if not recs:
            print(f"  ✓ No issues — converged!", file=sys.stderr)
            report["converged"] = True
            results.append(report)
            streak += 1
            if streak >= CONVERGENCE_STREAK: break
        else:
            rec_set = set(r.lower() for r in recs)
            if rec_set == prev_recs:
                streak += 1
                if streak >= CONVERGENCE_STREAK:
                    print(f"  ⚠ Stalled. Stopping.", file=sys.stderr)
                    report["stalled"] = True
            else:
                streak = 0
            prev_recs = rec_set

            # Lean: resolve target and apply with targeted calls
            applied = resolve_and_apply(band_issues, track_names, i, peak)
            report["applied"] = applied
            for line in applied:
                print(f"    {line}", file=sys.stderr)

        report["total_time"] = round(time.time() - iter_start, 1)
        results.append(report)
        os.unlink(audio_path)
        print(f"  ⏱ {report['total_time']}s", file=sys.stderr)

    history["iterations"].append({"run_id": run_id, "total_time": round(time.time() - loop_start, 1), "results": results})
    save_history(history)

    print(f"\n───── LOOP COMPLETE ({time.time() - loop_start:.0f}s) ─────", file=sys.stderr)
    return json.dumps(results, indent=2)


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"

    if cmd == "capture":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        audio_path = capture_blackhole(duration)
        comparison = compare_file(audio_path)
        if not comparison:
            print(json.dumps({"error": "comparison failed"}))
            sys.exit(1)
        print(json.dumps({
            "analysis": comparison.get("analysis", {}),
            "band_issues": comparison.get("band_issues", []),
            "recommendations": comparison.get("recommendations", []),
        }, indent=2))
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

        ok, msg = validate_audio_signal(analysis)
        if not ok:
            print(f"ERROR: {msg}", file=sys.stderr)
            sys.exit(1)

        print(f"Peak: {peak:.1f} dBFS", file=sys.stderr)
        for r in recs:
            print(f"  • {r}", file=sys.stderr)

        if recs:
            track_names = get_track_names()
            results = resolve_and_apply(band_issues, track_names, 0, peak)
            print(json.dumps({"recommendations": recs, "applied": results, "analysis": analysis}, indent=2))

        os.unlink(audio_path)

    elif cmd == "loop":
        iterations = min(int(sys.argv[2]) if len(sys.argv) > 2 else 5, MAX_ITERATIONS)
        duration = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        print(run_loop(iterations, duration))

    elif cmd == "analyze":
        if len(sys.argv) < 3:
            print("Usage: mix_loop.py analyze <file>")
            sys.exit(1)
        print(json.dumps(compare_file(sys.argv[2]), indent=2))

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
        changes, msg = apply_target_tcp(session["devices"], track_id, preset_name)
        print(msg)
        for line in changes:
            print(f"  {line}")

    elif cmd == "presets":
        for name in sorted(PRESETS.keys()):
            print(f"  {name:12s} — {PRESETS[name]['description']}")

    elif cmd == "roles":
        # Use fast batched command if available, fallback to per-track
        batch = lp_call("get_all_track_names", timeout=10)
        if batch.get("ok"):
            tracks = batch["result"].get("tracks", [])
            batch_mode = True
        else:
            r = lp_call("get_session_info", timeout=3)
            if not r.get("ok"):
                print("ERROR: Cannot reach LivePilot")
                sys.exit(1)
            tc = r["result"]["track_count"]
            tracks = []
            for ti in range(tc):
                t = lp_call("get_track_info", {"track_index": ti}, timeout=3)
                if not t.get("ok"): continue
                tracks.append({"index": ti, "name": t["result"].get("name", "?")})
            batch_mode = False

        tagged = untagged = 0
        print(f"{'Idx':4s} {'Track Name':30s} {'Role':10s} {'Category':12s}")
        print("-" * 60)
        for track in tracks:
            ti = track["index"]
            name = track["name"]
            role = parse_track_role(name)
            cat = get_track_category(name) or "-"
            if role: tagged += 1
            else: untagged += 1
            print(f"  [{ti:2d}] {name:30s} {role or '-':10s} {cat:12s}")
        print("-" * 60)
        print(f"  {tagged} tagged, {untagged} untagged")
        if batch_mode:
            print(f"  (used get_all_track_names — fast path)")

    elif cmd == "scan":
        session = scan_session(fast=True)
        if not session:
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        for d in session["devices"]:
            muted = " (MUTED)" if d["track_muted"] else ""
            role = parse_track_role(d["track_name"])
            cat = get_track_category(d["track_name"]) or "-"
            print(f"  [{d['track_idx']:2d}] {d['track_name']}{muted} role={role} [{cat}] → {d['device_name']}")

    elif cmd == "snapshot":
        session = scan_session(fast=True)
        if not session:
            print("ERROR: Cannot reach LivePilot")
            sys.exit(1)
        count = take_snapshot(session["devices"])
        print(f"Snapshot: {count} params → {SNAPSHOT}")

    elif cmd == "rollback":
        for line in rollback_snapshot():
            print(line)

    elif cmd == "history":
        print(json.dumps(load_history(), indent=2))

    elif cmd == "clear-history":
        if os.path.exists(HISTORY):
            os.unlink(HISTORY)
            print("History cleared.")
        else:
            print("No history file.")

    else:
        print(f"Unknown command: {cmd}")
        print("Usage: mix_loop.py [capture|fix|loop|analyze|target|presets|roles|scan|snapshot|rollback|history|clear-history]")
        sys.exit(1)


if __name__ == "__main__":
    main()
