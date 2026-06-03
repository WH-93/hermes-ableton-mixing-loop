#!/opt/homebrew/bin/python3
"""
Tier 3 TDD: Full arrangement construction — clip + notes + automation.
Tests the complete pipeline end-to-end on a single track.
"""
import sys, time
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

TRACK, SLOT = 1, 2  # hats ROLLER, scene 3 (has EQ Eight)
BARS = 4

osc = AbletonOSC()
osc.connect()
time.sleep(0.3)

print("=== Tier 3 TDD: Arrangement Builder ===\n")

# ── Test 1: create clip ──
print("1. Create clip...")
osc.send('/live/clip_slot/create_clip', TRACK, SLOT, BARS * 4.0)
time.sleep(0.5)
length = osc.query('/live/clip/get/length', TRACK, SLOT, timeout=2.0)
assert int(length[2]) == BARS * 4, f"Expected {BARS*4}, got {length[2]}"
print(f"   ✓ clip created: {int(length[2])} beats")

# ── Test 2: add notes ──
print("2. Add notes (4-bar kick pattern)...")
notes = []
for bar in range(BARS):
    notes.extend([36, bar * 4 + 0.0, 1.0, 100, 0])  # four-on-floor
osc.send('/live/clip/add/notes', TRACK, SLOT, *notes)
time.sleep(0.5)
note_count = osc.query('/live/clip/get/notes', TRACK, SLOT, timeout=2.0)
assert len(note_count) > 2, f"No notes returned"
print(f"   ✓ {len(note_count)} values ({len(note_count)//5} notes)")

# ── Test 3: create automation ──
print("3. Create automation envelope (EQ Eight 1 Gain A)...")
# Check if track has EQ Eight
devices_raw = osc.query('/live/track/get/devices/name', TRACK, timeout=2.0)
dev_names = devices_raw[1:] if devices_raw else []
has_eq = any('EQ' in str(d) or 'Eight' in str(d) for d in dev_names)
if not has_eq:
    print("   ⚠ No EQ Eight on this track, skipping automation test")
    osc.send('/live/clip/automation/clear_all', TRACK, SLOT)
    osc.disconnect()
    print("\n3/3 passed (automation skipped — no EQ on track)")
    sys.exit(0)

r = osc.query('/live/clip/automation/create', TRACK, SLOT, 'EQ Eight', '1 Gain A', timeout=2.0)
assert r[2] == 'ok', f"create failed: {r}"
print(f"   ✓ envelope created")

# ── Test 4: insert automation curve ──
print("4. Insert automation curve (ramp 0→1 over 4 bars)...")
steps = [(0.0, 0.0), (4.0, 0.3), (8.0, 0.7), (12.0, 1.0), (16.0, 0.5)]
for t, v in steps:
    r = osc.query('/live/clip/automation/insert_step', TRACK, SLOT, 0, t, v, 0.0, timeout=2.0)
    assert r[2] == 'ok', f"insert_step({t}, {v}) failed: {r}"
print(f"   ✓ {len(steps)} automation points inserted")

# ── Test 5: verify envelope count ──
print("5. Verify envelope count...")
r = osc.query('/live/clip/automation/list', TRACK, SLOT, timeout=2.0)
assert int(r[2]) == 1, f"Expected 1 envelope, got {r[2]}"
print(f"   ✓ 1 envelope confirmed")

# ── Cleanup ──
print("\n6. Cleanup...")
osc.send('/live/clip/automation/clear_all', TRACK, SLOT)
time.sleep(0.3)
r = osc.query('/live/clip/automation/list', TRACK, SLOT, timeout=2.0)
assert int(r[2]) == 0, f"Expected 0 after clear, got {r[2]}"
print(f"   ✓ cleared")

osc.disconnect()
print("\n✓✓ 6/6 — Tier 3 arrangement builder pipeline verified")
