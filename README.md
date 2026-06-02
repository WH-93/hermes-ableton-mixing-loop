# Hermes ↔ Ableton Closed-Loop Mixing

AI-assisted mixing loop: capture Ableton output via BlackHole → spectral analysis → reference track comparison → LivePilot parameter adjustments.

## Pipeline

```
Ableton → BlackHole 2ch → ffmpeg capture → audio_analyzer.py
    → compare vs 31 reference tracks → recommendations
    → LivePilot TCP → set_device_parameter → repeat
```

## Prerequisites

- Ableton Live 12 with LivePilot Remote Script (port 9878)
- BlackHole 2ch virtual audio driver
- Python 3.x with librosa, numpy, scipy, soundfile, pyloudnorm
- Reference tracks directory (optional — ships with 31-track deep techno profile)

## Quick Start

```bash
# 1. Check your track roles
python3 mix_loop.py roles

# 2. Analyze without changing anything
python3 mix_loop.py capture 6

# 3. Run one fix cycle (analyze + apply)
python3 mix_loop.py fix 6

# 4. Run 3 iterations with convergence tracking
python3 mix_loop.py loop 3 6
```

## Commands

| Command | Description |
|---------|-------------|
| `capture [seconds]` | Capture audio, analyze, print issues. No changes applied. |
| `fix [seconds]` | One capture→analyze→apply cycle. |
| `loop [n] [seconds]` | Run n iterations with convergence tracking, trends, and history. |
| `roles` | Show all tracks with their role classification. |
| `scan` | Show all devices in the session with role tags. |
| `analyze <file>` | Compare a WAV/MP3/FLAC/AIFF file against the reference profile. |
| `history` | Show iteration history from previous loop runs. |
| `clear-history` | Reset iteration history. |

## Track Role Tagging

**How it works:** The first word of each track name is parsed as a role tag. This tells the loop which tracks to target for which recommendations.

**Example track names:**
```
kick punchy 808          → role=kick, targets sub/bass adjustments
hats 909 minimal         → role=hats, targets presence/air adjustments
bass FM dark             → role=bass, targets sub/bass adjustments
pad warm chords          → role=pad, targets stereo width/presence
hook swirly phrygian     → role=hook, targets midrange/presence
group drums              → role=group, targets master/compression
perc tribal space        → role=perc, targets percussion presence
fx warehouse reverb      → role=fx, targets spatial/air
```

**Valid role tags and what they target:**

| Role tags | Category | Recommendations targeted |
|-----------|----------|--------------------------|
| `kick`, `bass`, `sub`, `rumble`, `808` | `low_end` | Sub frequencies (20-60Hz), Bass (60-120Hz) |
| `hats`, `hat`, `ride`, `cymbal`, `hihat` | `hi_freq` | Presence (2-6kHz), Air (6-16kHz) |
| `synth`, `pad`, `chord`, `lead`, `hook`, `melody`, `arp` | `synth` | Low-mids, Presence, Stereo width |
| `perc`, `toms`, `tom`, `conga`, `clap`, `snare`, `shaker` | `percussion` | Presence (2-6kHz) |
| `fx`, `reverb`, `delay`, `echo`, `noise`, `riser`, `sweep` | `spatial` | Air (6-16kHz), Stereo width |
| `group`, `bus`, `master`, `mix` | `mix_bus` | Master level, Compression, Dynamic range |
| `vox`, `vocal`, `voice`, `sample` | `mid` | Low-mids, Presence |

**Untagged tracks** (no matching first word) are neutral fallback — they'll be used if no tagged track matches the recommendation.

**Run `python3 mix_loop.py roles`** to see which tracks are tagged and which need renaming.

## Safety Features

- **Role-based targeting**: Recommendations hit the right track, not just the first device found
- **Gain ceiling**: Parameters have hard maximums — never exceed configured limits
- **Deadband**: No adjustment if within 0.8 sigma of reference
- **Proportional control**: Deltas shrink each iteration (factor = 0.5 / (1 + iteration × 0.3))
- **Red-line protection**: Blocks all gain increases if peak > -0.5 dBFS
- **Scan caching**: Device layout cached for 5 minutes across iterations
- **Convergence tracking**: Loop stops if stalled or all issues resolved

## Reference Profile

Built from 31 deep techno tracks (Alarico, Chlär, Rene Wise, Setaoc Mass, Benza, etc.).
Stored in `reference_profile.json`.

## Architecture

- `mix_loop.py` — Main loop: capture, analyze, compare, apply, role targeting
- `audio_analyzer.py` — Spectral/dynamic/stereo/LUFS analysis engine
- `reference_profile.json` — Aggregate reference profile from 31 tracks
- `tests/test_device_targeting.py` — 37 unit tests for role-based device targeting
- LivePilot Remote Script (TCP 9878) — Device parameter control

## Known Limitations

- LivePilot TCP calls take ~3s each — full session scan is ~150s (cached after first run)
- Some recommendations go unmapped if no matching device found on any track
- Track role tagging requires manually renaming tracks in Ableton (first-word convention)
