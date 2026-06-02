# Hermes ↔ Ableton Closed-Loop Mixing

AI-assisted mixing loop: capture Ableton output → spectral analysis → category routing → EQ adjustment with Q mapping.

## Architecture

```
audio_analyzer.py    520 lines   Pure analysis (librosa STFT, LUFS, ratio comparison)
mixing.py            450 lines   Shared logic (120+ role tags, presets, greedy, safety)
bridge_loop.py       850 lines   Async UDP receiver thread + TCP fallback (high-speed)
mix_loop.py          380 lines   Raw TCP transport + standalone CLI (14 commands, legacy)
orchestrator.py      310 lines   Hermes MCP transport (imports mixing, no transport calls)
tests/               4 files    63 unit tests

LivePilot patches (applied to Remote Script):
  devices.py   → get_all_device_parameters (150s scan → 3s)
  tracks.py    → get_all_track_names (90s roles → 1s)
  server.py    → FAST_WRITE_COMMANDS (skip 100ms settle delay)

M4L bridge:
  LivePilot_Analyzer.amxd on master track → UDP 9880/9881
  continuous 7-band spectral stream at ~50Hz
  set_param via UDP OSC (2ms vs 3s TCP)
```

## Two Loop Drivers

### bridge_loop.py — high-speed async (recommended)

```
M4L bridge (UDP spectral stream) → Receiver thread (non-blocking)
    → Main loop: analyze → map fix → UDP set_param (2ms)
    → BlackHole validation (async subprocess, every N iterations)
```

Per-iteration: ~5ms. Supports reference track selection, band-to-EQ filter mapping, Q/resonance mapping.

```bash
# List all 31 reference tracks
python bridge_loop.py --list-refs

# Run against specific tracks (cached, instant profile build)
python bridge_loop.py --refs 12,19,24 -n 30

# Run against all tracks (default)
python bridge_loop.py -n 50 -v 10
```

### mix_loop.py — synchronous TCP (legacy)

```bash
python mix_loop.py roles        # Check track roles
python mix_loop.py capture 4    # Analyze without changing
python mix_loop.py fix 4        # One capture→analyze→apply
python mix_loop.py loop 3 4     # 3 iterations
python mix_loop.py target "kick" aggressive  # Apply preset
```

## Category Routing

Spectral bands route fixes to the right track by role tag:

| Spectral band | Category | Example tracks |
|---|---|---|
| sub, bass | low_end | KICK, BASS, SUB, RUMBLE |
| presence, air | hi_freq | HATS, RIDE, CYMBAL, SHIMMER |
| low_mid, mid | synth, mid | PAD, LEAD, CHORD, VOX |
| — | spatial | FX, REVERB, DELAY |

If no categorized track found, falls back to all unmuted tracks.

## Band-to-EQ-Filter & Q Mapping

| Spectral band | EQ filter | Q (>8σ) | Q (4-8σ) | Q (2-4σ) | Q (1-2σ) |
|---|---|---|---|---|---|
| sub | 1 | 0.15 | 0.30 | 0.50 | 0.70 |
| bass | 2 | 0.15 | 0.30 | 0.50 | 0.70 |
| low_mid | 3 | 0.15 | 0.30 | 0.50 | 0.70 |
| mid | 5 | 0.15 | 0.30 | 0.50 | 0.70 |
| high_mid | 6 | 0.15 | 0.30 | 0.50 | 0.70 |
| presence | 7 | 0.15 | 0.30 | 0.50 | 0.70 |
| air | 8 | 0.15 | 0.30 | 0.50 | 0.70 |

Wider spectral gap → wider Q (lower resonance) to cover more frequencies.
Narrow gap → surgical Q.

## Track Role Tagging

**Convention: first word of track name = role tag.** Case-insensitive, plural-aware.

120+ tags across 7 categories. Examples: `kick punchy 808`, `hats 909 minimal`, `bass FM dark`, `pad warm chords`, `group drums`.

Full tag list in `mixing.py` → `ROLE_TO_CATEGORY`.

## Safety Features

- **Category routing**: Air fixes go to hats, bass fixes go to KICK
- **Band-to-filter mapping**: Correct EQ band for each spectral region
- **Q/resonance mapping**: Wider gaps get wider Q
- **EQ8 bipolar clamp**: Gain values clamped [-1, +1] (EQ Eight is bipolar, not unipolar)
- **Relative adjustment**: Current value + delta, not absolute 0.5 + delta
- **Param cache**: Updated after every write for consistent reads
- **ensure_device**: Adds EQ Eight/Utility via LivePilot if track doesn't have one
- **Deadband**: No adjustment within 0.8 sigma
- **Proportional control**: Deltas shrink each iteration

## LivePilot Patches

Applied to `~/Music/Ableton/User Library/Remote Scripts/LivePilot/`. Requires Ableton restart.

| File | Command | Impact |
|------|---------|--------|
| `devices.py` | `get_all_device_parameters` | 150s scan → 3s |
| `tracks.py` | `get_all_track_names` | 90s roles → 1s |
| `server.py` | FAST_WRITE_COMMANDS | 100ms settle → 0ms |

All scripts auto-detect if patches are loaded and fall back gracefully.

## Known Limitations

- Bridge spectral at 50Hz is too noisy for <0.05 gain changes — causes oscillation
- Small deltas (0.02) barely register on 7-band master spectrum
- No subjective override channel yet ("hats too dull")
- Reference profile is static — no live reference capture
- LivePilot TCP calls take ~3s (initial device discovery slow)
