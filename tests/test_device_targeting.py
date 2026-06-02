#!/usr/bin/env python3
"""
Tests for role-based device targeting (Issue #2).
Convention: first word of track name = role tag.
Run: python3 tests/test_device_targeting.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ============================================================
# CANONICAL ROLES
# ============================================================

# Mapping: role tag (first word of track name) → target category
# Used by find_device to prefer the right track for each recommendation
ROLE_TO_CATEGORY = {
    # Low-end roles — target for sub/bass adjustments
    "kick": "low_end",
    "bass": "low_end",
    "sub": "low_end",
    "rumble": "low_end",
    "808": "low_end",
    # High-frequency roles — target for presence/air adjustments
    "hats": "hi_freq",
    "hat": "hi_freq",
    "ride": "hi_freq",
    "cymbal": "hi_freq",
    "hihat": "hi_freq",
    # Synth roles — target for midrange/presence/width
    "synth": "synth",
    "pad": "synth",
    "chord": "synth",
    "lead": "synth",
    "hook": "synth",
    "melody": "synth",
    "arp": "synth",
    # Percussion roles
    "perc": "percussion",
    "toms": "percussion",
    "tom": "percussion",
    "conga": "percussion",
    "clap": "percussion",
    "snare": "percussion",
    "shaker": "percussion",
    # FX / spatial
    "fx": "spatial",
    "reverb": "spatial",
    "delay": "spatial",
    "echo": "spatial",
    "noise": "spatial",
    "riser": "spatial",
    "sweep": "spatial",
    # Mix bus
    "group": "mix_bus",
    "bus": "mix_bus",
    "master": "mix_bus",
    "mix": "mix_bus",
    # Vocal
    "vox": "mid",
    "vocal": "mid",
    "voice": "mid",
    "sample": "mid",
}

# Category → recommendation keywords it targets
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

# ============================================================
# IMPLEMENTATION
# ============================================================

def parse_track_role(track_name):
    """Extract role tag from first word of track name. Returns role or None."""
    if not track_name:
        return None
    first_word = track_name.strip().split()[0].lower()
    # Strip common suffixes
    for suffix in ["-", "_", ".", ":"]:
        if first_word.endswith(suffix):
            first_word = first_word[:-1]
    return first_word if first_word in ROLE_TO_CATEGORY else None


def get_track_category(track_name):
    """Get target category for a track based on its role tag. Returns None if untagged."""
    role = parse_track_role(track_name)
    return ROLE_TO_CATEGORY.get(role) if role else None


def category_matches_recommendation(category, rec_text):
    """Does this category target this recommendation text? Returns True/False."""
    if not category or category not in CATEGORY_TARGETS:
        return False
    rec_lower = rec_text.lower()
    return any(target.lower() in rec_lower for target in CATEGORY_TARGETS[category])


def find_device(session_devices, device_types, rec_text=None, exclude_muted=True):
    """Find best device matching device_types, preferring tracks whose role
    category matches the recommendation text.
    - rec_text: the recommendation text (e.g., "Sub frequencies are weak")
    - If no matching category found, falls back to first device match
    """
    # Backwards compat: no rec_text → return first match
    if rec_text is None:
        for d in session_devices:
            if exclude_muted and d.get("track_muted"):
                continue
            dname_lower = d["device_name"].lower()
            if any(dt.lower() in dname_lower for dt in device_types):
                return (d["track_idx"], d["device_idx"], d["device_name"], d["track_name"])
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

    # Sort: highest score first, then lowest track_idx as tiebreaker
    matches.sort(key=lambda x: (-x[0], x[1]["track_idx"]))
    best = matches[0][1]
    return (best["track_idx"], best["device_idx"], best["device_name"], best["track_name"])


# ============================================================
# TEST DATA — same device layout, but names now follow convention
# ============================================================

SESSION_DEVICES = [
    # kick punchy 808
    {"track_idx": 1, "device_idx": 1, "track_name": "kick punchy 808", "track_muted": False, "device_name": "EQ Eight"},
    {"track_idx": 1, "device_idx": 2, "track_name": "kick punchy 808", "track_muted": False, "device_name": "Utility"},
    {"track_idx": 1, "device_idx": 3, "track_name": "kick punchy 808", "track_muted": False, "device_name": "Compressor"},
    # hats 909 minimal
    {"track_idx": 2, "device_idx": 1, "track_name": "hats 909 minimal", "track_muted": False, "device_name": "Auto Filter"},
    {"track_idx": 2, "device_idx": 2, "track_name": "hats 909 minimal", "track_muted": False, "device_name": "EQ Eight"},
    # bass FM dark
    {"track_idx": 6, "device_idx": 1, "track_name": "bass FM dark", "track_muted": False, "device_name": "Operator"},
    {"track_idx": 6, "device_idx": 2, "track_name": "bass FM dark", "track_muted": False, "device_name": "Saturator"},
    # perc tribal space
    {"track_idx": 12, "device_idx": 1, "track_name": "perc tribal space", "track_muted": False, "device_name": "Auto Filter"},
    # rumble hardgroove
    {"track_idx": 15, "device_idx": 1, "track_name": "rumble hardgroove", "track_muted": False, "device_name": "Saturator"},
    # hook swirly phrygian
    {"track_idx": 16, "device_idx": 1, "track_name": "hook swirly phrygian", "track_muted": False, "device_name": "EQ Eight"},
    {"track_idx": 16, "device_idx": 2, "track_name": "hook swirly phrygian", "track_muted": False, "device_name": "Utility"},
    # pad warm chords
    {"track_idx": 23, "device_idx": 1, "track_name": "pad warm chords", "track_muted": False, "device_name": "Utility"},
    {"track_idx": 23, "device_idx": 2, "track_name": "pad warm chords", "track_muted": False, "device_name": "Saturator"},
    # group drums
    {"track_idx": 0, "device_idx": 0, "track_name": "group drums", "track_muted": False, "device_name": "Utility"},
    {"track_idx": 0, "device_idx": 1, "track_name": "group drums", "track_muted": False, "device_name": "Compressor"},
    # Untagged tracks (backwards compat)
    {"track_idx": 24, "device_idx": 1, "track_name": "TOMS", "track_muted": False, "device_name": "Utility"},
    {"track_idx": 25, "device_idx": 1, "track_name": "HATS-N-RIDES", "track_muted": False, "device_name": "Utility"},
]


# ============================================================
# TESTS
# ============================================================

passed = 0
failed = 0

def test(name, actual, expected, note=""):
    global passed, failed
    if actual == expected:
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
print("  1. Role parsing")
print("═══════════════════════════════════════════")

test("kick punchy 808 → kick", parse_track_role("kick punchy 808"), "kick")
test("bass FM dark → bass", parse_track_role("bass FM dark"), "bass")
test("hats 909 minimal → hats", parse_track_role("hats 909 minimal"), "hats")
test("hook swirly phrygian → hook", parse_track_role("hook swirly phrygian"), "hook")
test("group drums → group", parse_track_role("group drums"), "group")
test("pad warm chords → pad", parse_track_role("pad warm chords"), "pad")
test("TOMS (lowercase toms is a valid role) → toms", parse_track_role("TOMS"), "toms")
test("HATS-N-RIDES (hyphen ≠ space, no match) → None", parse_track_role("HATS-N-RIDES"), None)
test("Empty string → None", parse_track_role(""), None)


print("\n═══════════════════════════════════════════")
print("  2. Category mapping")
print("═══════════════════════════════════════════")

test("kick → low_end", get_track_category("kick punchy 808"), "low_end")
test("bass → low_end", get_track_category("bass FM dark"), "low_end")
test("rumble → low_end", get_track_category("rumble hardgroove"), "low_end")
test("hats → hi_freq", get_track_category("hats 909 minimal"), "hi_freq")
test("hook → synth", get_track_category("hook swirly phrygian"), "synth")
test("pad → synth", get_track_category("pad warm chords"), "synth")
test("group → mix_bus", get_track_category("group drums"), "mix_bus")
test("perc → percussion", get_track_category("perc tribal space"), "percussion")
test("toms → percussion", get_track_category("TOMS"), "percussion")


print("\n═══════════════════════════════════════════")
print("  3. Category matches recommendation")
print("═══════════════════════════════════════════")

test("low_end matches 'Sub frequencies are weak'", 
     category_matches_recommendation("low_end", "Sub frequencies (20-60Hz) are weak"), True)
test("low_end matches 'Bass is hot'",
     category_matches_recommendation("low_end", "Bass (60-120Hz) is hot"), True)
test("low_end does NOT match 'Presence is dull'",
     category_matches_recommendation("low_end", "Presence (2-6kHz) is dull"), False)
test("hi_freq matches 'Air is missing'",
     category_matches_recommendation("hi_freq", "Air (6-16kHz) is missing"), True)
test("hi_freq matches 'Presence is dull'",
     category_matches_recommendation("hi_freq", "Presence (2-6kHz) is dull"), True)
test("synth matches 'Mix is narrow'",
     category_matches_recommendation("synth", "Mix is narrow. Widen pads/hats/fx."), True)
test("mix_bus matches 'Master is too quiet'",
     category_matches_recommendation("mix_bus", "Master is too quiet. Raise master gain."), True)
test("mix_bus matches 'Track is over-compressed'",
     category_matches_recommendation("mix_bus", "Track is over-compressed."), True)


print("\n═══════════════════════════════════════════")
print("  4. Sub is weak → targets kick/bass (low_end)")
print("═══════════════════════════════════════════")

rec = "Sub frequencies (20-60Hz) are weak. Boost kick sub, add more rumble body."
result = find_device(SESSION_DEVICES, ["EQ Eight"], rec_text=rec)
test("Sub weak → kick EQ Eight (not hats)", result[3] if result else None, "kick punchy 808")

result = find_device(SESSION_DEVICES, ["Saturator"], rec_text=rec)
test("Sub weak → bass Saturator (not rumble)", result[3] if result else None, "bass FM dark")


print("\n═══════════════════════════════════════════")
print("  5. Presence dull → targets hats/hook (hi_freq/synth)")
print("═══════════════════════════════════════════")

rec = "Presence (2-6kHz) is dull. Boost hats, add saturation to synths, open filters."
result = find_device(SESSION_DEVICES, ["EQ Eight"], rec_text=rec)
test("Presence dull → hats EQ Eight (not kick)", result[3] if result else None, "hats 909 minimal")

# Auto Filter only exists on hats and perc — prefer hats
result = find_device(SESSION_DEVICES, ["Auto Filter"], rec_text=rec)
test("Presence dull → hats Auto Filter", result[3] if result else None, "hats 909 minimal")


print("\n═══════════════════════════════════════════")
print("  6. Master too quiet → targets group (mix_bus)")
print("═══════════════════════════════════════════")

rec = "Master is too quiet. Raise master gain or reduce headroom."
result = find_device(SESSION_DEVICES, ["Utility"], rec_text=rec)
test("Master quiet → group Utility (not kick)", result[3] if result else None, "group drums")

result = find_device(SESSION_DEVICES, ["Compressor"], rec_text=rec)
test("Master quiet → group Compressor (mix_bus)", result[3] if result else None, "group drums")


print("\n═══════════════════════════════════════════")
print("  7. Narrow mix → targets pad/hook (synth)")
print("═══════════════════════════════════════════")

rec = "Mix is narrow. Widen pads/hats/fx."
result = find_device(SESSION_DEVICES, ["Utility"], rec_text=rec)
test("Narrow → hook Utility (both hook and pad are synth, hook wins by idx)",
     result[3] if result else None, "hook swirly phrygian")


print("\n═══════════════════════════════════════════")
print("  8. Fallback: untagged tracks get neutral score")
print("═══════════════════════════════════════════")

rec = "Sub frequencies (20-60Hz) are weak."
result = find_device(SESSION_DEVICES, ["Utility"], rec_text=rec)
test("Sub weak → kick Utility (tagged low_end beats untagged TOMS)",
     result[3] if result else None, "kick punchy 808")


print("\n═══════════════════════════════════════════")
print("  9. No rec_text → first match (backwards compat)")
print("═══════════════════════════════════════════")

result = find_device(SESSION_DEVICES, ["EQ Eight"], rec_text=None)
test("No rec → first EQ Eight", result[3] if result else None, "kick punchy 808")

result = find_device(SESSION_DEVICES, ["Utility"], rec_text=None)
test("No rec → first Utility", result[3] if result else None, "kick punchy 808")


print("\n═══════════════════════════════════════════")
print("  10. No matching devices")
print("═══════════════════════════════════════════")

result = find_device(SESSION_DEVICES, ["NonexistentDevice"], rec_text="Sub is weak")
test("No match → None", result, None)


print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'='*50}")

if failed:
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
