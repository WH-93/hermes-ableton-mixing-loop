#!/opt/homebrew/bin/python3
"""Arrangement overdub — kick+hats only at start/end via track volume automation."""
import sys, time, math
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

BARS = 210; BPM = 140.0
BAR_S = 4 * 60.0 / BPM; TOTAL = BARS * BAR_S
BOOKEND = 30.0; FADE = 5.0  # 30s bookend, 5s fade transition
FADE_OUT_START = TOTAL - BOOKEND

def ramp(t0, v0, t1, v1, n=15):
    return [(t0 + i*(t1-t0)/n, v0 + i*(v1-v0)/n) for i in range(n+1)]
def hold(t0, v, t1, n=4):
    return [(t0 + i*(t1-t0)/n, v) for i in range(n+1)]

# Track volume automation — kick(0) untouched, hats(1) always on, others fade in/out
# (track_index, normal_volume)
TRACKS = [
    (1,  0.85, True),   # hats — always on
    (4,  0.85, False),  # bass DEEP — fades
    (6,  0.75, False),  # lead HYPNOTIC
    (12, 0.85, False),  # bass WEIGHT
    (13, 0.50, False),  # lead HOOK
    (14, 0.75, False),  # perc PERC-B
    (17, 0.70, False),  # lead CYBER-PERC
    (18, 0.70, False),  # pad VOID
    (20, 0.65, False),  # pad PAD-MIDI
    (21, 0.75, False),  # toms POUND
    (22, 0.75, False),  # CONGA-TRIBAL
]

# Build volume curves for each track
curves = []
for ti, normal_vol, always_on in TRACKS:
    if always_on:
        # Hats: always at normal volume
        points = hold(0, normal_vol, TOTAL, 4)
    else:
        points = (
            hold(0, 0.0, BOOKEND, 4) +                                    # silent first 30s
            ramp(BOOKEND, 0.0, BOOKEND + FADE, normal_vol, 10) +          # fade in over 5s
            hold(BOOKEND + FADE, normal_vol, FADE_OUT_START - FADE, 4) +  # hold during middle
            ramp(FADE_OUT_START - FADE, normal_vol, FADE_OUT_START, 0.0, 10) +  # fade out over 5s
            hold(FADE_OUT_START, 0.0, TOTAL, 4)                           # silent last 30s
        )
    curves.append((ti, points))

osc = AbletonOSC(); osc.connect(); time.sleep(0.3)
print(f"Volume automation — {len(curves)} tracks, {TOTAL/60:.0f} min, {BOOKEND}s bookends")

# ── Beat sync: wait for next beat 1 ──
osc.send('/live/song/start_listen/beat')
print("  Waiting for next bar...")
while True:
    try:
        beat = osc.query('/live/song/get/current_song_time', timeout=2.0)
        current_time = float(beat[0])
        beats_in = 4.0 - (current_time % 4.0)
        if beats_in < 0.05:
            break
        if beats_in > 3.8:  # near bar start
            break
        time.sleep(0.1)
    except:
        time.sleep(0.2)

osc.send('/live/song/stop_listen/beat')
print(f"✓ Synced to bar start at song_time={current_time:.1f}")

osc.send('/live/song/set/arrangement_overdub', 1); time.sleep(0.2)
osc.send('/live/song/set/record_mode', 1); time.sleep(0.2)
print("✓ Recording — check transport bar for red record light")

start = time.time(); last = 0; updates = 0
try:
    while time.time() - start < TOTAL + 2:
        now = time.time() - start
        if now - last < 0.2: time.sleep(0.05); continue
        last = now
        
        for ti, points in curves:
            for i in range(len(points) - 1):
                if points[i][0] <= now <= points[i+1][0]:
                    frac = (now - points[i][0]) / (points[i+1][0] - points[i][0])
                    val = points[i][1] + frac * (points[i+1][1] - points[i][1])
                    try: osc.send('/live/track/set/volume', ti, float(val))
                    except: pass; break
        
        updates += 1
        if updates % 25 == 0:
            print(f"  [{int(now)}s/{int(TOTAL)}s] {int(TOTAL-now)}s left")

except KeyboardInterrupt: print("\nInterrupted")

osc.send('/live/song/set/record_mode', 0); time.sleep(0.2)
osc.send('/live/song/set/arrangement_overdub', 0)
osc.disconnect()
print(f"✓ Done. {updates} volume updates. First/last {BOOKEND}s = kick+hats only.")
