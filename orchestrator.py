#!/usr/bin/env python3
"""
Hermes Orchestrator — MCP transport for Ableton mixing.

Imports mixing.py (shared logic) and audio_analyzer.py (analysis engine).
Designed to be called FROM Hermes context, where MCP tools are available.

USAGE (from Hermes):
  from orchestrator import MixOrchestrator
  orch = MixOrchestrator()
  
  # Run capture + analyze (subprocess)
  result = orch.analyze()                    # → {"band_issues": [...], ...}
  
  # Find what to fix (no Ableton calls)
  plan = orch.plan_fix(result["band_issues"]) # → {"band": "sub", "action": "reduce", ...}
  
  # Hermes executes the plan via MCP tools:
  #   orch.get_target_device(plan, track_names_from_mcp)
  #   mcp__ableton__set_device_parameter(...)

Does NOT contain:
  - Any transport (no TCP, no MCP calls) — Hermes does the actual calls
  - Any CLI — this is a library
"""

import json
import os
import subprocess
import tempfile
import time

from mixing import (
    GAIN_CEILINGS, GAIN_FLOORS, REDLINE_PEAK_DB, PROPORTIONAL_GAIN,
    find_biggest_deviation, map_band_to_fix,
    find_device, find_param_in_device,
    parse_track_role, get_track_category,
    validate_audio_signal, build_snapshot_dict,
    PRESETS, find_preset,
)

# Config
PYTHON = "/Users/warrenhayes/mlx-env/bin/python"
ANALYZER = os.path.expanduser("~/.hermes/scripts/audio_analyzer.py")
PROFILE = os.path.expanduser("~/.hermes/data/deepspace_reference_profile.json")
SNAPSHOT_PATH = os.path.expanduser("~/.hermes/data/mix_loop_snapshot.json")


class MixOrchestrator:
    """Orchestrates Ableton mixing from Hermes context.
    
    Pattern:
      1. orch.analyze() → get band issues
      2. orch.plan_fix(band_issues) → get action plan
      3. Hermes reads track names via MCP, passes to orch
      4. orch.resolve_target(action, tracks) → exact device/param/value
      5. Hermes calls MCP set_device_parameter with the result
    
    Snapshots are built here, stored on disk, applied by Hermes via MCP.
    """

    def __init__(self, profile_path=PROFILE):
        self.profile_path = profile_path

    # ─── Analysis (subprocess to audio_analyzer.py) ───

    def capture_and_analyze(self, duration=4):
        """Capture audio from BlackHole and run full analysis.
        Returns dict with band_issues, recommendations, analysis.
        Called by Hermes once per loop iteration."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            audio_path = tf.name

        subprocess.run([
            "ffmpeg", "-y", "-f", "avfoundation", "-i", ":2",
            "-t", str(duration), "-ar", "22050", "-ac", "2",
            "-c:a", "pcm_s16le", audio_path,
        ], capture_output=True, timeout=duration + 10)

        r = subprocess.run(
            [PYTHON, ANALYZER, "compare", audio_path, self.profile_path],
            capture_output=True, text=True, timeout=60,
        )
        os.unlink(audio_path)

        if r.returncode != 0:
            return {"error": "analysis failed", "stderr": r.stderr}

        data = json.loads(r.stdout)
        return {
            "analysis": data.get("analysis", {}),
            "band_issues": data.get("band_issues", []),
            "recommendations": data.get("recommendations", []),
        }

    # ─── Validation ───

    def check_audio(self, analysis):
        """Validate audio signal. Returns (ok, message)."""
        return validate_audio_signal(analysis)

    # ─── Planning (no Ableton calls) ───

    def plan_fix(self, band_issues, iteration=0):
        """Find the biggest deviation and return an action plan.
        Returns dict or None if nothing to fix.
        Called by Hermes — no MCP calls yet."""
        biggest = find_biggest_deviation(band_issues)
        if not biggest:
            return None

        band, direction, sigmas = biggest
        fix, rec_text, lower_band = map_band_to_fix(band, direction, iteration)

        if not fix:
            return None

        prop_factor = PROPORTIONAL_GAIN / (1 + iteration * 0.3)

        return {
            "band": band,
            "direction": direction,
            "sigmas": round(sigmas, 1),
            "rec_text": rec_text,
            "device_types": fix["devices"],
            "param_hints": fix["params"],
            "delta_base": fix["delta_base"],
            "delta": round(fix["delta_base"] * prop_factor * min(1.0, sigmas / 3.0), 4),
            "ceiling": fix.get("ceiling"),
        }

    # ─── Target resolution (needs track names from MCP) ───

    def build_device_list(self, track_data):
        """Convert MCP track info into session_devices format.
        track_data: list of {track_idx, track_name, track_muted, devices: [{name}]}
        Returns list of device dicts compatible with find_device()."""
        devices = []
        for track in track_data:
            for dev in track.get("devices", []):
                devices.append({
                    "track_idx": track["track_idx"],
                    "device_idx": dev.get("device_idx", 0),
                    "track_name": track["track_name"],
                    "track_muted": track.get("track_muted", False),
                    "device_name": dev.get("name", ""),
                })
        return devices

    def resolve_target(self, plan, session_devices):
        """Given an action plan and session device list, find the exact device+param+value.
        Returns dict with track_idx, device_idx, param_index, new_value, label.
        Hermes uses this to call MCP set_device_parameter."""
        if not plan:
            return None

        match = find_device(session_devices, plan["device_types"], rec_text=plan["rec_text"])
        if not match:
            return None

        ti, di, dname, tname = match
        return {
            "track_idx": ti,
            "device_idx": di,
            "device_name": dname,
            "track_name": tname,
            "param_hints": plan["param_hints"],
            "delta": plan["delta"],
            "ceiling": plan.get("ceiling"),
            "band": plan["band"],
            "direction": plan["direction"],
            "sigmas": plan["sigmas"],
        }

    def apply_resolved(self, resolved, current_value):
        """Calculate the new parameter value with safety checks.
        Returns (new_value, should_apply, reason).
        Called by Hermes after reading current param value via MCP."""
        if resolved is None:
            return 0.0, False, "No target resolved"

        delta = resolved["delta"]
        new_val = current_value + delta

        ck = resolved.get("ceiling")
        if ck and delta > 0 and current_value >= GAIN_CEILINGS.get(ck, 1.0):
            return current_value, False, f"At ceiling ({ck})"

        if ck and delta < 0:
            new_val = max(new_val, GAIN_FLOORS.get(ck, 0.0))

        if abs(delta) < 0.005:
            return current_value, False, "Deadband — delta too small"

        new_val = max(0.0, min(1.0, new_val))
        return new_val, True, "OK"

    # ─── Snapshots (Hermes reads/writes via MCP) ───

    def build_snapshot(self, session_devices):
        """Build a snapshot dict from session devices.
        Hermes populates params via MCP get_device_parameters, then calls this."""
        return build_snapshot_dict(session_devices, lambda ti, di: {})  # params filled by Hermes

    @staticmethod
    def save_snapshot(snapshot, path=SNAPSHOT_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2)

    @staticmethod
    def load_snapshot(path=SNAPSHOT_PATH):
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    # ─── Presets (same as CLI, returns plan for Hermes to execute) ───

    def plan_preset(self, track_identifier, preset_name, session_devices):
        """Plan a preset application. Returns list of action dicts.
        Hermes executes each via MCP set_device_parameter."""
        pname, preset = find_preset(preset_name)
        if not preset:
            return [], f"Unknown preset. Available: {', '.join(sorted(PRESETS.keys()))}"

        tid_lower = str(track_identifier).lower()
        matches = [d for d in session_devices
                   if not d.get("track_muted")
                   and (str(d["track_idx"]) == str(track_identifier)
                        or tid_lower in d["track_name"].lower())]

        if not matches:
            return [], f"No tracks matching '{track_identifier}'"

        actions = []
        for act in preset["actions"]:
            for dm in matches:
                if any(adt.lower() in dm["device_name"].lower() for adt in act["devices"]):
                    actions.append({
                        "track_idx": dm["track_idx"],
                        "device_idx": dm["device_idx"],
                        "param_hints": act["params"],
                        "delta": act["delta"],
                        "ceiling": act.get("ceiling"),
                        "track_name": dm["track_name"],
                        "device_name": dm["device_name"],
                    })
                    break

        return actions, f"Preset '{pname}': {preset['description']} → {len(actions)} actions"

    # ─── Track roles (same logic as CLI, uses MCP track names) ───

    def classify_tracks(self, track_data):
        """Classify a list of tracks by role tag.
        track_data: list of {track_idx, track_name}
        Returns list of {track_idx, track_name, role, category}."""
        result = []
        for track in track_data:
            role = parse_track_role(track["track_name"])
            category = get_track_category(track["track_name"])
            result.append({
                "track_idx": track["track_idx"],
                "track_name": track["track_name"],
                "role": role,
                "category": category or "-",
                "tagged": role is not None,
            })
        return result


# ─── Convenience: full orchestrated fix cycle ───
# Hermes calls this sequence:
#
#   orch = MixOrchestrator()
#
#   # 1. Analyze
#   result = orch.capture_and_analyze(duration=4)
#   ok, msg = orch.check_audio(result["analysis"])
#   if not ok: return error
#
#   # 2. Plan
#   plan = orch.plan_fix(result["band_issues"], iteration=i)
#   if not plan: return "converged"
#
#   # 3. Build device list from MCP
#   # track_data = [mcp__ableton__get_track_info(ti) for ti in range(N)]
#   session_devices = orch.build_device_list(track_data)
#
#   # 4. Resolve target
#   resolved = orch.resolve_target(plan, session_devices)
#   if not resolved: return "no device found"
#
#   # 5. Read current value via MCP
#   # params = mcp__ableton__get_device_parameters(track_idx, device_idx)
#   # current = find_param_value(params, resolved["param_hints"])
#
#   # 6. Calculate new value
#   new_val, should_apply, reason = orch.apply_resolved(resolved, current)
#   if not should_apply: return reason
#
#   # 7. Apply via MCP
#   # mcp__ableton__set_device_parameter(track_idx, device_idx, param_index, new_val)
#
#   # 8. Verify
#   # params = mcp__ableton__get_device_parameters(track_idx, device_idx)
#   # actual = find_param_value(params, resolved["param_hints"])
#   # if abs(actual - new_val) > 0.01: status = "REJECTED"
#
#   return f"{plan['band']} {plan['direction']} → {resolved['track_name']}/{resolved['device_name']}: {current}→{new_val}"
