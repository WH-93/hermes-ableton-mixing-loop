# Hermes ↔ Ableton Arrangement Automation

Pure OSC automation tools for Ableton Live 12 via ableton-agent. No MCP, no LivePilot, no spectral analysis — just UDP OSC.

## Architecture

```
ableton-agent (port 11000/11001) → AbletonOSC Remote Script → Live Object Model
                ↑
arrangement_overdub.py    Track volume automation with beat-synced recording
phaser_mod.py             Multi-parameter FX modulation with φ-geometric rates
arrangement_builder.py    Clip + automation envelope module
delay_modulator.py        Continuous delay parameter modulation (13 tracks)
```

## Quick Start

```bash
# Track volume arrangement (6 min, kick+hats bookends)
python arrangement_overdub.py

# Phaser/Flanger modulation (6 min, φ-rate drift)
python phaser_mod.py

# Continuous delay modulation (runs until Ctrl+C)
python delay_modulator.py
```

All scripts beat-sync to Ableton's transport. Position playhead at bar 1, hit play, then run.

## Tools

### arrangement_overdub.py
Streams `/live/track/set/volume` or `/live/device/set/parameter/value` into arrangement view via overdub recording. Waits for bar boundary before engaging. 6-minute default. Configurable curves, tracks, and duration.

### phaser_mod.py
Rich FX automation for Phaser-Flanger (or any device). Automates Dry/Wet, Amount, Feedback, Spread, Env Amount, Spin, Center Freq, Mod Rates, Mod Freqs. Uses φ-geometric ratios so rates continuously drift in and out of sync. Frequency capped to avoid harshness.

### arrangement_builder.py
Python module: `build_clip_with_automation(track, slot, bars, notes, automation)` and `build_arrangement(tracks_config)`. Creates clips, adds MIDI notes, creates automation envelopes, inserts curve points — all in one call.

### delay_modulator.py
Runs until terminated. Every 10s: swells Dry/Wet + Feedback + LP Freq on all delay-bearing tracks with unique φ-wave patterns. Every 6s: modulates secondary params on a rotating subset. Supports Echo, Delay, GrainDelay, FilterDelay (3-line independent). Smooth interpolation between swell targets.

## Remote Script Patches

The system AbletonOSC copy (`/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/MIDI Remote Scripts/AbletonOSC/`) needs these fixes:

1. **pythonosc/parsing/** — missing submodule, copy from `ableton-agent/.venv`, fix absolute import in `osc_types.py`
2. **bool(mute)** — `clip.py` line 153: `mute=bool(mute)` (OSC sends int, Live expects bool)
3. **Automation endpoints** — 5 custom handlers added to `clip.py`: `/live/clip/automation/list`, `create`, `insert_step`, `clear`, `clear_all`

Clear `__pycache__` and restart Ableton after each change.

## TDD Tests

```bash
python test_automation.py   # 6/6 — automation envelope handlers
python test_tier3.py        # 6/6 — full arrangement pipeline
```

## Key Findings

- Bool properties (`arrangement_overdub`, `record_mode`) need `int(1)` not `float(1.0)`
- `create_automation_envelope(param)` takes the parameter directly, not device+param
- `insert_step(time, value, slope)` — three doubles
- `automation_envelopes` is a single-use iterable — must `list()` it
- One parameter can only have one automation envelope total (Live constraint)
- `add_notes`: flatten tuples, chunk ≤80 notes, create clip via `/live/clip_slot/create_clip` first

## Scrapped

The spectral analysis/mixing path (bridge_loop.py, audio_analyzer.py, techno-ui, ws_bridge.py, ws_inject.py) has been abandoned. All current functionality is pure OSC through ableton-agent.
