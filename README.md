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
- Reference tracks directory

## Usage

```bash
python3 mix_loop.py capture [duration]   # analyze only, print issues
python3 mix_loop.py fix [duration]       # one capture→analyze→apply cycle
python3 mix_loop.py loop [n] [duration]  # n iterations with convergence tracking
python3 mix_loop.py scan                 # show current session device layout
python3 mix_loop.py history              # show iteration history
python3 mix_loop.py analyze <file>       # compare a file against reference profile
```

## Safety Features (v2)

- **Gain ceiling**: Parameters have hard maximums — never exceed
- **Deadband**: No adjustment if within 0.8 sigma of reference
- **Proportional control**: Deltas shrink each iteration
- **Red-line protection**: Blocks all gain increases if peak > -0.5 dBFS
- **Scan caching**: Device layout cached for 5 min across iterations
- **Project-agnostic**: Scans session for device types, not hardcoded track indices

## Reference Profile

Built from 31 deep techno tracks (Alarico, Chlär, Rene Wise, Setaoc Mass, Benza, etc.).
Stored in `reference_profile.json`.

## Architecture

- `mix_loop.py` — Main loop: capture, analyze, compare, apply
- `audio_analyzer.py` — Spectral/dynamic/stereo/LUFS analysis engine
- `reference_profile.json` — Aggregate reference profile from 31 tracks
- LivePilot Remote Script (TCP 9878) — Device parameter control

## Known Limitations

- LivePilot TCP calls take ~3s each — full session scan is ~150s (cached after first run)
- Device matching targets first device of each type (e.g., always KICK's EQ Eight)
- Some recommendations go unmapped if no matching device found
