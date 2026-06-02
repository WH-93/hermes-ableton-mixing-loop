#!/usr/bin/env python3
"""
Tests for pre-flight audio validation (Issue #3).
Run: python3 tests/test_preflight.py
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ============================================================
# IMPLEMENTATION (port to mix_loop.py after tests pass)
# ============================================================

def validate_audio_signal(analysis):
    """Check if capture contains actual audio (not silence).
    Returns (ok: bool, message: str)."""
    if not analysis:
        return False, "Analysis failed — cannot validate audio."

    rms = analysis.get("rms_db", -200)
    peak = analysis.get("peak_db", -200)
    lufs = analysis.get("lufs_integrated")

    # Silence check: RMS below -80 dB means effectively silent
    if rms < -80:
        return False, (
            "No audio detected. Check:\n"
            "  1. Ableton Preferences → Audio → Output Device = BlackHole 2ch\n"
            "  2. Playback is running (press space in Ableton)\n"
            "  3. Master channel is not muted\n"
            "  4. Tracks are not all muted"
        )

    # Very quiet but not silent — warn but proceed
    if rms < -35:
        return True, (
            f"Audio is very quiet (RMS {rms:.1f} dB, peak {peak:.1f} dB).\n"
            "Check Ableton master fader and track volumes."
        )

    # Red-line warning
    if peak > -0.3:
        return True, (
            f"WARNING: Peak at {peak:.1f} dBFS — near clipping.\n"
            "Loop will block gain increases (red-line protection active)."
        )

    # Clipped
    if peak >= 0.0:
        return True, (
            f"WARNING: Audio is clipping (peak {peak:.1f} dBFS).\n"
            "Reduce master level or track volumes. Loop will block all gain increases."
        )

    return True, f"Audio OK — RMS {rms:.1f} dB, peak {peak:.1f} dB, LUFS {lufs}"


# ============================================================
# TESTS
# ============================================================

passed = 0
failed = 0

def test(name, actual, expected_ok, expected_contains=""):
    global passed, failed
    ok, msg = actual
    if ok == expected_ok and (not expected_contains or expected_contains.lower() in msg.lower()):
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")
        print(f"    Expected ok={expected_contains}, got ok={ok}")
        print(f"    Message: {msg[:100]}")


print("═══════════════════════════════════════════")
print("  1. Normal audio — passes")
print("═══════════════════════════════════════════")

result = validate_audio_signal({"rms_db": -19.0, "peak_db": -11.6, "lufs_integrated": -20.0})
test("Normal mix levels", result, True, "Audio OK")

result = validate_audio_signal({"rms_db": -10.0, "peak_db": -3.0, "lufs_integrated": -8.0})
test("Loud mix", result, True, "Audio OK")


print("\n═══════════════════════════════════════════")
print("  2. Silence — fails with actionable message")
print("═══════════════════════════════════════════")

result = validate_audio_signal({"rms_db": -200.0, "peak_db": -200.0, "lufs_integrated": None})
test("Complete silence (-200 dB)", result, False, "BlackHole 2ch")

result = validate_audio_signal({"rms_db": -85.0, "peak_db": -90.0, "lufs_integrated": -90.0})
test("Near silence (-85 dB)", result, False, "BlackHole 2ch")


print("\n═══════════════════════════════════════════")
print("  3. Quiet but not silent — warns, proceeds")
print("═══════════════════════════════════════════")

result = validate_audio_signal({"rms_db": -38.0, "peak_db": -30.0, "lufs_integrated": -40.0})
test("Quiet audio (-38 dB)", result, True, "very quiet")


print("\n═══════════════════════════════════════════")
print("  4. Near clipping — warns about red-line")
print("═══════════════════════════════════════════")

result = validate_audio_signal({"rms_db": -6.0, "peak_db": -0.2, "lufs_integrated": -5.0})
test("Near clipping (-0.2 dB)", result, True, "red-line")

result = validate_audio_signal({"rms_db": -4.0, "peak_db": 0.5, "lufs_integrated": -4.0})
test("Clipping (+0.5 dB)", result, True, "clipping")


print("\n═══════════════════════════════════════════")
print("  5. None/invalid input")
print("═══════════════════════════════════════════")

result = validate_audio_signal(None)
test("None analysis", result, False, "cannot validate")

result = validate_audio_signal({})
test("Empty analysis", result, False, "cannot validate")


print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'='*50}")

if failed:
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
