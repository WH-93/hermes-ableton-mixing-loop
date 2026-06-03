#!/usr/bin/env python3
"""
Bridge spectral analysis engine — emits JSON findings for agent-driven control.

Architecture:
  Receiver thread:  binds UDP 9880, continuously reads spectral packets,
                    updates a thread-safe latest_spectrum buffer.
  Main thread:      grabs latest spectrum (non-blocking), periodically spawns
                    BlackHole validation, emits JSON findings to stdout.
  NO control logic: No parameter writes, no device discovery, no candidate
                    scoring. All adjustments go through ableton-agent OSC.

Output: JSON lines to stdout — one finding per significant event.
  {"type": "validation", "iteration": 10, "elapsed": 5.2, "band_issues": [...], "biggest": {...}}
  {"type": "spectral", "iteration": 15, "bands": {...}}

Usage:
  bridge_loop.py --refs 20,23 -n 50           # output JSON findings
  bridge_loop.py --list-refs                   # list reference tracks
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

# ─── Config ───
PYTHON = "/Users/warrenhayes/mlx-env/bin/python"
ANALYZER = os.path.expanduser("~/.hermes/scripts/audio_analyzer.py")
PROFILE = os.path.expanduser("~/.hermes/data/deepspace_reference_profile.json")
REF_DIR = os.path.expanduser("~/Desktop/Deepspace reference tracks")
MIXING = os.path.expanduser("~/.hermes/scripts")
sys.path.insert(0, MIXING)

from mixing import (
    find_biggest_deviation, map_band_to_fix, scale_delta_for_sigmas,
    ROLE_TO_CATEGORY, parse_track_role, get_track_category, category_matches_recommendation,
    GAIN_CEILINGS, GAIN_FLOORS, PROPORTIONAL_GAIN, SKIP_BRIDGE_THRESHOLD,
)

BRIDGE_RX = 9880
VALIDATION_INTERVAL = 10  # BlackHole ground truth every N iterations
BANDS = ['sub', 'bass', 'low_mid', 'mid', 'high_mid', 'presence', 'air']
BAND_INDEX_MAP = {'sub': 0, 'bass': 1, 'low_mid': 2, 'mid': 3, 'high_mid': 4, 'presence': 5, 'air': 6}

# EQ Eight has 8 filters. Map each spectral band to the closest filter number.
BAND_TO_EQ_FILTER = {
    'sub': 1,        # 20-60Hz
    'bass': 2,       # 60-120Hz
    'low_mid': 3,    # 120-250Hz
    'mid': 5,        # 250-2000Hz
    'high_mid': 6,   # 2000-6000Hz
    'presence': 7,   # 6000-12000Hz
    'air': 8,        # 12000Hz+
}


def sigmas_to_q(sigmas):
    """Map spectral deviation magnitude to EQ Q (resonance).
    Big gap → wide Q (low resonance) to cover more frequencies.
    Small gap → narrow Q (high resonance) for surgical fix.
    Returns resonance value 0.0-1.0 (EQ Eight: 0=wide, 1=narrow)."""
    if sigmas > 8:
        return 0.15
    elif sigmas > 4:
        return 0.30
    elif sigmas > 2:
        return 0.50
    elif sigmas > 1:
        return 0.70
    return None


# ═══════════════════════════════════════════
# REFERENCE TRACK SELECTION
# ═══════════════════════════════════════════

def list_reference_tracks():
    if not os.path.isdir(REF_DIR):
        return []
    tracks = []
    for f in sorted(os.listdir(REF_DIR)):
        if f.startswith("."):
            continue
        fp = os.path.join(REF_DIR, f)
        if os.path.isdir(fp):
            continue
        ext = f.lower().rsplit(".", 1)[-1] if "." in f else ""
        if ext in ("wav", "mp3", "flac", "aiff", "aif"):
            tracks.append(fp)
    return tracks


def print_reference_tracks(tracks):
    print(f"\n  {len(tracks)} reference tracks in {REF_DIR}:")
    for i, fp in enumerate(tracks):
        name = os.path.basename(fp)
        print(f"  [{i+1:2d}] {name}")


def build_profile_from_indices(track_paths, indices):
    cache_dir = os.path.expanduser("~/.hermes/data/deepspace_per_track")
    selected = []
    for idx in sorted(set(indices)):
        if 1 <= idx <= len(track_paths):
            selected.append(track_paths[idx - 1])
    if not selected:
        return None

    print(f"\n  Building profile from {len(selected)} track(s):", file=sys.stderr)
    analyses = []

    for fp in selected:
        name = os.path.basename(fp)
        cache_path = os.path.join(cache_dir, name + '.json')
        print(f"    - {name}", end='', file=sys.stderr)

        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    analyses.append(json.load(f))
                print("  [cached]", file=sys.stderr)
                continue
            except Exception:
                pass

        print("  [analyzing...]", end='', flush=True, file=sys.stderr)
        try:
            r = subprocess.run(
                [PYTHON, ANALYZER, "analyze", fp],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                analyses.append(json.loads(r.stdout))
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, 'w') as f:
                    f.write(r.stdout)
                print(" ✓", file=sys.stderr)
            else:
                print(" ✗ failed", file=sys.stderr)
        except Exception as e:
            print(f" ✗ {e}", file=sys.stderr)

    if not analyses:
        return None

    import numpy as np
    numeric_keys = [
        "lufs_integrated", "peak_db", "rms_db", "crest_factor_db",
        "spectral_centroid_mean", "spectral_rolloff_mean", "spectral_bandwidth_mean",
        "rms_min_db", "rms_max_db", "rms_range_db",
        "stereo_width", "stereo_correlation", "sub_ratio",
    ]

    profile = {"num_tracks": len(analyses), "files": [os.path.basename(fp) for fp in selected]}

    for key in numeric_keys:
        vals = [a[key] for a in analyses if a.get(key) is not None]
        if vals:
            profile[key] = {
                "median": round(float(np.median(vals)), 2),
                "mean": round(float(np.mean(vals)), 2),
                "std": round(float(np.std(vals)), 2),
                "min": round(float(min(vals)), 2),
                "max": round(float(max(vals)), 2),
            }

    for band_type in ["band_levels", "octave_levels", "band_stereo_width"]:
        agg = {}
        for a in analyses:
            if band_type not in a:
                continue
            for band_name, val in a[band_type].items():
                if band_name not in agg:
                    agg[band_name] = []
                agg[band_name].append(val)
        band_agg = {}
        for band_name, vals in agg.items():
            band_agg[band_name] = {
                "median": round(float(np.median(vals)), 2),
                "mean": round(float(np.mean(vals)), 2),
                "std": round(float(np.std(vals)), 2),
                "min": round(float(min(vals)), 2),
                "max": round(float(max(vals)), 2),
            }
        profile[band_type] = band_agg

    fd, profile_path = tempfile.mkstemp(suffix='.json', prefix='ref_profile_')
    os.close(fd)
    with open(profile_path, 'w') as f:
        json.dump(profile, f, indent=2)

    print(f"    ✓ Profile saved: {profile_path}", file=sys.stderr)
    return profile_path


# ═══════════════════════════════════════════
# SPECTRAL RECEIVER — background thread
# ═══════════════════════════════════════════

class SpectralReceiver(threading.Thread):
    """Continuously reads spectral packets from UDP 9880 in a background thread."""

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._latest = None
        self._timestamp = 0.0
        self._count = 0
        self._running = False
        self._sock = None

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', BRIDGE_RX))
        self._sock.settimeout(0.5)
        self._running = True

        while self._running:
            try:
                data, _ = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break

            null = data.find(b'\x00')
            if null < 0:
                continue
            addr = data[:null].decode('ascii', errors='replace')
            if addr != '/spectral_shape':
                continue

            pos = (null + 4) & ~3
            tag_end = data.find(b'\x00', pos)
            if tag_end < 0:
                continue
            pos = (tag_end + 4) & ~3

            values = []
            for _ in range(7):
                if pos + 4 > len(data):
                    break
                values.append(struct.unpack('>f', data[pos:pos+4])[0])
                pos += 4

            if len(values) == 7:
                with self._lock:
                    self._latest = values
                    self._timestamp = time.time()
                    self._count += 1

    def get_latest(self):
        with self._lock:
            return self._latest, self._timestamp, self._count

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ═══════════════════════════════════════════
# BLACKHOLE VALIDATION — async subprocess
# ═══════════════════════════════════════════

class AsyncValidation:
    """Spawn ffmpeg capture + analysis in background."""

    def __init__(self, profile_path=None):
        self._process = None
        self._wav_path = None
        self._profile_path = profile_path or PROFILE

    def start(self):
        fd, self._wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)

        self._process = subprocess.Popen(
            ['ffmpeg', '-y', '-f', 'avfoundation', '-i', ':2',
             '-t', '4', '-ar', '22050', '-ac', '2', '-c:a', 'pcm_s16le',
             self._wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._start_time = time.time()

    def poll(self):
        if self._process is None:
            return None
        if self._process.poll() is None:
            return None
        if self._process.returncode != 0:
            self._cleanup()
            return {"error": f"ffmpeg exited {self._process.returncode}"}

        try:
            r = subprocess.run(
                [PYTHON, ANALYZER, 'compare', self._wav_path, self._profile_path],
                capture_output=True, text=True, timeout=60,
            )
            self._cleanup()
            if r.returncode == 0:
                return json.loads(r.stdout)
            return {"error": f"analyzer exit {r.returncode}", "stderr": r.stderr[:500]}
        except Exception as e:
            self._cleanup()
            return {"error": str(e)}

    def _cleanup(self):
        if self._wav_path and os.path.exists(self._wav_path):
            try:
                os.unlink(self._wav_path)
            except Exception:
                pass
        self._process = None
        self._wav_path = None

    def elapsed(self):
        if self._start_time:
            return time.time() - self._start_time
        return 0

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None


# ═══════════════════════════════════════════
# MAIN ANALYSIS LOOP — JSON output only
# ═══════════════════════════════════════════

def emit(obj):
    """Write JSON finding to stdout."""
    print(json.dumps(obj))
    sys.stdout.flush()


def run_analysis_loop(iterations=50, validate_every=VALIDATION_INTERVAL, profile_path=None):
    profile_path = profile_path or PROFILE

    print("═" * 60, file=sys.stderr)
    print("Bridge Analysis Engine — agent-driven control mode", file=sys.stderr)
    print(f"  Max iterations: {iterations}", file=sys.stderr)
    print(f"  Validate every: {validate_every}", file=sys.stderr)
    print(f"  Output: JSON lines to stdout", file=sys.stderr)
    print("═" * 60, file=sys.stderr)

    # 1. Start spectral receiver thread
    receiver = SpectralReceiver()
    receiver.start()
    time.sleep(0.3)
    if not receiver.is_alive():
        emit({"type": "error", "message": "Receiver thread failed to start"})
        return

    _, _, init_count = receiver.get_latest()
    if init_count == 0:
        emit({"type": "error", "message": "No spectral packets received. Is Ableton playing?"})
        receiver.stop()
        return

    print(f"\n✓ Receiver alive — {init_count} packets buffered\n", file=sys.stderr)

    # 2. State
    last_validation_deviation = None
    prev_count = init_count
    validation = None
    prev_band_issues = None
    count = init_count

    for i in range(iterations):
        t0 = time.time()

        # ── Check for completed validation ──
        if validation is not None:
            result = validation.poll()
            if result is not None:
                if 'error' not in result:
                    band_issues = result.get('band_issues', [])
                    biggest = find_biggest_deviation(band_issues)

                    finding = {
                        "type": "validation",
                        "iteration": i + 1,
                        "elapsed": round(validation.elapsed(), 1),
                        "band_issues": band_issues,
                        "biggest": {
                            "band": biggest[0],
                            "direction": biggest[1],
                            "sigmas": biggest[2],
                        } if biggest else None,
                        "within_deadband": biggest is None,
                    }
                    emit(finding)

                    # Track improvement/worsening
                    if last_validation_deviation and biggest:
                        old_band, old_dir, old_sigmas = last_validation_deviation
                        if old_band == biggest[0] and old_dir == biggest[1]:
                            trend = "improved" if biggest[2] < old_sigmas else "worsened"
                            print(f"     {trend}: {old_band} {old_sigmas:.1f}σ → {biggest[2]:.1f}σ", file=sys.stderr)

                    last_validation_deviation = biggest
                    prev_band_issues = band_issues
                else:
                    emit({"type": "error", "message": result['error'], "iteration": i + 1})
                validation = None

        # ── Get latest spectrum ──
        spectral, ts, count = receiver.get_latest()
        if spectral is None:
            time.sleep(0.005)
            continue

        # ── Spawn validation if due ──
        if (i + 1) % validate_every == 0 and validation is None:
            validation = AsyncValidation(profile_path)
            validation.start()
            print(f"  ⏳ BlackHole validation started...", file=sys.stderr)

        # ── Periodic spectral sample (every ~2s, ~100 frames) ──
        if count - prev_count >= 100:
            band_dict = dict(zip(BANDS, [round(v, 4) for v in spectral]))
            emit({
                "type": "spectral",
                "iteration": i + 1,
                "timestamp": round(ts, 2),
                "frame_count": count,
                "bands": band_dict,
            })
            prev_count = count

        # Progress to stderr
        if i % 20 == 0:
            status = f"[{i+1:3d}] frames={count}"
            if validation and validation.running:
                status += f" validating({validation.elapsed():.0f}s)"
            if last_validation_deviation:
                b, d, s = last_validation_deviation
                status += f" target={b} {d}({s:.0f}σ)"
            print(status, file=sys.stderr)

        time.sleep(0.005)

    # ── Final validation ──
    if validation and validation.running:
        print("Waiting for final validation...", file=sys.stderr)
        while validation.running:
            time.sleep(0.5)
        result = validation.poll()
        if result and 'error' not in result:
            band_issues = result.get('band_issues', [])
            biggest = find_biggest_deviation(band_issues)
            emit({
                "type": "validation",
                "iteration": iterations,
                "elapsed": round(validation.elapsed(), 1),
                "band_issues": band_issues,
                "biggest": {
                    "band": biggest[0],
                    "direction": biggest[1],
                    "sigmas": biggest[2],
                } if biggest else None,
                "within_deadband": biggest is None,
                "final": True,
            })

    emit({"type": "complete", "iterations": iterations, "frames_received": count})
    receiver.stop()


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Bridge spectral analysis engine — JSON output')
    parser.add_argument('-n', '--iterations', type=int, default=50,
                        help='Max iterations (default: 50)')
    parser.add_argument('-v', '--validate-every', type=int, default=VALIDATION_INTERVAL,
                        help=f'Validation interval (default: {VALIDATION_INTERVAL})')
    parser.add_argument('--refs', type=str, default=None,
                        help='Comma-separated reference track indices (e.g. "3,5,12")')
    parser.add_argument('--list-refs', action='store_true',
                        help='List available reference tracks and exit')
    args = parser.parse_args()

    if args.list_refs:
        tracks = list_reference_tracks()
        print_reference_tracks(tracks)
        sys.exit(0)

    profile_path = PROFILE
    if args.refs:
        tracks = list_reference_tracks()
        if not tracks:
            print("ERROR: No reference tracks found in", REF_DIR, file=sys.stderr)
            sys.exit(1)
        try:
            indices = [int(x.strip()) for x in args.refs.split(',')]
        except ValueError:
            print("ERROR: --refs must be comma-separated numbers", file=sys.stderr)
            sys.exit(1)
        profile_path = build_profile_from_indices(tracks, indices)
        if not profile_path:
            print("ERROR: Failed to build profile", file=sys.stderr)
            sys.exit(1)

    run_analysis_loop(iterations=args.iterations, validate_every=args.validate_every,
                      profile_path=profile_path)
