#!/opt/homebrew/bin/python3
"""
Tier 3 Arrangement Builder — clip + notes + automation in one call.

Usage:
  from arrangement_builder import build_clip_with_automation
  build_clip_with_automation(track=1, slot=2, bars=8,
      notes=[(36, 0.0, 1.0, 100), ...],
      automation={"EQ Eight": {"1 Gain A": [(0.0, 0.0), (16.0, 1.0)]}})
"""
import sys, time
sys.path.insert(0, "/Users/warrenhayes/Documents/Codex/ableton-agent/src")
from ableton_agent.client import AbletonOSC


def build_clip_with_automation(track, slot, bars, notes=None, automation=None, osc=None):
    """
    Create a clip, optionally add MIDI notes, optionally add automation curves.
    
    Args:
        track: track index
        slot: clip slot index
        bars: length in bars
        notes: flat list [pitch, start, dur, vel, mute, ...] or None
        automation: dict of {device_name: {param_name: [(time, value, slope?), ...]}}
        osc: existing AbletonOSC connection (creates one if None)
    
    Returns dict with results.
    """
    should_close = osc is None
    if osc is None:
        osc = AbletonOSC()
        osc.connect()
        time.sleep(0.3)
    
    result = {"track": track, "slot": slot, "bars": bars, "notes": 0, "automation": {}}
    
    # 1. Create clip
    osc.send('/live/clip_slot/create_clip', track, slot, bars * 4.0)
    time.sleep(0.3)
    
    # 2. Add notes
    if notes:
        chunks = [notes[i:i+250] for i in range(0, len(notes), 250)]
        for chunk in chunks:
            osc.send('/live/clip/add/notes', track, slot, *chunk)
        time.sleep(0.3)
        result["notes"] = len(notes) // 5
    
    # 3. Add automation
    if automation:
        for dev_name, params in automation.items():
            for param_name, steps in params.items():
                r = osc.query('/live/clip/automation/create', track, slot, dev_name, param_name, timeout=2.0)
                if len(r) < 3 or r[2] != 'ok':
                    result["automation"][f"{dev_name}/{param_name}"] = f"create failed: {r}"
                    continue
                
                for step in steps:
                    t, v = step[0], step[1]
                    s = step[2] if len(step) > 2 else 0.0
                    osc.send('/live/clip/automation/insert_step', track, slot, 0, t, v, s)
                
                result["automation"][f"{dev_name}/{param_name}"] = len(steps)
    
    # 4. Set loop + fire
    osc.send('/live/clip/set/loop_start', track, slot, 0.0)
    osc.send('/live/clip/set/loop_end', track, slot, bars * 4.0)
    osc.send('/live/clip/fire', track, slot)
    
    if should_close:
        osc.disconnect()
    
    result["ok"] = True
    return result


def build_arrangement(tracks_config, osc=None):
    """
    Build multiple clips with automation across tracks.
    
    Args:
        tracks_config: dict of {track_index: {slot, bars, notes, automation}}
        osc: existing connection
    
    Returns dict of {track: result}.
    """
    should_close = osc is None
    if osc is None:
        osc = AbletonOSC()
        osc.connect()
        time.sleep(0.3)
    
    results = {}
    for track, config in tracks_config.items():
        results[track] = build_clip_with_automation(
            track=track,
            slot=config.get("slot", 0),
            bars=config.get("bars", 8),
            notes=config.get("notes"),
            automation=config.get("automation"),
            osc=osc
        )
    
    if should_close:
        osc.disconnect()
    
    return results
