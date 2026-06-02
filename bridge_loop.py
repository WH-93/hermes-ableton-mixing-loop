#!/usr/bin/env python3
"""
Async bridge-based mixing loop — continuous spectral stream + pipelined analysis.

Architecture:
  Receiver thread:  binds UDP 9880, continuously reads spectral packets,
                    updates a thread-safe latest_spectrum buffer.
  Main thread:      grabs latest spectrum (non-blocking), analyzes, applies fixes.
                    BlackHole validation spawned async — main loop doesn't pause.

Latency per iteration: analysis time + UDP write (~5-20ms)
  vs. old loop:  20× UDP read (~200ms) + analysis + write
  vs. TCP loop:  TCP round-trip (~300ms) + scan + analysis + write (~15-30s)

Bridge spectral stream runs at ~50Hz; we read the latest frame whenever we're ready.
No blocking reads, no artificial delays.
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
    find_biggest_deviation, map_band_to_fix,
    ROLE_TO_CATEGORY, parse_track_role, get_track_category, category_matches_recommendation,
    GAIN_CEILINGS, GAIN_FLOORS, PROPORTIONAL_GAIN,
)

BRIDGE_RX = 9880
BRIDGE_TX = 9881
VALIDATION_INTERVAL = 10  # BlackHole ground truth every N iterations
BANDS = ['sub', 'bass', 'low_mid', 'mid', 'high_mid', 'presence', 'air']
BAND_INDEX_MAP = {'sub': 0, 'bass': 1, 'low_mid': 2, 'mid': 3, 'high_mid': 4, 'presence': 5, 'air': 6}

# EQ Eight has 8 filters. Map each spectral band to the closest filter number.
# Filter 1 = lowest freq (sub), Filter 8 = highest (air).
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
        return 0.15   # very wide — whole region needs fixing
    elif sigmas > 4:
        return 0.30   # wide
    elif sigmas > 2:
        return 0.50   # medium
    elif sigmas > 1:
        return 0.70   # narrow — surgical
    return None        # within deadband


# ═══════════════════════════════════════════
# REFERENCE TRACK SELECTION
# ═══════════════════════════════════════════

def list_reference_tracks():
    """Return list of (index, filename) for all reference tracks."""
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
    """Print numbered list of reference tracks."""
    print(f"\n  {len(tracks)} reference tracks in {REF_DIR}:")
    for i, fp in enumerate(tracks):
        name = os.path.basename(fp)
        print(f"  [{i+1:2d}] {name}")


def build_profile_from_indices(track_paths, indices):
    """Build aggregate profile from selected track paths by index (1-based).
    Uses per-track cache (~/.hermes/data/deepspace_per_track/) for instant load.
    Returns path to profile JSON, or None on failure."""
    cache_dir = os.path.expanduser("~/.hermes/data/deepspace_per_track")

    selected = []
    for idx in sorted(set(indices)):
        if 1 <= idx <= len(track_paths):
            selected.append(track_paths[idx - 1])

    if not selected:
        return None

    print(f"\n  Building profile from {len(selected)} track(s):")
    analyses = []

    for fp in selected:
        name = os.path.basename(fp)
        cache_path = os.path.join(cache_dir, name + '.json')
        print(f"    - {name}", end='')

        # Try cache first (instant)
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    analyses.append(json.load(f))
                print("  [cached]")
                continue
            except Exception:
                pass

        # Fallback: live analysis (~7s per track)
        print("  [analyzing...]", end='', flush=True)
        try:
            r = subprocess.run(
                [PYTHON, ANALYZER, "analyze", fp],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                analyses.append(json.loads(r.stdout))
                # Save to cache for next time
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, 'w') as f:
                    f.write(r.stdout)
                print(" ✓")
            else:
                print(f" ✗ failed")
        except Exception as e:
            print(f" ✗ {e}")

    if not analyses:
        return None

    # Aggregate (same logic as build_profile in audio_analyzer.py)
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

    # Save to temp profile
    fd, profile_path = tempfile.mkstemp(suffix='.json', prefix='ref_profile_')
    os.close(fd)
    with open(profile_path, 'w') as f:
        json.dump(profile, f, indent=2)

    print(f"    ✓ Profile saved: {profile_path}")
    return profile_path


# ═══════════════════════════════════════════
# SPECTRAL RECEIVER — background thread
# ═══════════════════════════════════════════

class SpectralReceiver(threading.Thread):
    """Continuously reads spectral packets from UDP 9880 in a background thread.
    Main thread calls get_latest() for the most recent frame (non-blocking).
    """

    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._latest = None       # list of 7 floats
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

            # Parse OSC: find /spectral_shape
            null = data.find(b'\x00')
            if null < 0:
                continue
            addr = data[:null].decode('ascii', errors='replace')
            if addr != '/spectral_shape':
                continue

            # Skip type tag string
            pos = (null + 4) & ~3
            tag_end = data.find(b'\x00', pos)
            if tag_end < 0:
                continue
            pos = (tag_end + 4) & ~3

            # Parse 7 float32 values
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
        """Non-blocking. Returns (values, timestamp, count) or (None, 0, 0)."""
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
# BRIDGE COMMAND SENDER — fire and forget
# ═══════════════════════════════════════════

class BridgeSender:
    """Sends UDP commands to the M4L bridge (port 9881). Fire-and-forget —
    no response read needed; the spectral stream confirms results.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def set_param(self, track_idx, device_idx, param_idx, value):
        """Send set_param OSC command. No response read — returns immediately."""
        cmd = b'set_param\x00\x00\x00'
        types = b',iiif\x00'
        args = struct.pack('>iiif', track_idx, device_idx, param_idx, float(value))
        self._sock.sendto(cmd + types + args, ('127.0.0.1', BRIDGE_TX))

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# ═══════════════════════════════════════════
# BLACKHOLE VALIDATION — async subprocess
# ═══════════════════════════════════════════

class AsyncValidation:
    """Spawn ffmpeg capture + analysis in background. Main loop polls for result."""

    def __init__(self, profile_path=None):
        self._process = None
        self._wav_path = None
        self._profile_path = profile_path or PROFILE

    def start(self):
        """Launch ffmpeg capture → analysis in background."""
        fd, self._wav_path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)

        # Capture 4s of audio via BlackHole
        self._process = subprocess.Popen(
            ['ffmpeg', '-y', '-f', 'avfoundation', '-i', ':2',
             '-t', '4', '-ar', '22050', '-ac', '2', '-c:a', 'pcm_s16le',
             self._wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._start_time = time.time()

    def poll(self):
        """Check if capture+analysis is done. Returns comparison dict or None."""
        if self._process is None:
            return None

        # Check if ffmpeg finished
        if self._process.poll() is None:
            # Still running
            return None

        # ffmpeg done — run analysis
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
# TCP FALLBACK — for track/device discovery
# ═══════════════════════════════════════════

import socket as sock_module

def tcp_call(cmd, params=None, timeout=10):
    s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', 9878))
        s.sendall(json.dumps({'id':'x','type':cmd,'params':params or {}}).encode()+b'\n')
        r = b''
        while True:
            try:
                c = s.recv(65536)
                if not c: break
                r += c
            except: break
        for line in r.decode().strip().split('\n'):
            if line.strip():
                try: return json.loads(line)
                except: pass
        return {'ok': False}
    finally:
        s.close()

_track_cache = {}
_param_cache = {}

def get_track_names():
    r = tcp_call('get_all_track_names', timeout=10)
    return r['result']['tracks'] if r.get('ok') else []

def ensure_device(ti, device_name):
    """Ensure a native Live device exists on the track. Adds it if missing.
    Returns the device index, or -1 on failure."""
    devs = get_track_devices(ti)
    for d in devs:
        if device_name.lower() in d['name'].lower():
            return d['index']

    # Not found — insert via LivePilot
    print(f"     ＋ adding {device_name} to track {ti}...", end='', flush=True)
    r = tcp_call('insert_device', {'track_index': ti, 'device_name': device_name}, timeout=10)
    if r.get('ok'):
        # Invalidate cache so next get_track_devices re-fetches
        if ti in _track_cache:
            del _track_cache[ti]
        devs = get_track_devices(ti)
        for d in devs:
            if device_name.lower() in d['name'].lower():
                print(f" ✓ (index {d['index']})")
                return d['index']
    print(f" ✗ failed: {r.get('error', r)}")
    return -1


def get_track_devices(ti):
    if ti in _track_cache:
        return _track_cache[ti]
    r = tcp_call('get_track_info', {'track_index': ti}, timeout=4)
    if not r.get('ok'): return []
    devs = [{'index': di, 'name': d.get('name','')} for di, d in enumerate(r['result'].get('devices',[]))]
    _track_cache[ti] = devs
    return devs

def get_device_params(ti, di):
    r = tcp_call('get_device_parameters', {'track_index': ti, 'device_index': di}, timeout=6)
    if not r.get('ok'): return {}
    return {p['name'].lower(): {'index': p['index'], 'value': p['value']}
            for p in r['result'].get('parameters', [])}

def get_cached_param_index(ti, di, param_hints, band_name=None):
    """Find param index matching hints. For EQ Eight, uses band_name
    to target the correct filter (e.g. 'bass' → filter 2 gain, not filter 1).
    Returns (index, param_name) or (None, None)."""
    key = (ti, di)
    if key not in _param_cache:
        _param_cache[key] = get_device_params(ti, di)
    params = _param_cache[key]

    # If band specified and device looks like EQ, prefer band-specific filter
    if band_name and band_name in BAND_TO_EQ_FILTER:
        filter_num = BAND_TO_EQ_FILTER[band_name]
        for hint in param_hints:
            hint_lower = hint.lower()
            # Try band-specific first: "2 gain a" for bass
            filter_prefix = f"{filter_num} {hint_lower}"
            for pname, pinfo in params.items():
                if filter_prefix in pname:
                    return pinfo['index'], pname
            # Fallback: adjacent band
            for offset in [1, -1, 2, -2]:
                alt = filter_num + offset
                if 1 <= alt <= 8:
                    alt_prefix = f"{alt} {hint_lower}"
                    for pname, pinfo in params.items():
                        if alt_prefix in pname:
                            return pinfo['index'], pname

    # Generic fallback: substring match
    for hint in param_hints:
        hint_lower = hint.lower()
        for pname, pinfo in params.items():
            if hint_lower in pname:
                return pinfo['index'], pname
    return None, None


def get_resonance_index(ti, di, gain_param_name):
    """Given a gain param like '2 gain a', find the matching resonance param
    ('2 resonance a') and return its index, or None."""
    key = (ti, di)
    if key not in _param_cache:
        _param_cache[key] = get_device_params(ti, di)
    params = _param_cache[key]

    # Replace 'gain' with 'resonance' in the param name
    res_name = gain_param_name.replace('gain', 'resonance')
    if res_name in params:
        return params[res_name]['index']
    # Also try without 'a'/'b' suffix
    base = gain_param_name.rsplit(' ', 1)[0]  # "2 gain"
    for suffix in ['a', 'b']:
        candidate = f"{base.replace('gain', 'resonance')} {suffix}"
        if candidate in params:
            return params[candidate]['index']
    return None


# ═══════════════════════════════════════════
# ANALYSIS — compute fix from spectral delta
# ═══════════════════════════════════════════

# Bridge raw values are 0-1 energy, reference profile is dB (30-45).
# Bridge data is ONLY used for relative movement tracking between validations.
# BlackHole validation provides the absolute ground truth comparison.

_last_validation_deviation = None  # (band, direction, sigmas) from last BlackHole check
_last_validation_spectral = None   # bridge spectral at time of validation


def set_validation_ground_truth(comparison_result, current_spectral=None):
    """Called after each BlackHole validation to set the ground truth target."""
    global _last_validation_deviation, _last_validation_spectral
    if not comparison_result or 'error' in comparison_result:
        return
    band_issues = comparison_result.get('band_issues', [])
    _last_validation_deviation = find_biggest_deviation(band_issues)
    if current_spectral and _last_validation_deviation:
        _last_validation_spectral = list(current_spectral)


def get_band_direction(current_spectral, band_name):
    """Check if bridge band is moving up/down relative to last ground truth.
    Returns 'improving', 'worsening', or 'stable'.
    """
    global _last_validation_spectral, _last_validation_deviation
    if not _last_validation_deviation or not _last_validation_spectral:
        return 'stable'

    target_band, target_direction, _ = _last_validation_deviation
    if band_name != target_band:
        return 'stable'

    band_idx = BAND_INDEX_MAP.get(band_name, 0)
    delta = current_spectral[band_idx] - _last_validation_spectral[band_idx]

    if target_direction == 'weak' and delta > 0.001:
        return 'improving'
    elif target_direction == 'hot' and delta < -0.001:
        return 'improving'
    elif abs(delta) < 0.001:
        return 'stable'
    else:
        return 'worsening'


def checkpoint_spectral(spectral):
    """Store current spectral as the reference point. Called after applying a fix."""
    global _last_validation_spectral
    _last_validation_spectral = list(spectral)


# ═══════════════════════════════════════════
# MAIN ASYNC LOOP
# ═══════════════════════════════════════════

def run_async_loop(iterations=50, validate_every=VALIDATION_INTERVAL, profile_path=None):
    global _last_validation_deviation, _last_validation_spectral
    profile_path = profile_path or PROFILE

    print("═" * 60)
    print("Bridge Async Mixing Loop")
    print(f"  Architecture:  receiver thread + analysis pipeline")
    print(f"  Max iterations: {iterations}")
    print(f"  Validate every: {validate_every}")
    print("═" * 60)

    # 1. Start spectral receiver thread
    receiver = SpectralReceiver()
    receiver.start()
    time.sleep(0.3)  # let it start and get first packets
    if not receiver.is_alive():
        print("ERROR: Receiver thread didn't start. Is the bridge running?")
        return

    _, _, init_count = receiver.get_latest()
    if init_count == 0:
        print("ERROR: No spectral packets received. Is Ableton playing?")
        receiver.stop()
        return
    print(f"\n✓ Receiver thread alive — {init_count} packets buffered\n")

    # 2. Get track info via TCP (once)
    track_names = get_track_names()
    tagged = sum(1 for t in track_names if parse_track_role(t.get('name','')))
    print(f"  {len(track_names)} tracks, {tagged} tagged\n")

    # 4. Bridge sender
    bridge = BridgeSender()

    # 5. State
    prev_spectral = None
    prev_count = init_count
    validation = None
    last_fix_info = None  # (track_idx, device_idx, param_idx, delta, band, direction)
    applied_fix = False   # whether we just applied a fix

    print("Starting main loop...\n")

    for i in range(iterations):
        t0 = time.time()

        # Check for completed validation
        if validation is not None:
            result = validation.poll()
            if result is not None:
                if 'error' not in result:
                    band_issues = result.get('band_issues', [])
                    biggest = find_biggest_deviation(band_issues)
                    if biggest:
                        band, direction, sigmas = biggest
                        print(f"  📊 VALIDATION #{i+1}: {band} {direction} ({sigmas:.1f}σ) — {validation.elapsed():.1f}s")
                        set_validation_ground_truth(result, spectral)
                        # Force first fix on next iteration
                        applied_fix = False
                    else:
                        print(f"  📊 VALIDATION #{i+1}: all bands within deadband ✓ — {validation.elapsed():.1f}s")
                        _last_validation_deviation = None
                else:
                    print(f"  📊 VALIDATION #{i+1}: FAILED — {result['error']}")
                validation = None

        # ── Get latest spectrum ──
        spectral, ts, count = receiver.get_latest()

        if spectral is None:
            time.sleep(0.005)
            continue

        # ── If no ground truth yet, skip analysis ──
        if _last_validation_deviation is None:
            # Validation takes ~5s — slow poll until it finishes
            if validation and validation.running:
                print(f"[{i+1:3d}] ⏳ waiting for first validation ({validation.elapsed():.1f}s elapsed)")
                time.sleep(0.5)
            else:
                print(f"[{i+1:3d}] ⏳ waiting for first validation  ({time.time()-t0:.3f}s)")
                # Spawn first validation if not already running
                if validation is None:
                    validation = AsyncValidation(profile_path)
                    validation.start()
                    print(f"  ⏳ BlackHole validation started...")
                time.sleep(0.5)
            prev_spectral = spectral
            continue

        target_band, target_direction, target_sigmas = _last_validation_deviation

        # ── Check direction of movement ──
        movement = get_band_direction(spectral, target_band)

        if movement == 'improving':
            print(f"[{i+1:3d}] 🌉 {target_band:10s} {target_direction:4s} IMPROVING  ({time.time()-t0:.3f}s)")
            # Update checkpoint — this is the new baseline
            checkpoint_spectral(spectral)
        elif not applied_fix or movement == 'stable':
            # First fix after validation, or stable → apply next adjustment
            label = "applying fix" if not applied_fix else "stable — applying next fix"
            print(f"[{i+1:3d}] 🌉 {target_band:10s} {target_direction:4s} {label}  ({time.time()-t0:.3f}s)")
            # Apply the fix
            fix, rec_text, _ = map_band_to_fix(target_band, target_direction, i)
            if fix:
                candidates = [t for t in track_names
                            if not t.get('mute')
                            and get_track_category(t['name'])
                            and category_matches_recommendation(get_track_category(t['name']), rec_text)]
                if not candidates:
                    candidates = [t for t in track_names if not t.get('mute')]

                match = None
                for cand in candidates[:5]:
                    devs = get_track_devices(cand['index'])
                    for dev in devs:
                        if any(dt.lower() in dev['name'].lower() for dt in fix['devices']):
                            match = (cand['index'], dev['index'], dev['name'], cand['name'])
                            break
                    if match: break

                if match:
                    ti, di, dname, tname = match
                    pidx, pname = get_cached_param_index(ti, di, fix['params'], band_name=target_band)

                    if pidx is None:
                        # Try generic 'gain' match as fallback (e.g. 'Output Gain')
                        pidx, pname = get_cached_param_index(ti, di, fix['params'])
                        if pidx is not None:
                            print(f"     ⚠ band '{target_band}' param not found, using '{pname}' fallback")
                        else:
                            print(f"     ⚠ no matching param in {dname} (band '{target_band}')")
                            continue

                    # Get current value for relative adjustment
                    key = (ti, di)
                    current_val = 0.5  # default midpoint
                    if key in _param_cache:
                        for _pn, pinfo in _param_cache[key].items():
                            if pinfo['index'] == pidx:
                                current_val = pinfo['value']
                                break
                    delta = fix['delta_base'] * PROPORTIONAL_GAIN
                    new_val = max(-1.0, min(1.0, current_val + delta))  # EQ8 gain is bipolar
                    bridge.set_param(ti, di, pidx, new_val)
                    # Update cache so next read shows the new value
                    if key in _param_cache:
                        for _pn, pinfo in _param_cache[key].items():
                            if pinfo['index'] == pidx:
                                pinfo['value'] = new_val
                                break

                    # ── Q adjustment: wider gap → wider Q (lower resonance) ──
                    q_val = sigmas_to_q(target_sigmas)
                    if q_val is not None:
                        ridx = get_resonance_index(ti, di, pname)
                        if ridx is not None:
                            bridge.set_param(ti, di, ridx, q_val)
                            print(f"     ✏️  {dname}({tname}) {pname} Δ{delta:+.2f} ({current_val:.2f}→{new_val:.2f}) Q={q_val:.2f}")
                        else:
                            print(f"     ✏️  {dname}({tname}) {pname} Δ{delta:+.2f} ({current_val:.2f}→{new_val:.2f})")
                    else:
                        print(f"     ✏️  {dname}({tname}) {pname} Δ{delta:+.2f} ({current_val:.2f}→{new_val:.2f})")

                    last_fix_info = (ti, di, pidx, delta, target_band, target_direction)
                    checkpoint_spectral(spectral)  # baseline BEFORE fix
                    applied_fix = True
                    time.sleep(0.05)  # cooldown: let bridge stream 2-3 frames
                else:
                    print(f"     ⚠ no matching device for {rec_text}")
            else:
                print(f"     ⚠ no fix mapping for {target_band}/{target_direction}")
        elif movement == 'worsening':
            print(f"[{i+1:3d}] ⚠️  {target_band:10s} {target_direction:4s} WORSE — reversing  ({time.time()-t0:.3f}s)")
            # Reverse last fix
            if last_fix_info:
                ti, di, pidx, delta, _, _ = last_fix_info
                # Reverse: subtract delta from current value
                key = (ti, di)
                current_val = 0.5
                if key in _param_cache:
                    for pname, pinfo in _param_cache[key].items():
                        if pinfo['index'] == pidx:
                            current_val = pinfo['value']
                            break
                new_val = max(-1.0, min(1.0, current_val - delta))  # EQ8 gain is bipolar
                bridge.set_param(ti, di, pidx, new_val)
                # Update cache
                if key in _param_cache:
                    for _pn, pinfo in _param_cache[key].items():
                        if pinfo['index'] == pidx:
                            pinfo['value'] = new_val
                            break
                print(f"     ↩  reversed Δ{delta:+.2f} ({current_val:.2f}→{new_val:.2f})")
                applied_fix = False
        else:
            print(f"[{i+1:3d}] 🌉 {target_band:10s} {target_direction:4s} monitoring  ({time.time()-t0:.3f}s)")

        prev_spectral = spectral
        time.sleep(0.005)  # prevent CPU spin; bridge streams at ~50Hz

        # ── Spawn validation if due ──
        if (i + 1) % validate_every == 0 and validation is None:
            validation = AsyncValidation(profile_path)
            validation.start()
            print(f"  ⏳ BlackHole validation started (will resolve in ~5s)...")
            applied_fix = False  # reset — ground truth about to refresh

    # ── Cleanup ──
    print(f"\n{'═'*60}")
    if validation and validation.running:
        print("Waiting for final validation...")
        while validation.running:
            time.sleep(0.5)
        result = validation.poll()
        if result and 'error' not in result:
            print(f"Final: {len(result.get('band_issues',[]))} issues")
    print("Loop complete")
    print(f"{'═'*60}")

    receiver.stop()
    bridge.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Async bridge-based mixing loop')
    parser.add_argument('-n', '--iterations', type=int, default=50,
                        help='Max iterations (default: 50)')
    parser.add_argument('-v', '--validate-every', type=int, default=VALIDATION_INTERVAL,
                        help=f'Validation interval (default: {VALIDATION_INTERVAL})')
    parser.add_argument('--refs', type=str, default=None,
                        help='Comma-separated reference track indices (e.g. "3,5,12")')
    parser.add_argument('--list-refs', action='store_true',
                        help='List available reference tracks and exit')
    args = parser.parse_args()

    # --list-refs: print and exit
    if args.list_refs:
        tracks = list_reference_tracks()
        print_reference_tracks(tracks)
        sys.exit(0)

    # --refs: build targeted profile
    profile_path = PROFILE
    if args.refs:
        tracks = list_reference_tracks()
        if not tracks:
            print("ERROR: No reference tracks found in", REF_DIR)
            sys.exit(1)
        try:
            indices = [int(x.strip()) for x in args.refs.split(',')]
        except ValueError:
            print("ERROR: --refs must be comma-separated numbers, e.g. '3,5,12'")
            sys.exit(1)

        profile_path = build_profile_from_indices(tracks, indices)
        if not profile_path:
            print("ERROR: Failed to build profile from indices", args.refs)
            sys.exit(1)

    run_async_loop(iterations=args.iterations, validate_every=args.validate_every,
                   profile_path=profile_path)
