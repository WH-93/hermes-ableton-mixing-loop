---
name: hermes-ableton-mixing-loop
description: "Closed-loop mixing: BlackHole capture, spectral analysis, reference comparison, LivePilot parameter adjustment. Async bridge loop with sigma-scaled deltas, candidate scoring, and stuck detection."
version: 3.1.0
---

# Hermes ↔ Ableton Closed-Loop Mixing

Trigger: user wants to mix a track in Ableton with objective audio analysis feedback.

## Two Loop Drivers

### bridge_loop.py — async high-speed (recommended)

Architecture:
```
M4L bridge (UDP 9880 spectral stream) → Receiver thread (non-blocking)
    → Main loop: analyze → map fix → UDP set_param (2ms)
    → BlackHole validation (async subprocess, every N iterations)
```

Two operating modes:
- **Aggressive** (>20σ): Sigma-scaled deltas, skip bridge verification, apply-and-trust
- **Normal** (≤20σ): Bridge direction tracking, smaller deltas, surgical adjustments

```bash
bridge_loop.py --list-refs              # Numbered list of 30 reference tracks
bridge_loop.py --refs 12,20,24 -n 30    # Run against specific tracks
```

### mix_loop.py — synchronous TCP (legacy)

14 CLI commands: capture, fix, loop, target, presets, etc.

## Prerequisites

- BlackHole 2ch, Ableton output routed to it
- LivePilot Remote Script (port 9878)
- LivePilot Analyzer M4L device on master track (UDP 9880/9881)
- Python: /Users/warrenhayes/mlx-env/bin/python

## Six Tool Improvements (v3.1)

### 1. Sigma-Scaled Deltas
```python
scale_delta_for_sigmas(delta_base, sigmas)
# 2σ → 1x, 8σ → 2x, 20σ+ → 5x
```
Big gaps get aggressive moves. Small gaps stay surgical.

### 2. Aggressive Mode (Skip Bridge Verification)
Above 20σ, bridge direction tracking is noise — skip IMPROVING/WORSE/reverse dance. Apply fixes and verify on next BlackHole validation.

### 3. Candidate Scoring by Tag Match
Tracks scored by role tag relevance to band name:
- Role matches band ("bass" → "bass BASS-FM"): 5 points
- Same category (low_end → KICK): 2 points
- Untagged: 0 points

Bass fixes now prefer bass BASS-FM over KICK. Air fixes route to hats, not KICK.

### 4. Stuck Detection + Escalation
- Detects ceiling/floor hits (no room to move)
- Counts consecutive stuck iterations
- Labels [STUCK x5] in output
- After 5 stuck iterations, cycles to next candidate track or fix

### 5. Validation Comparison
Compares consecutive BlackHole validations:
- `✓ improved: bass 46σ → 31σ`
- `✗ worse: bass 31σ → 46σ`

Ground truth replaces noisy bridge direction tracking.

### 6. Validation Spawn Fix
Moved to top of each iteration so aggressive mode doesn't skip periodic BlackHole checks.

## Reference Tracks

30 tracks at `~/Desktop/Deepspace reference tracks/`:
- Alarico, Benza, Chlar, No Valentia, Vilchezz, and others
- Profile cache at `~/.hermes/data/deepspace_per_track/` (instant load)

## Category Routing

| Spectral band | Category | Example tracks |
|---|---|---|
| sub, bass | low_end | bass BASS-FM, KICK, HARDGROOVE |
| presence, air | hi_freq | hats 909-HARDCORE |
| low_mid, mid | synth, mid | PAD, LEAD, CHORD |

## Band-to-EQ-Filter & Q Mapping

| Band | EQ filter | >8σ Q | 4-8σ Q | 2-4σ Q | 1-2σ Q |
|---|---|---|---|---|---|
| sub | 1 | 0.15 | 0.30 | 0.50 | 0.70 |
| bass | 2 | 0.15 | 0.30 | 0.50 | 0.70 |
| low_mid | 3 | 0.15 | 0.30 | 0.50 | 0.70 |
| mid | 5 | 0.15 | 0.30 | 0.50 | 0.70 |
| high_mid | 6 | 0.15 | 0.30 | 0.50 | 0.70 |
| presence | 7 | 0.15 | 0.30 | 0.50 | 0.70 |
| air | 8 | 0.15 | 0.30 | 0.50 | 0.70 |

## Safety Features

- EQ8 bipolar clamp [-1, +1]
- Ceiling/floor detection with stuck escalation
- Param cache updated after every write
- ensure_device() adds devices via LivePilot if missing
- Deadband: no adjustment within 0.8σ

## Architecture

```
mixing.py          (470 lines) — shared logic, sigma scaling, category routing
bridge_loop.py     (960 lines) — async UDP receiver + aggressive/normal modes
mix_loop.py        (380 lines) — legacy TCP transport
audio_analyzer.py  (520 lines) — analysis engine
orchestrator.py    (310 lines) — Hermes MCP transport
```

## Known Limitations

- Bridge at 50Hz can't detect <0.05 changes — aggressive mode handles this
- Stuck escalation needs per-fix-index support in map_band_to_fix (coming)
- Reference profile is static — no live capture
- No subjective override channel yet
