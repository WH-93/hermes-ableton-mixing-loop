---
name: hermes-ableton-mixing-loop
description: "Closed-loop mixing: BlackHole capture, spectral analysis, reference comparison, LivePilot parameter adjustment. Also direct track targeting with presets."
version: 3.0.0
---

# Hermes ↔ Ableton Closed-Loop Mixing

Trigger: user wants to mix a track in Ableton with objective audio analysis feedback, or apply presets to specific tracks.

## Two Loop Drivers

### bridge_loop.py — async high-speed (UDP + TCP)
Location: `~/.hermes/scripts/bridge_loop.py`

Architecture:
```
M4L bridge (UDP 9880 spectral stream) → Receiver thread (non-blocking buffer)
    → Main loop: analyze → map fix → UDP set_param (2ms)
    → BlackHole validation (async subprocess, every N iterations)
    → Bridge spectral tracks direction between validations
```

Per-iteration latency: ~5ms (vs 15-30s for mix_loop.py TCP).

Key features:
- `--list-refs` — numbered list of 31 reference tracks
- `--refs 3,5,12` — targeted profile from specific tracks (cached, instant)
- Category-based track routing (air→hats, bass→KICK, etc.)
- Band-to-EQ-filter mapping (bass→filter 2, air→filter 8)
- Q/resonance mapped to spectral gap width (sigmas→Q)
- ensure_device() adds EQ Eight via LivePilot if missing
- Per-track analysis cache at `~/.hermes/data/deepspace_per_track/`

### mix_loop.py — synchronous TCP (legacy)
Location: repo root

14 CLI commands: capture, fix, loop, target, presets, roles, scan, snapshot, rollback, analyze, history, clear-history.

## Prerequisites

- BlackHole 2ch installed (virtual audio driver)
- Ableton output routed to BlackHole 2ch in Preferences → Audio
- LivePilot Remote Script running (port 9878)
- LivePilot Analyzer M4L device on master track (for bridge loop — port 9880/9881)
- Python deps: /Users/warrenhayes/mlx-env/bin/python (librosa, numpy, scipy, soundfile, pyloudnorm)
- Reference tracks at ~/Desktop/Deepspace reference tracks/ (31 deep techno tracks)
- Profile cache at ~/.hermes/data/deepspace_per_track/ (30/31 pre-analyzed)

## Track Role Tagging

**Convention: first word of track name = role tag.** Case-insensitive. Plurals handled. Trailing punctuation stripped.

120+ valid tags across 7 categories. See mixing.py → ROLE_TO_CATEGORY.

## Category Routing (v3)

The loop routes fixes to the right track by spectral band → category:

| Spectral band | Category | Example tracks |
|---|---|---|
| sub, bass | low_end | KICK, BASS, SUB, RUMBLE |
| presence, air | hi_freq | HATS, RIDE, CYMBAL, SHIMMER |
| low_mid, mid | synth, mid | PAD, LEAD, CHORD, VOX |
| — | percussion | PERC, SNARE, CLAP, TOMS |
| — | spatial | FX, REVERB, DELAY, NOISE |

If no categorized track found, falls back to all unmuted tracks.

## Band-to-EQ-Filter Mapping

| Spectral band | EQ Eight filter | Frequency range |
|---|---|---|
| sub | 1 | 20-60Hz |
| bass | 2 | 60-120Hz |
| low_mid | 3 | 120-250Hz |
| mid | 5 | 250-2000Hz |
| high_mid | 6 | 2000-6000Hz |
| presence | 7 | 6000-12000Hz |
| air | 8 | 12000Hz+ |

## Q/Resonance Mapping

Wider spectral gap → wider Q (lower resonance) to cover more frequencies:

| Sigmas | Q | Effect |
|---|---|---|
| >8σ | 0.15 | Very wide — whole region needs fixing |
| 4-8σ | 0.30 | Wide |
| 2-4σ | 0.50 | Medium |
| 1-2σ | 0.70 | Surgical — narrow |
| <1σ | (none) | Deadband — no adjustment |

## Reference Track Selection

```
bridge_loop.py --list-refs              # Numbered list of all 31 tracks
bridge_loop.py --refs 12,19,24 -n 30    # Run against 3 specific tracks
bridge_loop.py -n 30                    # Run against all 31 (default)
```

Profile built instantly from per-track cache. 30/31 tracks pre-analyzed.

## Architecture (refactored June 2026)

```
mixing.py          (450 lines) — pure logic, zero transport, shared library
bridge_loop.py     (850 lines) — async UDP receiver thread + TCP fallback
mix_loop.py        (380 lines) — raw TCP transport, standalone CLI
audio_analyzer.py  (520 lines) — analysis engine, unchanged
orchestrator.py    (310 lines) — Hermes MCP transport
```

## Safety Features

- **Role-based targeting**: Recommendations hit the right track by role tag
- **Gain ceiling**: Parameters have hard maximums (GAIN_CEILINGS)
- **Deadband**: No adjustment within 0.8 sigma
- **Proportional control**: Deltas shrink each iteration (PROPORTIONAL_GAIN)
- **EQ8 bipolar clamp**: Gain values clamped [-1, 1] (EQ Eight is bipolar)
- **Param cache**: Device params cached across iterations, updated after writes
- **Snapshot/rollback**: mix_loop.py supports auto-snapshot and rollback
- **ensure_device**: Adds EQ Eight/Utility via LivePilot insert_device if missing

## Parameter Pitfalls

- **EQ Eight gain is bipolar** [-1, +1], NOT unipolar [0, 1]. Clamping to 0 floors negative values.
- **Glue Compressor Attack/Ratio/Release** are ENUM steps (0-6, 0-2, 0-6). Intermediate values silently fail.
- **Utility Gain** range is [-1, 1], maps to -∞ to +35dB
- Always verify with get_device_parameters after setting
- Returned `ok: true` does NOT guarantee the value changed

## Known Limitations

- Bridge spectral at 50Hz is too noisy to detect <0.05 gain changes reliably
- Oscillation between IMPROVING/WORSE on small deltas
- Reference profile is static — no live reference capture yet
- No subjective override channel ("hats too dull") yet
- LivePilot TCP calls take ~3s — initial device discovery is slow
