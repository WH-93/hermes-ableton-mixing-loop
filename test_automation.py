#!/opt/homebrew/bin/python3
"""TDD tests for automation envelope OSC handlers."""
import sys
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

TRACK, SLOT = 1, 0

osc = AbletonOSC()
osc.connect()

print("=== Automation TDD ===")
print(f"Track {TRACK}, clip {SLOT} (hats ROLLER / 909-MINIMAL)\n")

tests = [
    ("list empty", "/live/clip/automation/list", (TRACK, SLOT), 0),
    ("create",     "/live/clip/automation/create", (TRACK, SLOT, "EQ Eight", "1 Gain A"), "ok"),
    ("list one",   "/live/clip/automation/list", (TRACK, SLOT), 1),
    ("insert",     "/live/clip/automation/insert_step", (TRACK, SLOT, 0, 0.0, 0.5, 0.0), "ok"),
    ("clear_all",  "/live/clip/automation/clear_all", (TRACK, SLOT), "ok"),
    ("list zero",  "/live/clip/automation/list", (TRACK, SLOT), 0),
]

passed = 0
for name, addr, args, expected in tests:
    try:
        r = osc.query(addr, *args, timeout=3.0)
        val = int(r[2]) if isinstance(expected, int) else r[2]
        assert val == expected, f"{val} != {expected}"
        print(f"  ✓ {name}: {val}")
        passed += 1
    except Exception as e:
        print(f"  ✗ {name}: {e}")

osc.disconnect()
print(f"\n{passed}/{len(tests)} passed")
sys.exit(0 if passed == len(tests) else 1)
