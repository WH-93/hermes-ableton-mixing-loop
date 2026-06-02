#!/usr/bin/env python3
"""
Audio Analysis Engine for Hermes ↔ Ableton Mixing Loop

Capabilities:
  analyze <file>            — full analysis of a single track
  profile <dir>             — build aggregate reference profile from directory of tracks
  compare <file> <profile>  — diff track against reference profile, output recommendations
  capture <duration>        — capture audio from BlackHole, analyze, compare

Output: JSON on stdout for parseability by Hermes.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

import numpy as np
import librosa
import soundfile as sf
import pyloudnorm as pyln
from scipy import signal

warnings.filterwarnings("ignore")

# ─── frequency band definitions (techno-specific) ───

BANDS = {
    "sub": (20, 60),
    "bass": (60, 120),
    "low_mid": (120, 250),
    "mid": (250, 500),
    "high_mid": (500, 2000),
    "presence": (2000, 6000),
    "air": (6000, 16000),
}

OCTAVE_BANDS = [
    (20, 40, "sub_low"),
    (40, 80, "sub_high"),
    (80, 160, "bass"),
    (160, 320, "low_mid"),
    (320, 640, "mid_low"),
    (640, 1280, "mid_high"),
    (1280, 2560, "high_mid_low"),
    (2560, 5120, "high_mid_high"),
    (5120, 10240, "presence"),
    (10240, 16000, "air"),
]


def load_audio(path: str, sr: int = 22050) -> tuple[np.ndarray, np.ndarray, int]:
    """Load audio once, return (mono, stereo, sr). Target sr=22050 for speed."""
    y_stereo, actual_sr = librosa.load(path, sr=sr, mono=False)
    if y_stereo.ndim == 1:
        y_stereo = np.stack([y_stereo, y_stereo], axis=0)
    y_mono = np.mean(y_stereo, axis=0)
    return y_mono, y_stereo, actual_sr


def _stft_band_energies(y: np.ndarray, sr: int, bands: list) -> dict:
    """STFT-based energy per band — windowed, fast, no giant FFT."""
    S = np.abs(librosa.stft(y, n_fft=4096, hop_length=2048))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
    # Mean energy across time, per frequency bin
    mean_mag = np.mean(S, axis=1)
    results = {}
    for low, high, name in bands:
        mask = (freqs >= low) & (freqs < high)
        if np.any(mask):
            energy = np.sqrt(np.mean(mean_mag[mask]**2))
            results[name] = round(float(20 * np.log10(energy + 1e-10)), 1)
        else:
            results[name] = -120.0
    return results


def band_rms(y: np.ndarray, sr: int, low: float, high: float) -> float:
    """RMS energy in a frequency band (FFT-based). Kept for API compat."""
    N = len(y)
    fft = np.abs(np.fft.rfft(y)) / N
    freqs = np.fft.rfftfreq(N, 1/sr)
    mask = (freqs >= low) & (freqs < high)
    return float(np.sqrt(np.mean(fft[mask]**2))) if np.any(mask) else 0.0


def compute_analysis(y_mono: np.ndarray, y_stereo: np.ndarray, sr: int) -> dict:
    """Full analysis of a track."""
    duration = len(y_mono) / sr
    results = {"duration": round(duration, 2), "sample_rate": sr}

    # ─── loudness ───
    # LUFS via pyloudnorm (needs stereo or mono float32)
    meter = pyln.Meter(sr)
    if y_stereo.ndim == 2 and y_stereo.shape[0] == 2:
        lufs_audio = y_stereo.T  # (samples, channels)
    else:
        lufs_audio = np.column_stack([y_mono, y_mono])
    try:
        loudness = meter.integrated_loudness(lufs_audio.astype(np.float32))
        results["lufs_integrated"] = round(float(loudness), 1)
    except Exception:
        results["lufs_integrated"] = None

    # ─── peak and RMS ───
    peak_db = 20 * np.log10(np.max(np.abs(y_mono)) + 1e-10)
    rms_total = np.sqrt(np.mean(y_mono**2))
    rms_db = 20 * np.log10(rms_total + 1e-10)
    crest_factor = peak_db - rms_db
    results["peak_db"] = round(float(peak_db), 1)
    results["rms_db"] = round(float(rms_db), 1)
    results["crest_factor_db"] = round(float(crest_factor), 1)

    # ─── per-band RMS (STFT-based) ───
    results["band_levels"] = _stft_band_energies(
        y_mono, sr, [(v[0], v[1], k) for k, v in BANDS.items()])
    results["octave_levels"] = _stft_band_energies(y_mono, sr, OCTAVE_BANDS)

    # ─── spectral features ───
    centroid = librosa.feature.spectral_centroid(y=y_mono, sr=sr)[0]
    results["spectral_centroid_mean"] = round(float(np.mean(centroid)), 1)

    rolloff = librosa.feature.spectral_rolloff(y=y_mono, sr=sr)[0]
    results["spectral_rolloff_mean"] = round(float(np.mean(rolloff)), 1)

    bandwidth = librosa.feature.spectral_bandwidth(y=y_mono, sr=sr)[0]
    results["spectral_bandwidth_mean"] = round(float(np.mean(bandwidth)), 1)

    # ─── dynamic range over time ───
    rms = librosa.feature.rms(y=y_mono)[0]
    rms_db_over_time = 20 * np.log10(rms + 1e-10)
    results["rms_min_db"] = round(float(np.min(rms_db_over_time)), 1)
    results["rms_max_db"] = round(float(np.max(rms_db_over_time)), 1)
    results["rms_range_db"] = round(float(np.ptp(rms_db_over_time)), 1)

    # ─── stereo analysis ───
    if y_stereo.ndim == 2 and y_stereo.shape[0] == 2:
        left = y_stereo[0]
        right = y_stereo[1]
        # stereo width (difference energy / sum energy)
        side = left - right
        mid = left + right
        side_rms = np.sqrt(np.mean(side**2))
        mid_rms = np.sqrt(np.mean(mid**2))
        width_ratio = side_rms / (mid_rms + 1e-10)
        results["stereo_width"] = round(float(width_ratio), 2)

        # stereo correlation
        if len(left) > 0:
            corr = np.corrcoef(left, right)[0, 1]
            results["stereo_correlation"] = round(float(corr), 2) if not np.isnan(corr) else 0.0
        else:
            results["stereo_correlation"] = 0.0

        # per-band stereo width (STFT-based)
        S_left = np.abs(librosa.stft(left, n_fft=4096, hop_length=2048))
        S_right = np.abs(librosa.stft(right, n_fft=4096, hop_length=2048))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
        mean_l = np.mean(S_left, axis=1)
        mean_r = np.mean(S_right, axis=1)
        band_widths = {}
        for name, (low, high) in BANDS.items():
            mask = (freqs >= low) & (freqs < high)
            if np.any(mask):
                s_energy = np.sqrt(np.mean((mean_l[mask] - mean_r[mask])**2))
                m_energy = np.sqrt(np.mean((mean_l[mask] + mean_r[mask])**2))
                bw = s_energy / (m_energy + 1e-10)
            else:
                bw = 0.0
            band_widths[name] = round(float(bw), 2)
        results["band_stereo_width"] = band_widths
    else:
        results["stereo_width"] = 0.0
        results["stereo_correlation"] = 1.0
        results["band_stereo_width"] = {k: 0.0 for k in BANDS}

    # ─── sub presence check (STFT-based) ───
    S = np.abs(librosa.stft(y_mono, n_fft=4096, hop_length=2048))
    fft_freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
    mean_mag = np.mean(S, axis=1)
    sub_mask = (fft_freqs >= 20) & (fft_freqs < 80)
    full_mask = (fft_freqs >= 20) & (fft_freqs < 16000)
    sub_energy = np.sqrt(np.mean(mean_mag[sub_mask]**2)) if np.any(sub_mask) else 0.0
    total_energy = np.sqrt(np.mean(mean_mag[full_mask]**2)) if np.any(full_mask) else 1e-10
    results["sub_ratio"] = round(float(sub_energy / total_energy), 3)

    return results


def build_profile(ref_dir: str) -> dict:
    """Analyze all tracks in a directory and build aggregate reference profile."""
    ref_dir = os.path.expanduser(ref_dir)
    tracks = []
    for f in sorted(os.listdir(ref_dir)):
        if f.startswith("."):
            continue
        fp = os.path.join(ref_dir, f)
        if os.path.isdir(fp):
            continue
        ext = f.lower().rsplit(".", 1)[-1] if "." in f else ""
        if ext not in ("wav", "mp3", "flac", "aiff", "aif"):
            continue
        tracks.append(fp)

    if not tracks:
        print(json.dumps({"error": "no audio files found in directory"}))
        sys.exit(1)

    all_analyses = []
    for fp in tracks:
        try:
            y_mono, y_stereo, sr = load_audio(fp)
            analysis = compute_analysis(y_mono, y_stereo, sr)
            analysis["filename"] = os.path.basename(fp)
            all_analyses.append(analysis)
        except Exception as e:
            print(f"# WARNING: failed to analyze {os.path.basename(fp)}: {e}", file=sys.stderr)

    if not all_analyses:
        print(json.dumps({"error": "all tracks failed analysis"}))
        sys.exit(1)

    # aggregate numeric fields
    numeric_keys = [
        "lufs_integrated", "peak_db", "rms_db", "crest_factor_db",
        "spectral_centroid_mean", "spectral_rolloff_mean", "spectral_bandwidth_mean",
        "rms_min_db", "rms_max_db", "rms_range_db",
        "stereo_width", "stereo_correlation", "sub_ratio",
    ]

    profile = {"num_tracks": len(all_analyses), "files": [a["filename"] for a in all_analyses]}

    for key in numeric_keys:
        vals = [a[key] for a in all_analyses if a.get(key) is not None]
        if vals:
            profile[key] = {
                "median": round(float(np.median(vals)), 2),
                "mean": round(float(np.mean(vals)), 2),
                "std": round(float(np.std(vals)), 2),
                "min": round(float(min(vals)), 2),
                "max": round(float(max(vals)), 2),
            }

    # per-band aggregation
    for band_type in ["band_levels", "octave_levels", "band_stereo_width"]:
        agg = {}
        for a in all_analyses:
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

    return profile


def compare_track(analysis: dict, profile: dict) -> dict:
    """Diff track against reference profile, return issues and recommendations."""
    issues = []
    recommendations = []

    numeric_keys = [
        ("lufs_integrated", "LUFS", "dB"),
        ("crest_factor_db", "Crest factor", "dB"),
        ("stereo_width", "Stereo width", "ratio"),
        ("sub_ratio", "Sub energy ratio", ""),
        ("spectral_centroid_mean", "Spectral centroid", "Hz"),
        ("rms_range_db", "Dynamic range", "dB"),
    ]

    for key, label, unit in numeric_keys:
        if key not in analysis or key not in profile:
            continue
        track_val = analysis[key]
        if track_val is None:
            continue
        ref = profile[key]
        median = ref["median"]
        std = ref["std"]
        dev = track_val - median
        sigmas = abs(dev) / (std + 1e-6)
        direction = "higher" if dev > 0 else "lower"

        if sigmas > 2.0:
            issues.append({
                "parameter": key,
                "label": label,
                "track_value": round(track_val, 2),
                "reference_median": round(median, 2),
                "deviation": round(dev, 2),
                "sigmas": round(sigmas, 1),
                "direction": direction,
                "severity": "high",
            })
        elif sigmas > 1.0:
            issues.append({
                "parameter": key,
                "label": label,
                "track_value": round(track_val, 2),
                "reference_median": round(median, 2),
                "deviation": round(dev, 2),
                "sigmas": round(sigmas, 1),
                "direction": direction,
                "severity": "medium",
            })

    # per-band frequency comparison
    band_issues = []
    track_bands = analysis.get("band_levels", {})
    ref_bands = profile.get("band_levels", {})
    for band_name, track_level in track_bands.items():
        if band_name not in ref_bands:
            continue
        ref = ref_bands[band_name]
        median = ref["median"]
        std = ref["std"]
        dev = track_level - median
        sigmas = abs(dev) / (std + 1e-6)
        direction = "hot" if dev > 0 else "weak"

        if sigmas > 2.0:
            band_issues.append({
                "band": band_name,
                "track_db": track_level,
                "reference_db": round(median, 1),
                "deviation_db": round(dev, 1),
                "sigmas": round(sigmas, 1),
                "direction": direction,
                "severity": "high",
            })
        elif sigmas > 1.0:
            band_issues.append({
                "band": band_name,
                "track_db": track_level,
                "reference_db": round(median, 1),
                "deviation_db": round(dev, 1),
                "sigmas": round(sigmas, 1),
                "direction": direction,
                "severity": "medium",
            })

    issues.append({"band_levels": band_issues})

    # generate recommendations
    for issue in issues:
        if "parameter" not in issue:
            continue
        param = issue["parameter"]
        severity = issue["severity"]
        direction = issue["direction"]

        if param == "lufs_integrated":
            if direction == "lower" and severity == "high":
                recommendations.append("Master is too quiet. Raise master gain or reduce headroom.")
            elif direction == "higher":
                recommendations.append("Master is loud. Reduce master fader or limiter input gain.")
        elif param == "crest_factor_db":
            if direction == "lower":
                recommendations.append("Track is over-compressed. Ease back on master compressor/limiter, increase attack times.")
            elif direction == "higher":
                recommendations.append("Track is very dynamic. Consider more compression/saturation for glue.")
        elif param == "stereo_width":
            if direction == "lower" and severity == "high":
                recommendations.append("Mix is narrow. Widen pads/hats/fx. Pan percussion elements. Add stereo reverb return.")
            elif direction == "higher":
                recommendations.append("Mix is unusually wide. Check for phase issues on master. Consider narrowing sub.")
        elif param == "sub_ratio":
            if direction == "higher" and severity == "high":
                recommendations.append("Sub is overwhelming. Reduce kick sub, HP filter low end, check master EQ.")
            elif direction == "lower" and severity == "high":
                recommendations.append("Sub is weak. Boost kick sub, add rumble, check HP filter cutoffs.")
        elif param == "rms_range_db":
            if direction == "lower":
                recommendations.append("Limited dynamic range. Add more arrangement variation, reduce master bus compression.")
            elif direction == "higher":
                recommendations.append("Wide dynamics. May need more compression for club playability.")

    # per-band recommendations
    for bi in band_issues:
        band = bi["band"]
        sev = bi["severity"]
        direction = bi["direction"]
        if sev != "high":
            continue
        if band == "sub" and direction == "hot":
            recommendations.append("Sub frequencies (20-60Hz) are hot. Reduce kick sub, tighten rumble, check master EQ low shelf.")
        elif band == "sub" and direction == "weak":
            recommendations.append("Sub frequencies (20-60Hz) are weak. Boost kick sub, add more rumble body.")
        elif band == "bass" and direction == "hot":
            recommendations.append("Bass (60-120Hz) is hot. Tame bass synth, reduce kick body, narrow low-mid EQ.")
        elif band == "bass" and direction == "weak":
            recommendations.append("Bass (60-120Hz) is weak. Boost bass synth level, increase kick body, check HP filters.")
        elif band == "low_mid" and direction == "hot":
            recommendations.append("Low-mids (120-250Hz) are muddy. Cut pads/bass in this region, HP filter reverb returns.")
        elif band == "low_mid" and direction == "weak":
            recommendations.append("Low-mids (120-250Hz) are thin. Boost warmth on pads/chords, add saturation.")
        elif band == "presence" and direction == "hot":
            recommendations.append("Presence (2-6kHz) is harsh. Tame hats, reduce distortion, cut synth highs.")
        elif band == "presence" and direction == "weak":
            recommendations.append("Presence (2-6kHz) is dull. Boost hats, add saturation to synths, open filters.")
        elif band == "air" and direction == "hot":
            recommendations.append("Air (6-16kHz) is harsh. Reduce hat highs, tame reverb tails, LP filter high elements.")
        elif band == "air" and direction == "weak":
            recommendations.append("Air (6-16kHz) is missing. Open hat filters, boost high shelf on master, add shimmer reverb.")

    return {"issues": issues, "band_issues": band_issues, "recommendations": recommendations[:8]}


def capture_blackhole(duration: int) -> str:
    """Capture audio from BlackHole 2ch via ffmpeg, return temp file path."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        output_path = tf.name

    cmd = [
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-i", ":2",  # BlackHole 2ch device index
        "-t", str(duration),
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        output_path,
    ]

    subprocess.run(cmd, capture_output=True, timeout=duration + 15)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Audio Analysis Engine for Hermes")
    sub = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("file", help="Audio file to analyze")

    # profile
    p_profile = sub.add_parser("profile")
    p_profile.add_argument("dir", help="Directory of reference tracks")

    # compare
    p_compare = sub.add_parser("compare")
    p_compare.add_argument("file", help="Audio file to compare")
    p_compare.add_argument("profile", help="Profile JSON file or directory of reference tracks")

    # capture
    p_capture = sub.add_parser("capture")
    p_capture.add_argument("duration", type=int, help="Capture duration in seconds")
    p_capture.add_argument("--profile", help="Reference profile JSON file for comparison", default=None)
    p_capture.add_argument("--output", help="Save captured audio to this path", default=None)

    args = parser.parse_args()

    if args.command == "profile":
        profile = build_profile(args.dir)
        print(json.dumps(profile, indent=2))

    elif args.command == "analyze":
        y_mono, y_stereo, sr = load_audio(args.file)
        analysis = compute_analysis(y_mono, y_stereo, sr)
        analysis["filename"] = os.path.basename(args.file)
        print(json.dumps(analysis, indent=2))

    elif args.command == "compare":
        y_mono, y_stereo, sr = load_audio(args.file)
        analysis = compute_analysis(y_mono, y_stereo, sr)
        analysis["filename"] = os.path.basename(args.file)

        pf_path = args.profile
        if os.path.isdir(pf_path):
            profile = build_profile(pf_path)
        else:
            with open(pf_path) as f:
                profile = json.load(f)

        result = compare_track(analysis, profile)
        result["analysis"] = analysis
        print(json.dumps(result, indent=2))

    elif args.command == "capture":
        print(f"Capturing {args.duration}s from BlackHole 2ch...", file=sys.stderr)
        audio_path = capture_blackhole(args.duration)

        if args.output:
            os.rename(audio_path, args.output)
            audio_path = args.output
            print(f"Saved to {args.output}", file=sys.stderr)

        y_mono, y_stereo, sr = load_audio(audio_path)
        analysis = compute_analysis(y_mono, y_stereo, sr)
        analysis["filename"] = os.path.basename(audio_path)

        output = {"analysis": analysis}

        if args.profile:
            if os.path.isdir(args.profile):
                profile = build_profile(args.profile)
            else:
                with open(args.profile) as f:
                    profile = json.load(f)
            comparison = compare_track(analysis, profile)
            comparison["analysis"] = analysis
            output = comparison

        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
