#!/opt/homebrew/bin/python3
"""Dorian-Phrygian dual-layer — syllabic rhythm 'future is next' reversed alternation."""
import sys, math, time, random
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

random.seed(42)

HYBRID = [50, 51, 52, 53, 55, 57, 58, 59, 60]
HYBRID_HIGH = [p + 12 for p in HYBRID]
OCTAVE_DOWN = [p - 12 for p in HYBRID]
OCTAVE_DOWN_LOW = [p - 24 for p in HYBRID]

# 'future is next' syllables → 4 positions per bar
# FU(strong) ture(weak) IS(strong) next(weak)
# Reversed: next(weak→strong) IS(strong→weak) ture(weak→strong) FU(strong→weak)
# Alternating: odd beats = long/strong, even beats = short/weak, with 20-40% variation

BASE_PHRASE = [
    (7,90),(5,75),(3,70),(1,65),(0,60),(2,70),(4,80),(6,85),
    (0,85),(6,65),(1,80),(5,70),(2,85),(4,75),(3,80),(7,90),
    (0,70),(2,75),(4,80),(6,85),(7,90),(5,80),(3,75),(1,70),
    (8,95),(0,60),(8,90),(0,55),(6,85),(0,50),(4,80),(0,60),
    (0,70),(1,75),(2,78),(3,80),(4,85),(5,88),(6,90),(7,95),
    (7,85),(5,70),(6,80),(4,65),(5,75),(3,60),(4,70),(2,55),
    (8,95),(6,85),(4,75),(2,65),(0,55),(3,70),(5,80),(7,90),
    (0,90),(4,70),(7,85),(3,65),(5,80),(1,60),(6,75),(0,85),
]

def syllabic_build(phrase, scale, offset_idx=False, layer=0):
    """
    Build notes with reversed syllabic alternation.
    Even positions (0,2,4,6...): long dur + high vel
    Odd positions (1,3,5,7...): short dur + low vel
    Variation: 20-40% from base values
    """
    notes = []
    for i, (pi, vel) in enumerate(phrase):
        idx = (8 - pi) if offset_idx else pi
        pitch = scale[idx]
        pos_in_bar = i % 8
        
        # ── Reversed syllabic pattern ──
        # FU=pos0(long), ture=pos1(short), IS=pos2(long), next=pos3(short)
        # Reversed: pos0(short), pos1(long), pos2(short), pos3(long)
        # And every other position alternates
        is_long = (pos_in_bar % 2 == 1)  # reversed: odd = long, even = short
        
        # Duration: long 0.35-0.45, short 0.15-0.25 (20-40% range)
        dur_var = random.uniform(0.8, 1.2)  # ±20%
        if is_long:
            dur = (0.35 + random.uniform(0, 0.1)) * dur_var
        else:
            dur = (0.15 + random.uniform(0, 0.1)) * dur_var
        
        # Velocity: long=70-95, short=40-65 (20-40% range from base)
        vel_var = random.uniform(0.8, 1.2)
        if is_long:
            vel = int(max(65, min(95, vel + layer * 5)) * vel_var)
        else:
            vel = int(max(35, min(65, vel - 25 - layer * 5)) * vel_var)
        
        notes.extend([pitch, i * 0.5, round(dur, 3), vel, 0])
    
    return notes

osc = AbletonOSC(); osc.connect(); time.sleep(0.3)

layers = [
    (9,  0, syllabic_build(BASE_PHRASE, HYBRID, layer=0),
     "Simpler L1 — forward mid, syllabic rhythm"),
    (9,  1, syllabic_build(BASE_PHRASE, HYBRID_HIGH, layer=1),
     "Simpler L2 — forward high"),
    (10, 0, syllabic_build(BASE_PHRASE, OCTAVE_DOWN, offset_idx=True, layer=0),
     "Sampler L1 — reversed bass, syllabic"),
    (10, 1, syllabic_build(BASE_PHRASE, OCTAVE_DOWN_LOW, offset_idx=True, layer=1),
     "Sampler L2 — reversed sub"),
]

for track, slot, notes, label in layers:
    osc.send('/live/clip_slot/create_clip', track, slot, 32.0); time.sleep(0.2)
    for chunk in [notes[i:i+200] for i in range(0, len(notes), 200)]:
        osc.send('/live/clip/add/notes', track, slot, *chunk)
    time.sleep(0.15)
    osc.send('/live/clip/set/loop_start', track, slot, 0.0)
    osc.send('/live/clip/set/loop_end', track, slot, 32.0)
    if slot == 0:
        osc.send('/live/clip/fire', track, slot)
    print(f"✓ [{track}] {label}: {len(notes)//5} notes")

osc.disconnect()
print("\nSyllabic rhythm: odd 8ths=long+hot, even 8ths=short+cool. ±20-40% variation.")
print("Scene 1 playing, scene 2 primed.")
