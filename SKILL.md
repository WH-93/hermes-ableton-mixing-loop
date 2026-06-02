---
name: hermes-ableton-mixing-loop
description: "Closed-loop mixing: BlackHole capture, spectral analysis, reference comparison, LivePilot parameter adjustment. Also direct track targeting with presets."
version: 2.0.0
---

# Hermes ↔ Ableton Closed-Loop Mixing

Trigger: user wants to mix a track in Ableton with objective audio analysis feedback, or apply presets to specific tracks.

## Pipeline Overview

```
Ableton → BlackHole 2ch → ffmpeg capture → audio_analyzer.py
    → ratio-based comparison vs reference profile → greedy single-shot fix
    → LivePilot TCP → set_device_parameter → repeat
```

## Prerequisites

- BlackHole 2ch installed (virtual audio driver)
- Ableton output routed to BlackHole 2ch in Preferences → Audio
- LivePilot Remote Script running (port 9878)
- Python deps in mlx-env: /Users/warrenhayes/mlx-env/bin/python (librosa, numpy, scipy, soundfile, pyloudnorm)
- Reference profile at ~/.hermes/data/deepspace_reference_profile.json (31 deep techno tracks)

## Commands

| Command | Description |
|---------|-------------|
| `capture [seconds]` | Analyze only, print issues. Default 4s. |
| `fix [seconds]` | One capture→analyze→apply cycle. Auto-snapshots. |
| `loop [n] [seconds]` | n iterations with convergence tracking, time budgets, frozen scan. |
| `roles` | Show all tracks with role classification. |
| `scan` | Show all devices with role tags. |
| `snapshot` | Save all device params for later rollback. |
| `rollback` | Restore all params from last snapshot. |
| `target <id> <preset>` | Apply preset to track by index, name, or role. |
| `presets` | List all 10 available presets. |
| `analyze <file>` | Compare WAV/MP3/FLAC file against reference. |
| `history` | Show iteration history. |

## Track Role Tagging (Issue #2)

**Convention: first word of track name = role tag.** Case-insensitive. Plurals handled. Trailing punctuation stripped.

120+ valid tags across 7 categories:

- **LOW END**: kick, kicks, bass, basses, bassline, sub, subs, rumble, rumbles, 808, 909, lowend, low, thump, weight, body
- **HIGH FREQ**: hats, hat, ride, rides, cymbal, cymbals, crash, hihat, hihats, hh, openhat, closedhat, shimmer, sparkle, top, tops
- **SYNTH**: synth, synths, pad, pads, chord, chords, lead, leads, hook, hooks, melody, melodies, arp, arps, arpeggio, stab, stabs, pluck, plucks, drone, drones, texture, textures, keys, key, organ, piano
- **PERCUSSION**: perc, percs, percussion, toms, tom, conga, congas, clap, claps, snare, snares, shaker, shakers, rim, rims, rimshot, cowbell, tambourine, maraca, maracas, triangle, woodblock, block, drum, drums, click, clicks, transient
- **SPATIAL**: fx, fxs, effect, effects, reverb, reverbs, delay, delays, echo, echoes, noise, noises, riser, risers, sweep, sweeps, wash, washes, atmosphere, ambience, ambient, space
- **MIX BUS**: group, groups, bus, busses, buses, master, masters, mix, main, sum
- **MID**: vox, vocal, vocals, voice, voices, sample, samples, chop, chops, phrase, phrases

Example names: `kick punchy 808`, `hats 909 minimal`, `bass FM dark`, `pad warm chords`, `group drums`

Untagged tracks are neutral fallback. Run `roles` command to audit.

## Ratio-Based Spectral Comparison

Level-independent: compares band RELATIONSHIPS not absolute levels. Fixes the bug where quiet mixes (-20 LUFS) looked "weak" against mastered references (-7.5 LUFS).

Six adjacent-band ratios: sub/bass, bass/low_mid, low_mid/mid, mid/high_mid, high_mid/presence, presence/air.

Recommendations: "Sub-to-bass gap is wide (8.5 dB vs ref 2.4 dB). Reduce sub or boost bass."

Full details: `references/ratio-based-comparison.md`

## Greedy Single-Shot (Issue #3)

Instead of applying 7+ conflicting recommendations per iteration, finds the SINGLE biggest band deviation and applies ONE parameter fix. Eliminates oscillation. Cuts per-iteration LivePilot calls from 7×3s to 1×3s.

## Direct Track Targeting (Presets)

`mix_loop.py target <id> <preset>` — apply preset to specific track. Matches by track index, track name (partial), or role tag. Auto-snapshots before applying.

10 presets: aggressive, wider, darker, brighter, punchier, softer, bigger, tighter, warmer, clean.

## Safety Features (V2)

- **Role-based targeting**: Recommendations hit the right track by role tag
- **Gain ceiling**: Parameters have hard maximums
- **Deadband**: No adjustment within 0.8 sigma
- **Proportional control**: Deltas shrink each iteration
- **Red-line protection**: Blocks gain increases if peak > -0.5 dBFS
- **Scan caching**: Device layout cached for 5 min
- **Frozen scan**: One scan at loop start, reused across all iterations
- **Time budgets**: 120s loop total, 20s per iteration (NASA Rule 2)
- **Parameter verification**: Re-reads after set, detects REJECTED enum-step params (NASA Rule 7)
- **Snapshot/rollback**: Auto-snapshot before fix/loop, rollback anytime
- **RMS silence gate**: Exit early if capture is silent

## Parameter Pitfalls

- **batch_set_parameters** uses `name_or_index` key, NOT `name` or `index`
- **Glue Compressor Attack/Ratio/Release** are ENUM steps: Attack={0-6}, Ratio={0-2}, Release={0-6}. Intermediate values silently fail. Our param verification detects this (reports REJECTED).
- **Utility Gain** range is [-1, 1], maps to -∞ to +35dB
- Always verify with get_device_parameters after setting
- Returned `ok: true` does NOT guarantee the value changed

## Current Track State

Warren's cyber techno project (May 31, 2026):
- 142 BPM, D Phrygian dominant, 30 tracks
- Key tracks: KICK[1], 909-HARDCORE[2], BASS-FM[6], TRIBAL-SPACE[12], HARDGROOVE[15], HOOK[16], PAD-MIDI[23], TOMS[24], HATS-N-RIDES[25], CONGA-TRIBAL[26]
- Most tracks route to 1-Group
- Master: clean (no processing)

## Architecture (refactored June 2026)

```
mixing.py        (350 lines) — pure logic, zero transport, shared library
mix_loop.py      (380 lines) — raw TCP transport, standalone CLI
audio_analyzer.py (520 lines) — analysis engine, unchanged
orchestrator.py  (310 lines) — Hermes MCP transport
```

- `mixing.py` — Role tagging, device matching, greedy algorithm, presets, safety limits. Importable by both transport layers.
- `mix_loop.py` — Imports mixing.py. Raw TCP transport. Audio analysis subprocess. 14 CLI commands.
- `audio_analyzer.py` — Spectral/dynamic/stereo/LUFS analysis, ratio-based comparison, reference profile. Zero Ableton dependency.
- `orchestrator.py` — Imports mixing.py. Designed for Hermes MCP transport. Returns action plans — Hermes executes via MCP tools.

API boundary: `capture` command produces JSON with band_issues, recommendations, analysis. Both transports consume the same JSON.

## LivePilot Patches (applied June 2026)

Optimizations to `~/Music/Ableton/User Library/Remote Scripts/LivePilot/`. Requires Ableton restart.

| File | Command | Before | After |
|------|---------|--------|-------|
| `devices.py` | `get_all_device_parameters` | 30+ calls (150s) | 1 call (~3s) |
| `tracks.py` | `get_all_track_names` | 30 calls (90s) | 1 call (~1s) |
| `server.py` | FAST_WRITE_COMMANDS | 100ms settle delay | 0ms (instant) |
| `mix_loop.py` | response value verify | read(3s) + write(3s) = 6s | write(3s) = 3s |

`set_device_parameter` returns actual parameter value in response — we use it for verification instead of a separate `get_device_parameters` call. All scripts auto-detect patches and fall back gracefully.

Patches archived at: `livepilot_devices_patch.py`, `livepilot_tracks_patch.py`, `livepilot_server_patch.py` in repo.

See `references/architecture-transport.md` for full details.

### References

- `references/livepilot-patches.md` — Three LivePilot Remote Script optimizations (get_all_device_parameters, get_all_track_names, FAST_WRITE_COMMANDS). Restart Ableton to activate. Reduces scan from 150s to ~3s.

## Known Limitations

- LivePilot TCP calls take ~3s each — initial scan is slow (cached after first run)
- Device matching may target wrong track if no role tags are set
- Reference profile is 31 mastered deep techno tracks at -7.5 LUFS
- Ratio-based comparison helps but mastered vs unmastered is never exact
