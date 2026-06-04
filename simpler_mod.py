#!/opt/homebrew/bin/python3
"""Simpler + Sampler — syllable-rhythm modulation. 'future is next' = 4-hit 16ths pattern."""
import sys, time, math
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

phi = 1.618033988749895
BARS = 64; BPM = 140.0; SEC_PER_BEAT = 60.0 / BPM
SIXTEENTH = SEC_PER_BEAT / 4  # ~0.107s
EIGHTH = SEC_PER_BEAT / 2      # ~0.214s
TOTAL = BARS * 4 * SEC_PER_BEAT

# 'future is next' syllables → 16th-note positions in a bar
# FU-ture-IS-next → hits on 0, 4, 8, 12 (every quarter beat)
# Secondary 8th-note positions: 2, 6, 10, 14
SYLLABLE_HITS_16TH = [0, 4, 8, 12]   # strong syllables
SUB_HITS_8TH = [2, 6, 10, 14]         # 8th-note between syllables

ORIGINAL_SIMPLER = 9
MULTI_SAMPLER = 10

# Parameter groups — each syllable triggers a different parameter value
# Pattern repeats every bar (4 syllables per bar)
# Values cycle through 4 states per parameter

params = [
    # ═══ OriginalSimpler (track 9, dev 0) ═══
    # Each param: (track, dev, idx, [values_for_4_syllables], sub_8th_jitter)
    
    # Filter — each syllable changes filter state
    (ORIGINAL_SIMPLER, 0, 41, [0.15, 0.35, 0.65, 1.0], True),  # Freq: closed→half→open
    (ORIGINAL_SIMPLER, 0, 42, [0.0, 0.2, 0.45, 0.6], True),    # Res
    (ORIGINAL_SIMPLER, 0, 44, [0.0, 0.1, 0.2, 0.25], False),   # Drive
    (ORIGINAL_SIMPLER, 0, 43, [0.0, 0.15, 0.35, 0.5], True),   # Morph
    (ORIGINAL_SIMPLER, 0, 53, [0.0, 0.1, 0.2, 0.3], True),     # Filter LFO
    
    # Amp envelope — percussive shapes per syllable
    (ORIGINAL_SIMPLER, 0, 20, [0.6, 0.85, 0.7, 1.0], False),   # Volume: accent pattern
    (ORIGINAL_SIMPLER, 0, 23, [-0.7, -0.2, 0.2, 0.7], True),   # Pan: stereo spread
    (ORIGINAL_SIMPLER, 0, 26, [0.0, 0.05, 0.15, 0.3], True),   # Ve Attack
    (ORIGINAL_SIMPLER, 0, 27, [0.3, 0.5, 0.65, 0.8], False),   # Ve Decay
    (ORIGINAL_SIMPLER, 0, 29, [0.2, 0.35, 0.5, 0.7], False),   # Ve Release
    
    # Pitch — syllable bends
    (ORIGINAL_SIMPLER, 0, 11, [-2.0, 0.0, 2.0, -1.0], True),   # Transpose
    (ORIGINAL_SIMPLER, 0, 12, [-0.2, 0.0, 0.2, 0.3], True),    # Detune
    
    # Filter envelope
    (ORIGINAL_SIMPLER, 0, 47, [0.0, 0.1, 0.25, 0.4], False),   # Fe Attack
    (ORIGINAL_SIMPLER, 0, 48, [0.3, 0.45, 0.6, 0.8], False),   # Fe Decay
    
    # Spread + Glide
    (ORIGINAL_SIMPLER, 0, 8,  [0.0, 0.15, 0.3, 0.5], False),   # Spread
    (ORIGINAL_SIMPLER, 0, 10, [0.3, 0.45, 0.6, 0.8], False),   # Glide
    
    # ═══ MultiSampler (track 10, dev 0) ═══
    (MULTI_SAMPLER, 0, 42, [0.12, 0.3, 0.6, 1.0], True),      # Freq
    (MULTI_SAMPLER, 0, 43, [0.0, 0.15, 0.4, 0.55], True),     # Res
    (MULTI_SAMPLER, 0, 45, [0.0, 0.08, 0.18, 0.22], False),    # Drive
    (MULTI_SAMPLER, 0, 44, [0.0, 0.12, 0.3, 0.45], True),      # Morph
    (MULTI_SAMPLER, 0, 64, [0.0, 0.08, 0.18, 0.28], True),     # Filter LFO
    
    (MULTI_SAMPLER, 0, 15, [0.5, 0.8, 0.65, 1.0], False),     # Volume
    (MULTI_SAMPLER, 0, 18, [-0.7, -0.2, 0.2, 0.7], True),     # Pan
    (MULTI_SAMPLER, 0, 21, [0.0, 0.04, 0.12, 0.25], True),    # Ve Attack
    (MULTI_SAMPLER, 0, 24, [0.3, 0.48, 0.62, 0.78], False),   # Ve Decay
    (MULTI_SAMPLER, 0, 28, [0.2, 0.33, 0.48, 0.65], False),   # Ve Release
    (MULTI_SAMPLER, 0, 31, [0.35, 0.55, 0.7, 0.9], True),     # Ve Loop
    (MULTI_SAMPLER, 0, 56, [0.35, 0.55, 0.7, 0.88], False),   # Fe End
    
    (MULTI_SAMPLER, 0, 11, [-1.5, 0.0, 1.5, -0.5], True),    # Transpose
    (MULTI_SAMPLER, 0, 12, [-0.15, 0.0, 0.18, 0.25], True),   # Detune
    
    (MULTI_SAMPLER, 0, 48, [0.0, 0.08, 0.22, 0.38], False),   # Fe Attack
    (MULTI_SAMPLER, 0, 51, [0.3, 0.42, 0.58, 0.75], False),   # Fe Decay
    
    (MULTI_SAMPLER, 0, 5,  [0.0, 0.12, 0.28, 0.45], False),   # Spread
    (MULTI_SAMPLER, 0, 8,  [0.3, 0.42, 0.58, 0.75], False),   # Glide
]

print(f"'future is next' rhythm — {len(params)} params, {BARS} bars, 16th-note stepped")
print(f"  Syllable hits: {['FU','ture','IS','next']} at 16th positions {SYLLABLE_HITS_16TH}")
print(f"  8th-note jitter on {sum(1 for p in params if p[4])} params")

osc = AbletonOSC(); osc.connect(); time.sleep(0.3)

# Beat sync: trigger on next beat advance after play
print("  Waiting for play...")
prev_ct = -1
while True:
    try:
        ct = float(osc.query('/live/song/get/current_song_time', timeout=2.0)[0])
        if prev_ct >= 0 and ct > prev_ct and ct - prev_ct < 0.5:
            break  # transport just advanced — play was pressed
        prev_ct = ct
        time.sleep(0.05)
    except: time.sleep(0.2)
print(f"✓ Play detected at {ct:.2f}s")

osc.send('/live/song/set/arrangement_overdub', 1); time.sleep(0.3)
osc.send('/live/song/set/record_mode', 1); time.sleep(0.3)
r = osc.query('/live/song/get/record_mode', timeout=2.0)
if not r or not r[0]: print("✗ Record failed"); osc.disconnect(); sys.exit(1)
print("✓ Recording")

start = time.time(); last = 0; updates = 0; bar_count = -1
try:
    while time.time() - start < TOTAL + 2:
        now = time.time() - start
        if now - last < SIXTEENTH / 2: time.sleep(0.01); continue
        last = now
        
        beat_in_song = now / SEC_PER_BEAT
        bar = int(beat_in_song / 4)
        beat_in_bar = beat_in_song % 4
        sixteenth_in_bar = int(beat_in_bar * 4)  # 0-15
        
        if bar != bar_count:
            bar_count = bar
            if bar % 8 == 0:
                print(f"  [bar {bar}/{BARS}]")
        
        # Check if we're on a syllable hit (16th) or sub hit (8th)
        is_syllable = sixteenth_in_bar in SYLLABLE_HITS_16TH
        is_sub = sixteenth_in_bar in SUB_HITS_8TH
        syllable_idx = SYLLABLE_HITS_16TH.index(sixteenth_in_bar) if is_syllable else -1
        
        for ti, di, pi, syl_vals, has_jitter in params:
            if is_syllable and syllable_idx >= 0:
                # Syllable hit: set to the syllable's value
                val = syl_vals[syllable_idx]
            elif is_sub and has_jitter:
                # 8th-note sub hit: slight variation on nearest syllable
                prev_syl = max(0, sixteenth_in_bar // 4 - 1)
                next_syl = min(3, sixteenth_in_bar // 4)
                val = (syl_vals[prev_syl] + syl_vals[next_syl]) / 2
                # φ-wobble
                val += (syl_vals[next_syl] - syl_vals[prev_syl]) * 0.2 * math.sin(beat_in_song * phi)
            else:
                continue  # only update on hits
            
            try: osc.send('/live/device/set/parameter/value', ti, di, pi, float(val))
            except: pass
        
        updates += 1

except KeyboardInterrupt: print("\nInterrupted")

osc.send('/live/song/set/record_mode', 0); time.sleep(0.2)
osc.send('/live/song/set/arrangement_overdub', 0); osc.disconnect()
print(f"✓ {updates} rhythmic updates — 'FU-ture-IS-next' pattern across {len(params)} params.")
