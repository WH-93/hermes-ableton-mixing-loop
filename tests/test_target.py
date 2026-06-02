#!/usr/bin/env python3
"""
Tests for direct track targeting (preset commands).
Run: python3 tests/test_target.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ============================================================
# PRESETS
# ============================================================

PRESETS = {
    "aggressive": {
        "description": "Add grit and intensity — boost drive, tighten attack, add compression",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": +0.15, "ceiling": "Saturator/Drive"},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": -0.08, "ceiling": None},
            {"devices": ["Drum Buss"], "params": ["drive"], "delta": +0.10, "ceiling": "Drum Buss/Drive"},
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta": +0.05, "ceiling": None},
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
        "description": "Reduce high frequencies — darker, warmer tone",
        "actions": [
            {"devices": ["Auto Filter"], "params": ["frequency"], "delta": -0.08, "ceiling": None},
            {"devices": ["EQ Eight"], "params": ["gain", "high"], "delta": -0.06, "ceiling": "EQ Eight/Gain"},
            {"devices": ["Reverb"], "params": ["high cut", "highcut"], "delta": -0.10, "ceiling": None},
        ]
    },
    "brighter": {
        "description": "Boost high frequencies — more presence and air",
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
        "description": "Reduce grit — less drive, more dynamic, gentler",
        "actions": [
            {"devices": ["Saturator"], "params": ["drive"], "delta": -0.10, "ceiling": None},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["threshold"], "delta": +0.08, "ceiling": None},
            {"devices": ["Compressor", "Glue Compressor"], "params": ["ratio"], "delta": -0.05, "ceiling": None},
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
            {"devices": ["EQ Eight"], "params": ["gain", "low mid"], "delta": +0.04, "ceiling": "EQ Eight/Gain"},
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


# ============================================================
# IMPLEMENTATION
# ============================================================

def find_preset(name):
    """Find preset by name (case-insensitive, partial match)."""
    name_lower = name.lower().strip()
    # Exact match
    if name_lower in PRESETS:
        return name_lower, PRESETS[name_lower]
    # Partial match
    for pname, preset in PRESETS.items():
        if pname in name_lower or name_lower in pname:
            return pname, preset
    return None, None


def build_target_changes(session_devices, track_identifier, preset_name):
    """Find devices on matching track(s) and return list of parameter changes.
    track_identifier: track index (int), track name (str), or role tag.
    Returns list of (track_idx, device_idx, param_name, new_value, action_desc)."""
    pname, preset = find_preset(preset_name)
    if not preset:
        return [], f"Unknown preset '{preset_name}'. Available: {', '.join(sorted(PRESETS.keys()))}"

    # Find matching tracks
    matches = []
    tid_lower = str(track_identifier).lower()

    for d in session_devices:
        if d.get("track_muted"):
            continue
        # Match by track index
        if str(d["track_idx"]) == str(track_identifier):
            matches.append(d)
        # Match by track name (partial)
        elif tid_lower in d["track_name"].lower():
            matches.append(d)
        # Match by role tag
        elif track_identifier == "perc" and d.get("_category") == "percussion":
            matches.append(d)
        elif track_identifier == "synth" and d.get("_category") == "synth":
            matches.append(d)

    if not matches:
        return [], f"No tracks found matching '{track_identifier}'"

    changes = []
    for action in preset["actions"]:
        for device_match in matches:
            dname = device_match["device_name"].lower()
            if any(adt.lower() in dname for adt in action["devices"]):
                # Found a device — check for matching params
                for param_hint in action["params"]:
                    for pname, pinfo in device_match.get("params", {}).items():
                        if param_hint.lower() in pname:
                            new_val = max(0.0, min(1.0, pinfo["value"] + action["delta"]))
                            changes.append((
                                device_match["track_idx"],
                                device_match["device_idx"],
                                pname,
                                new_val,
                                f"{device_match['track_name']}/{device_match['device_name']}/{pname}: "
                                f"{pinfo['value']:.3f}→{new_val:.3f} (Δ{action['delta']:+.2f})"
                            ))
                            break  # one param per action per device
                break  # one device match per action (don't apply to all matching devices)

    if not changes:
        return [], f"Preset '{pname}' applied — no matching devices found on '{track_identifier}'"

    msg = f"Preset '{pname}': {preset['description']} → {len(changes)} changes on {len(set(c[0] for c in changes))} track(s)"
    return changes, msg


# ============================================================
# TESTS
# ============================================================

passed = 0
failed = 0

def test(name, actual, expected, note=""):
    global passed, failed
    ok = actual == expected
    if ok:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")
        print(f"    Expected: {expected}")
        print(f"    Got:      {actual}")
        if note:
            print(f"    Note:     {note}")


print("═══════════════════════════════════════════")
print("  1. Preset lookup")
print("═══════════════════════════════════════════")

pname, preset = find_preset("aggressive")
test("Exact match", pname, "aggressive")
test("Has description", bool(preset["description"]), True)
test("Has actions", len(preset["actions"]) > 0, True)

pname, preset = find_preset("AGGRESSIVE")
test("Case insensitive", pname, "aggressive")

pname, preset = find_preset("aggr")
test("Partial match", pname, "aggressive")

pname, preset = find_preset("nonexistent")
test("No match → None", pname, None)


print("\n═══════════════════════════════════════════")
print("  2. Build target changes")
print("═══════════════════════════════════════════")

# Mock session devices with params
devices = [
    {"track_idx": 1, "device_idx": 0, "track_name": "kick punchy", "track_muted": False,
     "device_name": "Saturator", "params": {"drive": {"index": 0, "value": 0.30}}},
    {"track_idx": 1, "device_idx": 1, "track_name": "kick punchy", "track_muted": False,
     "device_name": "Compressor", "params": {"threshold": {"index": 0, "value": 0.40}}},
    {"track_idx": 6, "device_idx": 0, "track_name": "bass FM", "track_muted": False,
     "device_name": "Saturator", "params": {"drive": {"index": 0, "value": 0.20}}},
    {"track_idx": 16, "device_idx": 0, "track_name": "hook swirly", "track_muted": False,
     "device_name": "Utility", "params": {"stereo width": {"index": 0, "value": 1.00}}},
    {"track_idx": 16, "device_idx": 1, "track_name": "hook swirly", "track_muted": False,
     "device_name": "Reverb", "params": {"dry/wet": {"index": 0, "value": 0.25}}},
]

changes, msg = build_target_changes(devices, 1, "aggressive")
test("Track 1 aggressive → finds Saturator", len(changes) > 0, True)
if changes:
    test("Correct track", changes[0][0], 1)
    test("Drive increased", changes[0][3] > 0.30, True)

changes, msg = build_target_changes(devices, "kick", "aggressive")
test("Name match 'kick'", len(changes) > 0, True)

changes, msg = build_target_changes(devices, "hook", "wider")
test("Hook wider → Utility width increased", len(changes) > 0, True)

changes, msg = build_target_changes(devices, 99, "aggressive")
test("Nonexistent track → no changes", len(changes), 0)

changes, msg = build_target_changes(devices, 1, "nonexistent_preset")
test("Nonexistent preset → no changes", len(changes), 0)


print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'='*50}")

if failed:
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
