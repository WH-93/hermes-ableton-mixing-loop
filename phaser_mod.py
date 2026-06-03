#!/opt/homebrew/bin/python3
"""Phaser-Flanger dual modulation — rich parameter automation, freq ≤ 1.2kHz."""
import sys, time, math
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

phi = 1.618033988749895
BARS = 210; BPM = 140.0
BAR_S = 4 * 60.0 / BPM; TOTAL = BARS * BAR_S
I1, I2 = TOTAL * 0.20, TOTAL * 0.60  # 20% intro, 60% mid

def ramp(t0, v0, t1, v1, n=20):
    return [(t0 + i*(t1-t0)/n, v0 + i*(v1-v0)/n) for i in range(n+1)]
def hold(t0, v, t1, n=4):
    return [(t0 + i*(t1-t0)/n, v) for i in range(n+1)]

# ═══ ALL PARAMETER CURVES ═══
all_curves = []

for di in [0, 1]:
    off = 0.03 if di == 0 else -0.03  # slight stereo offset
    
    # ── Dry/Wet: 0 → 0.2 → 0.4 → 0 ──
    all_curves.append((6, di, 30,  # index 30 = Dry/Wet
        ramp(0, 0.0, I1, 0.20 + off, 15) +           # 0→20% over intro
        hold(I1, 0.20 + off, I2, 4) +                 # hold 20%
        ramp(I2, 0.20 + off, TOTAL*0.75, 0.40+off,12)+# rise to 40%
        ramp(TOTAL*0.75, 0.40+off, TOTAL, 0.0, 20)    # fade out
    ))
    
    # ── Amount: 0 → 0.5 → 1.0 → 0 ──
    all_curves.append((6, di, 1,
        ramp(0, 0.0, I1*0.5, 0.3, 10) +
        ramp(I1*0.5, 0.3, I1, 0.5+off, 8) +
        hold(I1, 0.5+off, I2, 4) +
        ramp(I2, 0.5+off, TOTAL*0.7, 1.0, 10) +
        ramp(TOTAL*0.7, 1.0, TOTAL, 0.0, 15)
    ))
    
    # ── Feedback: 0 → subtle → bloom → 0 ──
    all_curves.append((6, di, 25,
        hold(0, 0.0, I1, 4) +
        ramp(I1, 0.0, I2, 0.15+off, 8) +
        ramp(I2, 0.15+off, TOTAL*0.75, 0.35, 10) +
        ramp(TOTAL*0.75, 0.35, TOTAL, 0.0, 15)
    ))
    
    # ── Spread: stereo width pulse ──
    all_curves.append((6, di, 24,
        hold(0, 0.3, TOTAL*0.2, 4) +
        ramp(TOTAL*0.2, 0.3, TOTAL*0.5, 0.7, 10) +
        ramp(TOTAL*0.5, 0.7, TOTAL*0.8, 0.3, 10) +
        hold(TOTAL*0.8, 0.3, TOTAL, 4)
    ))
    
    # ── Env Amount: follows intensity ──
    all_curves.append((6, di, 15,
        hold(0, 0.0, I1*0.5, 4) +
        ramp(I1*0.5, 0.0, I1, 0.4, 8) +
        hold(I1, 0.4, I2, 4) +
        ramp(I2, 0.4, TOTAL*0.7, 0.7, 10) +
        ramp(TOTAL*0.7, 0.7, TOTAL, 0.0, 15)
    ))
    
    # ── Spin: occasional rotation ──
    all_curves.append((6, di, 11,
        hold(0, 0.0, TOTAL*0.3, 4) +
        ramp(TOTAL*0.3, 0.0, TOTAL*0.5, 0.4, 8) +
        ramp(TOTAL*0.5, 0.4, TOTAL*0.7, 0.7, 8) +
        ramp(TOTAL*0.7, 0.7, TOTAL, 0.0, 12)
    ))

# ═══ RATE CURVES: geometric φ-ratios, but keep freq ≤ 1.2kHz ═══
# Mod Freq (0-1) controls modulation rate relative to Center Freq
# Limit Mod Freq to 0.5 max to keep resulting freq ≤ ~1.2kHz
rate_configs = [
    (6, 0, 7,  1.0),      # dev0 Mod Rate
    (6, 0, 8,  phi),      # dev0 Mod Rate2
    (6, 1, 7,  phi**2),   # dev1 Mod Rate
    (6, 1, 8,  phi**3),   # dev1 Mod Rate2
]

for ti, di, pi, ratio in rate_configs:
    points = []
    for t in [i * 0.4 for i in range(int(TOTAL * 2.5) + 1)]:
        base = 2.0 + 2.5 * math.sin(t / TOTAL * math.pi * 1.7 + ratio * 0.5)
        wobble = 0.3 * math.sin(t * phi * 0.9 + ratio)
        val = (base + wobble) * ratio * 0.8
        val = max(0.3, min(8.0, val))
        points.append((t, round(val, 2)))
    all_curves.append((ti, di, pi, points))

# Mod Freq: sweep but capped at 0.5 (keeps modulation ≤ ~1.2kHz)
for di in [0, 1]:
    for pi in [3, 4]:
        ratio = phi if di == 1 else 1.0
        points = []
        for t in [i * 0.5 for i in range(int(TOTAL * 2) + 1)]:
            w = math.sin(t / TOTAL * math.pi * 1.3 + (di + pi) * 0.7)
            val = 0.15 + 0.35 * abs(w)  # 0.15-0.50 range
            val = max(0.05, min(0.50, val))  # cap at 0.50 = ~1.2kHz
            points.append((t, round(val, 3)))
        all_curves.append((6, di, pi, points))

# ═══ Center Freq: slow drift, never above 1.2kHz target ═══
for di in [0, 1]:
    points = []
    for t in [i * 0.5 for i in range(int(TOTAL * 2) + 1)]:
        val = 0.35 + 0.15 * math.sin(t / TOTAL * math.pi * 0.7 + di)
        val = max(0.2, min(0.55, val))  # keeps freq reasonable
        points.append((t, round(val, 3)))
    all_curves.append((6, di, 23, points))

print(f"Phaser-Flanger: {len(all_curves)} parameter curves, {TOTAL/60:.0f} min")
print(f"  Dry/Wet: 0→20%→40%→0  |  Freq ≤ 1.2kHz  |  φ-geometric rates")

osc = AbletonOSC(); osc.connect(); time.sleep(0.3)

# Beat sync
print("  Waiting for next bar...")
while True:
    try:
        ct = float(osc.query('/live/song/get/current_song_time', timeout=2.0)[0])
        if ct % 4.0 < 0.12 or ct % 4.0 > 3.8: break
        time.sleep(0.15)
    except: time.sleep(0.3)
print(f"✓ Synced at {ct:.1f}s")

# Engage recording
osc.send('/live/song/set/arrangement_overdub', 1); time.sleep(0.3)
r = osc.query('/live/song/get/arrangement_overdub', timeout=2.0)
osc.send('/live/song/set/record_mode', 1); time.sleep(0.3)
r2 = osc.query('/live/song/get/record_mode', timeout=2.0)
if not r or not r[0] or not r2 or not r2[0]:
    print("✗ Recording failed to engage"); osc.disconnect(); sys.exit(1)
print("✓ Recording")

start = time.time(); last = 0; updates = 0
try:
    while time.time() - start < TOTAL + 2:
        now = time.time() - start
        if now - last < 0.2: time.sleep(0.05); continue
        last = now
        for ti, di, pi, points in all_curves:
            for i in range(len(points)-1):
                if points[i][0] <= now <= points[i+1][0]:
                    frac = (now-points[i][0])/(points[i+1][0]-points[i][0])
                    osc.send('/live/device/set/parameter/value', ti, di, pi,
                            float(points[i][1] + frac*(points[i+1][1]-points[i][1])))
                    break
        updates += 1
        if updates % 50 == 0:
            print(f"  [{int(now)}s/{int(TOTAL)}s]")

except KeyboardInterrupt: print("\nInterrupted")

osc.send('/live/song/set/record_mode', 0); time.sleep(0.2)
osc.send('/live/song/set/arrangement_overdub', 0); osc.disconnect()
print(f"✓ {updates} updates. 2x Phaser-Flanger, {len(all_curves)} curves.")
