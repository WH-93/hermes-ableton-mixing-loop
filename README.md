# Hermes ↔ Ableton Closed-Loop Mixing

AI-assisted mixing loop: capture Ableton output via BlackHole → ratio-based spectral analysis → greedy single-shot optimization → LivePilot parameter control.

## Architecture

```
audio_analyzer.py    520 lines   Pure analysis (librosa STFT, LUFS, ratio comparison)
mixing.py            350 lines   Shared logic (120+ role tags, presets, greedy, safety)
mix_loop.py          380 lines   Raw TCP transport + standalone CLI (14 commands)
orchestrator.py      310 lines   Hermes MCP transport (imports mixing, no transport calls)
tests/               4 files     63 unit tests

LivePilot patches (applied to Remote Script):
  devices.py   → get_all_device_parameters (150s scan → 3s)
  tracks.py    → get_all_track_names (90s roles → 1s)
  server.py    → FAST_WRITE_COMMANDS (skip 100ms settle delay)
```

## Quick Start

```bash
# 1. Check your track roles (fast — uses get_all_track_names)
python3 mix_loop.py roles

# 2. Analyze without changing anything
python3 mix_loop.py capture 4

# 3. Run one fix cycle (analyze + apply)
python3 mix_loop.py fix 4

# 4. Run 3 iterations with convergence tracking
python3 mix_loop.py loop 3 4

# 5. Apply a preset to a specific track
python3 mix_loop.py target "kick" aggressive
```

## Commands

| Command | Description |
|---------|-------------|
| `capture [s]` | Analyze only, print JSON (band_issues, recommendations). Default 4s. |
| `fix [s]` | One capture→analyze→apply cycle. Auto-snapshots. |
| `loop [n] [s]` | n iterations with convergence tracking, time budgets, frozen scan. |
| `target <id> <preset>` | Apply preset to track by index, name, or role tag. |
| `presets` | List all 10 available presets. |
| `roles` | Show all tracks with role classification. |
| `scan` | Show all devices with role tags. |
| `snapshot` | Save all device params for rollback. |
| `rollback` | Restore all params from last snapshot. |
| `analyze <file>` | Compare WAV/MP3/FLAC against reference profile. |
| `history` | Show iteration history. |
| `clear-history` | Reset iteration history. |

## Track Role Tagging

**Convention: first word of track name = role tag.** Case-insensitive, plural-aware.

120+ tags across 7 categories. Examples: `kick punchy 808`, `hats 909 minimal`, `bass FM dark`, `pad warm chords`, `group drums`.

Full tag list in `mixing.py` → `ROLE_TO_CATEGORY`. Run `roles` command to audit.

## Safety Features

- **Role-based targeting**: Hits the right track by role tag
- **Ratio-based comparison**: Level-independent — compares band relationships, not absolute levels
- **Greedy single-shot**: One fix per iteration — no conflicting changes
- **Gain ceiling**: Parameters have hard maximums
- **Deadband**: No adjustment within 0.8 sigma of reference
- **Proportional control**: Deltas shrink each iteration
- **Red-line protection**: Blocks gain increases if peak > -0.5 dBFS
- **Parameter verification**: Reads actual value from set_device_parameter response (no extra call)
- **Time budgets**: 120s loop total, 20s per iteration (NASA Rule 2)
- **Frozen scan**: One scan at loop start, reused across all iterations
- **Snapshot/rollback**: Auto-snapshot before fix/loop, rollback anytime

## LivePilot Patches

Applied to `~/Music/Ableton/User Library/Remote Scripts/LivePilot/`. Requires Ableton restart.

| File | Command | Impact |
|------|---------|--------|
| `devices.py` | `get_all_device_parameters` | 150s scan → 3s |
| `tracks.py` | `get_all_track_names` | 90s roles → 1s |
| `server.py` | FAST_WRITE_COMMANDS | 100ms settle → 0ms |
| `mix_loop.py` | response value check | 3s verify → 0ms |

All scripts auto-detect if patches are loaded and fall back gracefully.

## Known Limitations

- LivePilot LOM calls take ~3s each — not fixable in Remote Script (Ableton API limitation)
- Reference profile is 31 mastered deep techno tracks at -7.5 LUFS
- Ratio-based comparison helps but mastered vs unmastered is never exact
- Track role tagging requires manually renaming tracks in Ableton
