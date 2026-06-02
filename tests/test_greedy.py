#!/usr/bin/env python3
"""
Tests for greedy single-shot optimization with batch application.
Run: python3 tests/test_greedy.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ============================================================
# GREEDY ALGORITHM
# Instead of applying all 7-8 recommendations (which fight),
# find the SINGLE biggest deviation and apply ONE fix.
# ============================================================

def find_biggest_deviation(band_issues):
    """From per-band issues, return the single biggest deviation.
    Returns (band_name, direction, sigmas) or None if within deadband."""
    if not band_issues:
        return None
    # Filter out anything within deadband
    significant = [b for b in band_issues if b.get("sigmas", 0) > 0.8]
    if not significant:
        return None
    biggest = max(significant, key=lambda b: b["sigmas"])
    return (biggest["band"], biggest["direction"], biggest["sigmas"])


def build_single_fix(band, direction, sigmas, device_cache, rec_map):
    """Build a single parameter change targeting the biggest deviation.
    Returns (track_idx, dev_idx, param_idx, new_value, label) or None.
    
    Unlike the old apply_smart which generated 7-8 recs and searched for
    all of them, this generates ONE fix for ONE band. Much faster because
    we only need to find ONE matching device instead of 7."""
    
    # Map band name to recommendation keywords
    band_to_rec = {
        "sub": "sub frequencies" if direction == "weak" else "sub is hot",
        "bass": "bass (60-120hz) is weak" if direction == "weak" else "bass (60-120hz) is hot",
        "low_mid": "low-mids are thin" if direction == "weak" else "low-mids are muddy",
        "presence": "presence is dull" if direction == "weak" else "presence is harsh",
        "air": "air is missing" if direction == "weak" else "air is harsh",
    }
    
    rec_text = band_to_rec.get(band)
    if not rec_text:
        return None
    
    # Find the best matching fix for this band
    for smart in rec_map:
        if not any(m.lower() in rec_text.lower() for m in smart["match"]):
            continue
        # Take only the first fix action (greedy: one fix per iteration)
        fix = smart["fix"][0]
        break
    else:
        return None
    
    # Find matching device — already handles role targeting
    for d in device_cache:
        if d.get("track_muted"):
            continue
        dname = d["device_name"].lower()
        if any(dt.lower() in dname for dt in fix["devices"]):
            # Check for matching parameter
            for pname_hint in fix["params"]:
                for pname, pinfo in d.get("params", {}).items():
                    if pname_hint.lower() in pname:
                        # Calculate delta — proportional to deviation
                        delta = fix["delta_base"] * min(1.0, sigmas / 3.0)
                        new_val = max(0.0, min(1.0, pinfo["value"] + delta))
                        return (
                            d["track_idx"], d["device_idx"],
                            pinfo["index"], new_val,
                            f"{d['track_name']}/{d['device_name']}/{pname}: "
                            f"{pinfo['value']:.3f}→{new_val:.3f} "
                            f"({band} {direction}, {sigmas:.1f}σ)"
                        )
    
    return None


# ============================================================
# BATCH APPLICATION
# Instead of calling set_device_parameter N times (N×3s),
# build a batch and call batch_set_parameters once (3s).
# ============================================================

def build_batch_payload(track_idx, device_idx, changes):
    """Build a batch_set_parameters payload from a list of (param_name, value) tuples.
    Returns the params dict ready for LivePilot."""
    return {
        "track_index": track_idx,
        "device_index": device_idx,
        "parameters": [
            {"name_or_index": name, "value": val}
            for name, val in changes
        ]
    }


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
print("  1. find_biggest_deviation")
print("═══════════════════════════════════════════")

# Multiple issues — picks the largest
issues = [
    {"band": "sub", "direction": "weak", "sigmas": 2.1},
    {"band": "presence", "direction": "weak", "sigmas": 4.5},
    {"band": "air", "direction": "weak", "sigmas": 1.2},
]
result = find_biggest_deviation(issues)
test("Picks presence (4.5σ > 2.1σ)", result, ("presence", "weak", 4.5))

# All within deadband — returns None
issues = [
    {"band": "sub", "direction": "weak", "sigmas": 0.5},
    {"band": "air", "direction": "weak", "sigmas": 0.3},
]
result = find_biggest_deviation(issues)
test("All within deadband → None", result, None)

# Empty list
test("Empty list → None", find_biggest_deviation([]), None)

# Single issue
issues = [{"band": "bass", "direction": "hot", "sigmas": 3.2}]
result = find_biggest_deviation(issues)
test("Single issue → that one", result, ("bass", "hot", 3.2))


print("\n═══════════════════════════════════════════")
print("  2. batch_payload construction")
print("═══════════════════════════════════════════")

payload = build_batch_payload(1, 2, [("Gain", 0.045), ("Freq", 0.500)])
test("Two params → two entries", len(payload["parameters"]), 2)
test("Track index", payload["track_index"], 1)
test("Device index", payload["device_index"], 2)
test("First param uses name_or_index", 
     payload["parameters"][0].get("name_or_index"), "Gain")
test("Second param value",
     payload["parameters"][1]["value"], 0.500)

# Single change
payload = build_batch_payload(6, 0, [("Level", 0.75)])
test("Single param batch", len(payload["parameters"]), 1)


print("\n═══════════════════════════════════════════")
print("  3. Greedy: one fix instead of seven")
print("═══════════════════════════════════════════")

# Simulated device cache with role-tagged tracks
device_cache = [
    {"track_idx": 1, "device_idx": 1, "track_name": "kick punchy", "track_muted": False,
     "device_name": "EQ Eight", "params": {"output gain": {"index": 0, "value": 0.0}}},
    {"track_idx": 2, "device_idx": 1, "track_name": "hats 909", "track_muted": False,
     "device_name": "Auto Filter", "params": {"frequency": {"index": 0, "value": 0.5}}},
    {"track_idx": 6, "device_idx": 2, "track_name": "bass FM", "track_muted": False,
     "device_name": "Saturator", "params": {"drive": {"index": 1, "value": 0.3}}},
]

# Simulated recommendation map (subset of SMART_RECOMMENDATIONS)
rec_map = [
    {"match": ["sub frequencies", "sub is weak"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.06, "ceiling": "EQ Eight/Gain"}
    ]},
    {"match": ["bass (60-120hz) is weak"], "fix": [
        {"devices": ["EQ Eight"], "params": ["gain"], "delta_base": 0.05, "ceiling": "EQ Eight/Gain"}
    ]},
    {"match": ["presence is dull"], "fix": [
        {"devices": ["Auto Filter"], "params": ["frequency"], "delta_base": 0.03, "ceiling": None}
    ]},
]

# Sub is weak → targets kick EQ Eight
result = build_single_fix("sub", "weak", 3.5, device_cache, rec_map)
test("Sub weak → kick EQ Eight", result[4].split(":")[0] if result else None, "kick punchy/EQ Eight/output gain")

# Presence dull → targets hats Auto Filter
result = build_single_fix("presence", "weak", 4.2, device_cache, rec_map)
test("Presence dull → hats Auto Filter", result[4].split(":")[0] if result else None, "hats 909/Auto Filter/frequency")

# Bass weak — no matching device (no EQ Eight on bass track, only Saturator)
# Falls through to first EQ Eight found (kick)
result = build_single_fix("bass", "weak", 2.8, device_cache, rec_map)
test("Bass weak → falls back to first EQ Eight", result is not None, True)


print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'='*50}")

if failed:
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
