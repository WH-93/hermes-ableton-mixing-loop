# Hermes ↔ Ableton Closed-Loop Mixing

AI-assisted mixing loop: capture Ableton output → spectral analysis → category routing → sigma-scaled EQ adjustment.

## Architecture

```
audio_analyzer.py    520 lines   Pure analysis (librosa STFT, LUFS, ratio comparison)
mixing.py            470 lines   Shared logic (120+ role tags, sigma scaling, safety)
bridge_loop.py       960 lines   Async UDP receiver + aggressive/normal dual-mode
mix_loop.py          380 lines   Raw TCP transport + standalone CLI (14 commands, legacy)
orchestrator.py      310 lines   Hermes MCP transport
tests/               4 files    63 unit tests
```

## Quick Start

```bash
# List reference tracks
python bridge_loop.py --list-refs

# High-speed async loop against specific tracks
python bridge_loop.py --refs 20,23 -n 30 -v 10
```

## Six Tool Improvements (v3.1)

### 1. Sigma-Scaled Deltas
`scale_delta_for_sigmas(delta_base, sigmas)` — 2σ→1x, 8σ→2x, 20σ+→5x. Big gaps get aggressive moves.

### 2. Aggressive Mode
Above 20σ, skip bridge direction tracking entirely. Apply fixes and verify on next BlackHole validation. No IMPROVING/WORSE oscillation.

### 3. Candidate Scoring
Tracks scored by role tag relevance to band name. Bass fixes prefer "bass BASS-FM" (5pts) over "KICK" (2pts).

### 4. Stuck Detection
Detects ceiling hits (no room to move), counts consecutive stuck iterations, labels [STUCK x5], cycles to next candidate.

### 5. Validation Comparison
Tracks improved/worse between consecutive BlackHole validations: `✓ improved: bass 46σ → 31σ`.

### 6. Validation Spawn Fix
Moved to top of loop so aggressive mode doesn't skip periodic ground truth checks.

## Category Routing

| Spectral band | Category | Example tracks |
|---|---|---|
| sub, bass | low_end | bass BASS-FM, KICK |
| presence, air | hi_freq | hats 909-HARDCORE |
| low_mid, mid | synth, mid | PAD, LEAD, CHORD |

## Band-to-EQ-Filter & Q Mapping

Wider spectral gap → wider Q (lower resonance):

| Band | EQ filter | >8σ Q | 4-8σ Q | 2-4σ Q | 1-2σ Q |
|---|---|---|---|---|---|
| sub | 1 | 0.15 | 0.30 | 0.50 | 0.70 |
| bass | 2 | 0.15 | 0.30 | 0.50 | 0.70 |
| low_mid | 3 | 0.15 | 0.30 | 0.50 | 0.70 |
| mid | 5 | 0.15 | 0.30 | 0.50 | 0.70 |
| high_mid | 6 | 0.15 | 0.30 | 0.50 | 0.70 |
| presence | 7 | 0.15 | 0.30 | 0.50 | 0.70 |
| air | 8 | 0.15 | 0.30 | 0.50 | 0.70 |

## Track Role Tagging

**Convention: first word of track name = role tag.** Case-insensitive, plural-aware. 120+ tags across 7 categories.

## Safety Features

- Sigma-scaled deltas (aggressive on big gaps, surgical on small)
- Aggressive mode skip-bridge above 20σ
- EQ8 bipolar clamp [-1, +1]
- Ceiling/floor detection with stuck escalation
- Param cache updated after every write
- ensure_device() adds devices via LivePilot if missing
- Deadband: no adjustment within 0.8σ

## LivePilot Patches

| File | Command | Impact |
|------|---------|--------|
| `devices.py` | `get_all_device_parameters` | 150s scan → 3s |
| `tracks.py` | `get_all_track_names` | 90s roles → 1s |
| `server.py` | FAST_WRITE_COMMANDS | 100ms settle → 0ms |

## Known Limitations

- Bridge at 50Hz can't detect <0.05 changes — aggressive mode handles this
- Stuck escalation needs per-fix-index support (coming)
- No subjective override channel yet
