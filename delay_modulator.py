#!/opt/homebrew/bin/python3
"""Delay modulation engine — organic swell patterns. Run: delay-modulator --duration 360"""
import math, random, sys, time
phi = 1.618033988749895
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC

# Verified indices from actual device queries
TRACKS = {
    6:  {"dev":2, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    8:  {"dev":7, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    12: {"dev":4, "type":"Delay",  "wet":20, "fb":12, "lp":15,               "lt":6,  "rt":7,  "modf":17},
    13: {"dev":2, "type":"Grain",  "wet":6,  "fb":5,  "lp":2,                "lt":10,                    "modf":2},
    14: {"dev":4, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    15: {"dev":1, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    16: {"dev":1, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    17: {"dev":3, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    18: {"dev":3, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    20: {"dev":4, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    21: {"dev":2, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    22: {"dev":2, "type":"Echo",   "wet":52, "fb":16, "lp":31, "hp":29, "lt":2,  "rt":7,  "modf":34, "modr":36, "rvb":43, "rpt":15},
    24: {"dev":1, "type":"FilterDelay",
         "1_freq":3, "1_width":4, "1_time":8, "1_fb":9, "1_pan":10, "1_vol":11,
         "2_freq":14,"2_width":15,"2_time":19,"2_fb":20,"2_pan":21,"2_vol":22,
         "3_freq":25,"3_width":26,"3_time":30,"3_fb":31,"3_pan":32,"3_vol":33},
}

SWELL = 10.0; MOD = 6.0; TICK = 0.5; STEPS = int(SWELL / TICK)  # 20 steps per swell

def wave(t, tid, off=0.0):
    p = (t*phi + tid*0.37 + off) % (2*math.pi)
    return ((math.sin(p) + math.sin(p*phi)*0.5 + math.sin(p*phi*phi)*0.25)/1.75 + 1)/2

def send(osc, ti, di, idx, val):
    try: osc.send('/live/device/set/parameter/value', ti, di, idx, float(val))
    except Exception as e: print(f"  ✗ [{ti}] {idx}: {e}", file=sys.stderr)

def main(dur=360):
    print(f"Delay modulator — {len(TRACKS)} tracks, {dur}s (smooth interpolation)", file=sys.stderr)
    osc = AbletonOSC(); start = time.time()
    sw, mc, ls, lm = 0, 0, -SWELL, -MOD
    tids = sorted(TRACKS)
    try:
        while time.time()-start < dur:
            n = time.time()-start
            
            # ── Interpolation tick ── (runs every TICK seconds)
            sw_frac = sw + min(1.0, (n - ls) / SWELL)  # smooth 0→1 between swell targets
            
            for ti in tids:
                t = TRACKS[ti]; di = t["dev"]
                if t["type"] == "FilterDelay":
                    for line in [1,2,3]:
                        wl = wave(sw_frac, ti, off=line*0.33)
                        send(osc, ti, di, t[f"{line}_freq"], 0.15 + wl*0.7)
                        send(osc, ti, di, t[f"{line}_fb"], 0.05 + wl*0.75)
                        send(osc, ti, di, t[f"{line}_pan"], -1.0 + wl*2.0)
                        send(osc, ti, di, t[f"{line}_time"], 5.0 + wl*15.0)
                else:
                    w = wave(sw_frac, ti)
                    wet = 0.1 + w*0.8; fb = 0.1 + w*0.6; lp = 0.2 + w*0.7
                    if t["type"] == "Grain":
                        send(osc, ti, di, t["wet"], 0.2 + w*0.8)
                        send(osc, ti, di, t["fb"], w*0.5)
                        send(osc, ti, di, t["lp"], 0.3 + w*0.6)
                    else:
                        send(osc, ti, di, t["wet"], wet)
                        send(osc, ti, di, t["fb"], fb)
                        send(osc, ti, di, t["lp"], lp)
            
            # ── New swell target ──
            if n - ls >= SWELL:
                sw += 1; ls = n
            
            # ── Secondary modulation ──
            if n - lm >= MOD:
                mc += 1; lm = n
                active = [ti for ti in tids if (mc*phi + ti) % 1 < 0.6]
                for ti in active:
                    t = TRACKS[ti]; di = t["dev"]; w = wave(mc, ti, 0.5)
                    if t["type"] == "FilterDelay":
                        for line in [1,2,3]:
                            wl = wave(mc, ti, off=line*0.5 + 0.25)
                            send(osc, ti, di, t[f"{line}_width"], 1.0 + wl*6.0)
                            send(osc, ti, di, t[f"{line}_vol"], 0.5 + wl*0.5)
                        continue
                    sec = [k for k in ["hp","modf","modr","rvb","rpt","lt","rt"] if k in t]
                    if sec:
                        k = sec[int(w*len(sec)) % len(sec)]
                        send(osc, ti, di, t[k], 0.1 + w*0.8)
            
            time.sleep(TICK)
    except KeyboardInterrupt: pass
    print(f"\n{sw} swells, {mc} mod cycles", file=sys.stderr)

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--duration", type=int, default=0)
    dur = p.parse_args().duration
    if dur <= 0:
        dur = float('inf')
        print(f"Delay modulator — {len(TRACKS)} tracks, running until terminated", file=sys.stderr)
    main(dur)
