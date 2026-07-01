from __future__ import annotations

import gc
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import requests
import soundfile as sf

try:
    from scipy.signal import resample_poly, butter, sosfilt, sosfilt_zi
except Exception:  # pragma: no cover - optional scipy fallback
    resample_poly = None
    butter = sosfilt = sosfilt_zi = None

try:
    from openai import OpenAI
except Exception:  # optional runtime dependency
    OpenAI = None

from .io import TARGET_SR, load_audio_file
from .analyzer import analyze_audio_fast_qc
from .source_morphology_restoration import apply_source_morphology_repair

_AUDIO_EXTS = {".wav", ".wave", ".flac", ".aif", ".aiff", ".ogg"}
_SCHEMA = "busy_auto_mixing_v8_5_3_64_6_4_transient_hf_ownership_lock"


def _env_on(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name, default)).strip().lower() not in {"0", "false", "off", "no", "n"}


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(float(str(os.environ.get(name, default)).strip()))
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except Exception:
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _db(x: float, floor: float = -120.0) -> float:
    try:
        x = float(x)
    except Exception:
        return floor
    if not math.isfinite(x) or x <= 0:
        return floor
    return 20.0 * math.log10(max(x, 1e-12))


def _amp(db_value: float) -> float:
    return float(10.0 ** (float(db_value) / 20.0))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, (float,)):
        return value if math.isfinite(value) else None
    return value


def _runtime_safe_lra_lu(y: np.ndarray, sr: int) -> float | None:
    """Bounded short-window loudness-range estimate for BAMix premaster QC.

    analyze_audio_fast_qc is intentionally cheap and may leave LRA null.  The
    mastering handoff governor needs a non-null premaster LRA, so estimate a
    stereo short-term range with capped windows.  This is telemetry/governance
    support, not a full EBU R128 replacement.
    """
    try:
        arr = _ensure_stereo(np.asarray(y, dtype=np.float32))
        n = int(arr.shape[0])
        sr_i = int(sr)
        if n <= 0 or sr_i <= 0:
            return None
        win_sec = _env_float("BUSY_AUTOMIX_PREMASTER_LRA_WINDOW_SEC", 3.0, minimum=1.0, maximum=6.0)
        hop_sec = _env_float("BUSY_AUTOMIX_PREMASTER_LRA_HOP_SEC", 1.0, minimum=0.25, maximum=3.0)
        max_windows = _env_int("BUSY_AUTOMIX_PREMASTER_LRA_MAX_WINDOWS", 64, minimum=4, maximum=160)
        win = max(1, int(float(win_sec) * sr_i))
        hop = max(1, int(float(hop_sec) * sr_i))
        if n < max(1024, win // 2):
            return None
        vals: list[float] = []
        starts = range(0, max(1, n - win + 1), hop) if n >= win else [0]
        for idx, start in enumerate(starts):
            if idx >= int(max_windows):
                break
            seg = arr[start:min(n, start + win)]
            if seg.size < 512:
                continue
            rms = float(np.sqrt(np.mean(np.square(seg), dtype=np.float64) + 1e-12))
            if rms <= 1e-9:
                continue
            vals.append(20.0 * math.log10(max(rms, 1e-12)) - 3.0)
        if len(vals) < 4:
            return None
        lo = float(np.percentile(vals, 10.0))
        hi = float(np.percentile(vals, 95.0))
        lra = hi - lo
        if not math.isfinite(lra):
            return None
        return float(max(0.0, lra))
    except Exception:
        return None


def _extract_json_from_text(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _safe_zip_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9가-힣._ -]+", "_", str(name or "stem.wav"))[:180]


def _role_priors_from_filename(name: str) -> dict[str, float]:
    f = str(name or "").lower().replace("_", " ").replace("-", " ")
    priors: dict[str, float] = {}

    def bump(role: str, amount: float) -> None:
        priors[role] = max(priors.get(role, 0.0), amount)

    if any(t in f for t in ["lead vocal", "lead vocals", "main vocal", "main vocals", "vocal", "vocals", "voice", "vox", "bgv", "backing vocal", "backing vocals"]):
        bump("vocal", 0.90)
    if any(t in f for t in ["kick"]):
        bump("kick", 0.92); bump("drums", 0.35)
    if any(t in f for t in ["snare", "clap"]):
        bump("snare", 0.86); bump("drums", 0.42)
    if any(t in f for t in ["hat", "hihat", "hi hat", "cymbal", "ride"]):
        bump("hats", 0.86); bump("drums", 0.34)
    if any(t in f for t in ["drum", "drums", "beat", "perc", "percussion"]):
        bump("drums", 0.86)
    if any(t in f for t in ["bass", "sub", "808"]):
        bump("bass", 0.92)
    if any(t in f for t in ["synth", "pad", "keys", "piano", "guitar", "strings", "organ", "music", "instrument", "melody", "chord", "other"]):
        bump("music_bed", 0.72)
    if any(t in f for t in ["fx", "sfx", "ambience", "ambient", "atmos", "reverb", "space", "riser", "impact"]):
        bump("fx_ambience", 0.78)
    return priors


def _filename_role_authority(name: str) -> tuple[str | None, float, str | None]:
    """High-trust filename authority for common exported stem ZIPs.

    v63.0.2: if stems are named Lead Vocals/Drums/Bass/Synth/etc., the filename
    should outrank crude proxy features.  Proxy features are still recorded for
    risk/intensity, but they should not route a clearly named vocal stem into the
    drum bus or an "Other" stem into kick.
    """
    stem = Path(str(name or "")).stem.lower().replace("_", " ").replace("-", " ")
    stem = re.sub(r"^[0-9]+\s+", "", stem).strip()
    rules: list[tuple[str, list[str], float]] = [
        ("vocal", ["lead vocal", "lead vocals", "main vocal", "main vocals", "vocal", "vocals", "voice", "vox", "bgv", "backing vocal", "backing vocals"], 0.96),
        ("kick", ["kick"], 0.95),
        ("snare", ["snare", "clap"], 0.92),
        ("hats", ["hi hat", "hihat", "hat", "cymbal", "ride"], 0.90),
        ("drums", ["drums", "drum", "beat", "percussion", "perc"], 0.92),
        ("bass", ["bass", "sub", "808"], 0.95),
        ("fx_ambience", ["fx", "sfx", "ambience", "ambient", "atmos", "reverb", "space", "riser", "impact"], 0.86),
        ("music_bed", ["synth", "pad", "keys", "piano", "guitar", "strings", "organ", "music", "instrument", "melody", "chord", "other"], 0.84),
    ]
    for role, tokens, conf in rules:
        if any(t == stem or t in stem for t in tokens):
            return role, conf, "filename_role_authority"
    return None, 0.0, None


def _ensure_stereo(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        return np.stack([x, x], axis=1)
    if x.shape[1] == 1:
        return np.repeat(x, 2, axis=1)
    return x[:, :2].astype(np.float32, copy=False)


def _rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(float(np.mean(x * x)) + 1e-18))


def _peak(x: np.ndarray) -> float:
    x = np.asarray(x)
    return float(np.max(np.abs(x))) if x.size else 0.0


def _corr(x: np.ndarray) -> float:
    y = _ensure_stereo(x)
    if y.shape[0] < 16:
        return 1.0
    l = y[:, 0].astype(np.float64, copy=False)
    r = y[:, 1].astype(np.float64, copy=False)
    den = math.sqrt(float(np.mean(l * l)) * float(np.mean(r * r))) + 1e-12
    return float(np.clip(float(np.mean(l * r)) / den, -1.0, 1.0))


def _band_proxy(x: np.ndarray, sr: int) -> dict[str, float]:
    y = _ensure_stereo(x)
    mono = np.mean(y, axis=1).astype(np.float64, copy=False)
    if mono.size < 1024 or _rms(mono) < 1e-7:
        return {"sub": 0.0, "low": 0.0, "lowmid": 0.0, "mid": 0.0, "presence": 0.0, "hiss_air": 0.0}
    n = int(min(mono.size, 262144))
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(mono[:n] * win)) ** 2 + 1e-18
    freqs = np.fft.rfftfreq(n, 1.0 / float(sr))
    total = float(np.sum(spec)) + 1e-18

    def frac(lo: float, hi: float) -> float:
        m = (freqs >= lo) & (freqs < hi)
        return float(np.sum(spec[m]) / total) if np.any(m) else 0.0

    return {
        "sub": frac(20, 60),
        "low": frac(60, 120),
        "lowmid": frac(120, 450),
        "mid": frac(450, 2000),
        "presence": frac(2000, 6000),
        "hiss_air": frac(9000, min(20000, sr / 2 - 1)),
    }


def _v645_fakeprint_peak_proxy(x: np.ndarray, sr: int) -> dict[str, Any]:
    """Cheap high-band narrow-peak witness for neural-codec-like residue."""
    try:
        y = _ensure_stereo(np.asarray(x, dtype=np.float32))
        if y.size == 0 or int(sr) <= 0:
            return {"available": False, "reason": "empty_audio"}
        side = ((y[:, 0] - y[:, 1]) * 0.5).astype(np.float32, copy=False)
        mono = np.mean(y, axis=1).astype(np.float32, copy=False)
        probe = side if _rms(side) > _rms(mono) * 0.08 else mono
        n = int(min(probe.size, _env_int("BUSY_BAMIX_V645_FAKEPRINT_FFT_N", 65536, minimum=8192, maximum=262144)))
        if n < 4096 or _rms(probe[:n]) < 1e-9:
            return {"available": False, "reason": "insufficient_probe"}
        seg = probe[:n].astype(np.float64, copy=False)
        seg = (seg - float(np.mean(seg))) * np.hanning(n)
        mag = np.abs(np.fft.rfft(seg)) + 1e-12
        freqs = np.fft.rfftfreq(n, d=1.0 / float(sr))
        mask = (freqs >= 5000.0) & (freqs <= min(18000.0, float(sr) * 0.46))
        if not np.any(mask):
            return {"available": False, "reason": "no_high_band"}
        hf = mag[mask]
        ff = freqs[mask]
        smooth = np.full_like(hf, max(float(np.median(hf)), 1e-12))
        if hf.size >= 17:
            try:
                from scipy import ndimage  # type: ignore
                smooth = ndimage.median_filter(hf, size=17, mode="nearest") + 1e-12
            except Exception:
                pass
        ratio = hf / np.maximum(smooth, 1e-12)
        peak_mask = ratio >= _env_float("BUSY_BAMIX_V645_FAKEPRINT_PEAK_RATIO", 3.2, minimum=1.5, maximum=12.0)
        peak_count = int(np.sum(peak_mask))
        bandwidth_khz = max(0.1, (float(ff[-1]) - float(ff[0])) / 1000.0)
        density = float(peak_count) / bandwidth_khz
        strength = float(np.percentile(ratio, 99.2)) if ratio.size else 0.0
        confidence = float(np.clip(0.58 * min(1.0, density / 6.0) + 0.42 * min(1.0, max(0.0, strength - 2.0) / 8.0), 0.0, 1.0))
        return {
            "available": True,
            "confidence": round(confidence, 4),
            "peak_count_5k_18k": peak_count,
            "peak_density_per_khz": round(density, 4),
            "peak_strength_p99_ratio": round(strength, 4),
            "policy": "fakeprint-like narrow high-band peak witness only; no provenance detection and no cancellation claim",
        }
    except Exception as exc:
        return {"available": False, "reason": "exception", "error": str(exc)[:120]}


def _onset_density_proxy(x: np.ndarray, sr: int) -> float:
    y = _ensure_stereo(x)
    mono = np.mean(y, axis=1).astype(np.float32, copy=False)
    if mono.size < 2048:
        return 0.0
    hop = max(256, int(sr * 0.02))
    n = (mono.size // hop) * hop
    if n <= hop:
        return 0.0
    frames = mono[:n].reshape(-1, hop)
    env = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    diff = np.diff(env, prepend=env[:1])
    med = float(np.median(diff))
    mad = float(np.median(np.abs(diff - med))) + 1e-9
    peaks = (diff > med + 3.0 * mad) & (env > np.percentile(env, 55))
    return float(np.sum(peaks) / max(mono.size / float(sr), 1e-6))


def _proxy_read_audio(path: Path, *, max_sec: float = 45.0, target_sr: int = TARGET_SR) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True, frames=int(max_sec * max(8000, target_sr)))
    data = _ensure_stereo(data)
    if sr != target_sr and resample_poly is not None and data.size:
        import math as _math
        g = _math.gcd(int(sr), int(target_sr))
        up = int(target_sr // g)
        down = int(sr // g)
        data = resample_poly(data, up, down, axis=0).astype(np.float32, copy=False)
        sr = target_sr
    return data, sr


def _infer_role(metric: dict[str, Any], filename: str) -> tuple[str, float]:
    hard_role, hard_conf, hard_reason = _filename_role_authority(filename)
    if hard_role and _env_on("BUSY_AUTOMIX_FILENAME_ROLE_AUTHORITY", "1"):
        metric["filename_role_authority"] = {"role": hard_role, "confidence": round(float(hard_conf), 4), "reason": hard_reason}
        return hard_role, float(hard_conf)
    b = metric.get("bands") or {}
    lff = float(b.get("sub", 0.0) + b.get("low", 0.0))
    lowmid = float(b.get("lowmid", 0.0))
    presence = float(b.get("presence", 0.0))
    air = float(b.get("hiss_air", 0.0))
    onset = float(metric.get("onset_density", 0.0))
    crest = float(metric.get("crest_factor_db", 0.0))
    width = float(metric.get("width_proxy", 0.0))
    scores = {
        "vocal": 0.34 * min(1, presence / 0.22) + 0.18 * min(1, air / 0.16) + 0.14 * min(1, lowmid / 0.34) - 0.22 * min(1, lff / 0.65),
        "kick": 0.56 * min(1, lff / 0.65) + 0.22 * min(1, crest / 13.0) + 0.14 * min(1, onset / 2.0),
        "bass": 0.66 * min(1, lff / 0.60) + 0.18 * (1.0 - min(1, onset / 2.5)),
        "drums": 0.43 * min(1, onset / 3.0) + 0.30 * min(1, crest / 14.0) + 0.12 * min(1, air / 0.20),
        "hats": 0.45 * min(1, air / 0.22) + 0.32 * min(1, onset / 3.0),
        "music_bed": 0.35 * min(1, (lowmid + presence) / 0.52) + 0.20 * (1.0 - min(1, onset / 3.5)) + 0.13 * min(1, width / 0.85),
        "fx_ambience": 0.42 * min(1, width / 0.85) + 0.24 * min(1, air / 0.20) + 0.16 * (1.0 - min(1, crest / 14.0)),
    }
    for role, p in _role_priors_from_filename(filename).items():
        scores[role] = scores.get(role, 0.0) + p
    # Avoid routing isolated low-end with high onset as pure bass; keep kick if filename/crest/onsets support it.
    if scores.get("kick", 0.0) > scores.get("bass", 0.0) * 0.92 and onset > 0.8 and crest > 9.0:
        scores["kick"] += 0.08
    best_role = max(scores, key=scores.get)
    sorted_vals = sorted(scores.values(), reverse=True)
    margin = (sorted_vals[0] - sorted_vals[1]) if len(sorted_vals) > 1 else sorted_vals[0]
    confidence = float(np.clip(0.42 + margin + max(scores[best_role], 0.0) * 0.35, 0.20, 0.98))
    return best_role, confidence


def _analyze_stem_proxy(path: Path, original_name: str) -> dict[str, Any]:
    info = sf.info(str(path))
    proxy_sec = _env_float("BUSY_AUTOMIX_PROXY_SEC", 45.0, minimum=5.0, maximum=120.0)
    audio, sr = _proxy_read_audio(path, max_sec=proxy_sec, target_sr=TARGET_SR)
    p = _peak(audio)
    r = _rms(audio)
    bands = _band_proxy(audio, sr)
    crest = _db(p / max(r, 1e-9)) if p > 0 and r > 0 else 0.0
    corr = _corr(audio)
    width = float(np.clip(1.0 - corr, 0.0, 1.5) / 1.5)
    onset = _onset_density_proxy(audio, sr)
    fakeprint_probe = _v645_fakeprint_peak_proxy(audio, sr)
    metric = {
        "filename": original_name,
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "frames": int(info.frames),
        "duration_sec": round(float(info.frames) / max(float(info.samplerate), 1.0), 3),
        "proxy_sample_rate": int(sr),
        "proxy_duration_sec": round(float(audio.shape[0]) / max(float(sr), 1.0), 3),
        "rms_db": round(_db(r), 3),
        "peak_dbfs": round(_db(p), 3),
        "crest_factor_db": round(crest, 3),
        "phase_correlation": round(corr, 4),
        "width_proxy": round(width, 4),
        "onset_density": round(onset, 4),
        "bands": {k: round(float(v), 6) for k, v in bands.items()},
    }
    role, conf = _infer_role(metric, original_name)
    artifact_risk = 0.0
    # Proxy risks: high air + low role certainty + negative correlation + low crest/strange dynamics.
    artifact_risk += 0.32 * min(1.0, float(bands.get("hiss_air", 0.0)) / 0.20)
    artifact_risk += 0.22 * max(0.0, 0.60 - conf) / 0.60
    artifact_risk += 0.22 * max(0.0, -corr)
    artifact_risk += 0.12 * max(0.0, 6.0 - crest) / 6.0
    artifact_risk += 0.12 * (1.0 if r < 1e-6 else 0.0)
    fakeprint_conf = float((fakeprint_probe or {}).get("confidence") or 0.0)
    side_hf_hash = float(np.clip(
        0.34 * min(1.0, float(bands.get("hiss_air", 0.0)) / 0.20)
        + 0.28 * max(0.0, -corr)
        + 0.20 * min(1.0, width / 0.80)
        + 0.18 * fakeprint_conf,
        0.0,
        1.0,
    ))
    hf_floor = float(np.clip(
        0.46 * min(1.0, float(bands.get("hiss_air", 0.0)) / 0.24)
        + 0.24 * min(1.0, max(0.0, 10.0 - crest) / 10.0)
        + 0.18 * max(0.0, 0.65 - conf) / 0.65
        + 0.12 * fakeprint_conf,
        0.0,
        1.0,
    ))
    metric.update({
        "role": role,
        "role_confidence": round(conf, 4),
        "artifact_risk": round(float(np.clip(artifact_risk, 0.0, 1.0)), 4),
        "v645_neural_codec_residue_probe": {
            "side_hf_hash_score": round(float(side_hf_hash), 4),
            "hf_floor_witness": round(float(hf_floor), 4),
            "fakeprint_like_peak_confidence": round(float(fakeprint_conf), 4),
            "fakeprint_like_peak_probe": fakeprint_probe,
            "policy": "deterministic witness for stem-safe routing; not additive-noise cancellation and not true source restoration",
        },
    })
    return metric


def _preflight_zip(stem_zip_path: Path, work_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".preflight",
        "zip_path": str(stem_zip_path),
        "zip_exists": bool(stem_zip_path.exists()),
        "technical_failure": False,
        "failure_reasons": [],
        "audio_member_count": 0,
        "total_uncompressed_bytes": 0,
        "max_duration_sec_header": 0.0,
        "policy": {
            "quality_defects_are_not_rejection_conditions": True,
            "technical_failure_only": True,
            "multi_candidate_full_render_forbidden": True,
        },
    }
    if not stem_zip_path.exists():
        report["technical_failure"] = True
        report["failure_reasons"].append("stem_zip_missing")
        return [], report
    if not zipfile.is_zipfile(stem_zip_path):
        report["technical_failure"] = True
        report["failure_reasons"].append("invalid_zip_archive")
        return [], report

    max_stems = _env_int("BUSY_AUTOMIX_MAX_STEMS", 32, minimum=1, maximum=96)
    max_uncompressed_mb = _env_float("BUSY_AUTOMIX_MAX_UNCOMPRESSED_MB", 2400.0, minimum=64.0, maximum=50000.0)
    extract_dir = work_dir / "busy_auto_mixing_stems"
    extract_dir.mkdir(parents=True, exist_ok=True)
    members: list[dict[str, Any]] = []
    with zipfile.ZipFile(stem_zip_path) as zf:
        for zi in zf.infolist():
            if zi.is_dir():
                continue
            suffix = Path(zi.filename).suffix.lower()
            if suffix not in _AUDIO_EXTS:
                continue
            if len(members) >= max_stems:
                report.setdefault("nonfatal_warnings", []).append("stem_count_exceeds_max_extra_members_ignored")
                break
            safe = _safe_zip_name(Path(zi.filename).name)
            local = extract_dir / f"{len(members):03d}_{safe}"
            with zf.open(zi) as src, open(local, "wb") as dst:
                # Copy in chunks; do not read the whole member into RAM.
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            try:
                info = sf.info(str(local))
                duration = float(info.frames) / max(float(info.samplerate), 1.0)
            except Exception as exc:
                members.append({"filename": zi.filename, "local_path": str(local), "readable": False, "error": str(exc)[:300], "size_bytes": int(zi.file_size)})
                continue
            members.append({
                "filename": zi.filename,
                "local_path": str(local),
                "readable": True,
                "size_bytes": int(zi.file_size),
                "sample_rate": int(info.samplerate),
                "channels": int(info.channels),
                "frames": int(info.frames),
                "duration_sec": round(duration, 3),
            })
            report["total_uncompressed_bytes"] += int(zi.file_size)
            report["max_duration_sec_header"] = max(float(report.get("max_duration_sec_header") or 0.0), duration)

    readable = [m for m in members if m.get("readable")]
    report["audio_member_count"] = len(members)
    report["readable_audio_member_count"] = len(readable)
    report["max_uncompressed_mb"] = max_uncompressed_mb
    report["total_uncompressed_mb"] = round(float(report["total_uncompressed_bytes"]) / (1024.0 * 1024.0), 3)
    if not readable:
        report["technical_failure"] = True
        report["failure_reasons"].append("no_readable_audio_stems")
    elif len(readable) < 2 and _env_on("BUSY_AUTOMIX_REQUIRE_AT_LEAST_TWO_STEMS", "1"):
        report["technical_failure"] = True
        report["failure_reasons"].append("less_than_two_readable_stems")
    if float(report["total_uncompressed_mb"]) > max_uncompressed_mb:
        report["technical_failure"] = True
        report["failure_reasons"].append("uncompressed_payload_exceeds_runtime_limit")
    return readable, report


def _extract_analysis_duration_candidates(features: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(features, dict):
        return out
    keys = ["duration_sec", "source_duration_sec", "input_duration_sec", "analysis_duration_sec"]
    for key in keys:
        try:
            v = features.get(key)
            if v is not None:
                out.append({"path": key, "duration_sec": round(float(v), 3)})
        except Exception:
            pass
    # A few existing reports place capped analysis duration in nested structures.
    nested_paths = [
        ["preflight_clean", "duration_sec"],
        ["post_pre_clean_analysis", "duration_sec"],
        ["segment_analysis", "duration_sec"],
        ["quality_indices", "duration_sec"],
    ]
    for path in nested_paths:
        cur: Any = features
        ok = True
        for part in path:
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur.get(part)
        if ok:
            try:
                out.append({"path": ".".join(path), "duration_sec": round(float(cur), 3)})
            except Exception:
                pass
    return out


def _duration_authority(original_path: Path, stems: list[dict[str, Any]], features: dict[str, Any] | None = None) -> dict[str, Any]:
    source_header = None
    try:
        info = sf.info(str(original_path))
        source_header = float(info.frames) / max(float(info.samplerate), 1.0)
    except Exception:
        source_header = None
    stem_durations = []
    for s in stems or []:
        try:
            d = float(s.get("duration_sec"))
            if math.isfinite(d) and d > 0:
                stem_durations.append(d)
        except Exception:
            pass
    stem_min = min(stem_durations) if stem_durations else None
    stem_max = max(stem_durations) if stem_durations else None
    stem_median = float(np.median(stem_durations)) if stem_durations else None
    analysis_candidates = _extract_analysis_duration_candidates(features)
    analysis_cap_detected = False
    for c in analysis_candidates:
        try:
            d = float(c.get("duration_sec"))
            # Common capped analysis windows, especially 120 sec, must never become render crop authority.
            if source_header and abs(d - source_header) > 2.0 and d <= source_header * 0.90:
                analysis_cap_detected = True
            if stem_max and abs(d - stem_max) > 2.0 and d <= stem_max * 0.90:
                analysis_cap_detected = True
        except Exception:
            pass
    decision = "use_source_header_and_stem_consensus_no_crop"
    if source_header is None and stem_median:
        decision = "use_stem_consensus_no_crop"
    return _jsonable({
        "schema_version": _SCHEMA + ".duration_authority",
        "source_header_duration_sec": round(source_header, 3) if source_header else None,
        "stem_min_duration_sec": round(stem_min, 3) if stem_min else None,
        "stem_median_duration_sec": round(stem_median, 3) if stem_median else None,
        "stem_max_duration_sec": round(stem_max, 3) if stem_max else None,
        "analysis_duration_candidates": analysis_candidates[:12],
        "analysis_duration_cap_detected": bool(analysis_cap_detected),
        "render_crop_allowed": False,
        "duration_decision": decision,
        "policy": "analysis/preview duration caps are telemetry only and never crop a Busy Auto Mixing render",
    })


def _reference_summary(features: dict[str, Any] | None, original_path: Path) -> dict[str, Any]:
    features = features if isinstance(features, dict) else {}
    summary: dict[str, Any] = {
        "source": "original_stereo_reference_anchor",
        "integrated_lufs": features.get("integrated_lufs") or (features.get("loudness", {}) or {}).get("integrated_lufs") if isinstance(features.get("loudness"), dict) else features.get("integrated_lufs"),
        "lra_lu": (features.get("loudness", {}) or {}).get("short_term_lufs", {}).get("lra_lu") if isinstance(features.get("loudness"), dict) and isinstance((features.get("loudness", {}) or {}).get("short_term_lufs"), dict) else features.get("lra_lu"),
        "approx_true_peak_dbfs": features.get("approx_true_peak_dbfs") or features.get("true_peak_dbfs"),
        "tempo_bpm": (features.get("rhythm", {}) or {}).get("tempo_bpm") if isinstance(features.get("rhythm"), dict) else None,
        "selected_profile": features.get("selected_profile") or (features.get("genre", {}) or {}).get("selected_profile") if isinstance(features.get("genre"), dict) else None,
    }
    try:
        proxy, sr = _proxy_read_audio(original_path, max_sec=_env_float("BUSY_AUTOMIX_REF_PROXY_SEC", 60.0, minimum=5.0, maximum=180.0), target_sr=TARGET_SR)
        bands = _band_proxy(proxy, sr)
        summary.update({
            "proxy_phase_correlation": round(_corr(proxy), 4),
            "proxy_bands": {k: round(float(v), 6) for k, v in bands.items()},
            "proxy_peak_dbfs": round(_db(_peak(proxy)), 3),
            "proxy_rms_db": round(_db(_rms(proxy)), 3),
            "vocal_frontness_proxy": round(float(bands.get("presence", 0.0) + bands.get("mid", 0.0)) / max(float(bands.get("lowmid", 0.0) + bands.get("low", 0.0) + 1e-9), 1e-9), 4),
            "bass_weight_proxy": round(float(bands.get("sub", 0.0) or 0.0) + float(bands.get("low", 0.0) or 0.0), 6),
        })
        # v63.3.4: side-texture needs are M/S-specific and cannot be inferred
        # from a mono spectral band proxy.  Use a bounded FFT proxy on the
        # original stereo reference to detect abrasive/high side dominance before
        # BAMix rendering.  This is telemetry/governance only; the actual DSP is
        # still block-wise and stateful in the renderer.
        try:
            ref_st = _ensure_stereo(proxy).astype(np.float32, copy=False)
            mid_ref = ((ref_st[:, 0] + ref_st[:, 1]) * 0.5).astype(np.float64, copy=False)
            side_ref = ((ref_st[:, 0] - ref_st[:, 1]) * 0.5).astype(np.float64, copy=False)
            n_ms = int(min(mid_ref.size, 262144))
            if n_ms >= 2048:
                win_ms = np.hanning(n_ms)
                freqs_ms = np.fft.rfftfreq(n_ms, 1.0 / float(sr))
                mask_hi = (freqs_ms >= 3500.0) & (freqs_ms <= 12000.0)
                mid_spec = np.abs(np.fft.rfft(mid_ref[:n_ms] * win_ms)) ** 2
                side_spec = np.abs(np.fft.rfft(side_ref[:n_ms] * win_ms)) ** 2
                mid_hi_e = float(np.sum(mid_spec[mask_hi])) + 1e-18
                side_hi_e = float(np.sum(side_spec[mask_hi])) + 1e-18
                total_side_e = float(np.sum(side_spec)) + 1e-18
                total_e = float(np.sum(mid_spec) + np.sum(side_spec)) + 1e-18
                summary["proxy_side_high_over_mid_high_db"] = round(float(10.0 * math.log10(side_hi_e / mid_hi_e)), 3)
                summary["proxy_side_high_fraction"] = round(float(side_hi_e / total_e), 6)
                summary["proxy_side_total_fraction"] = round(float(total_side_e / total_e), 6)
        except Exception as _side_exc:
            summary["proxy_side_texture_error"] = str(_side_exc)[:180]
        # v63.3.2: high L/R correlation can still hide a poor mono loudness
        # translation.  Measure the original stereo proxy against its mono
        # downmix so the stem augmentation planner can enable center-anchor
        # assist even when Pearson correlation looks superficially safe.
        try:
            proxy_fast = analyze_audio_fast_qc(_ensure_stereo(proxy).astype(np.float32, copy=False), sr)
            mono_proxy = np.mean(_ensure_stereo(proxy), axis=1).astype(np.float32, copy=False)
            mono_stereo = np.stack([mono_proxy, mono_proxy], axis=1)
            mono_fast = analyze_audio_fast_qc(mono_stereo.astype(np.float32, copy=False), sr)
            st_lufs = proxy_fast.get("integrated_lufs")
            mo_lufs = mono_fast.get("integrated_lufs")
            if st_lufs is not None and mo_lufs is not None:
                summary["proxy_stereo_lufs"] = round(float(st_lufs), 3)
                summary["proxy_mono_downmix_lufs"] = round(float(mo_lufs), 3)
                summary["proxy_stereo_minus_mono_lufs_db"] = round(float(st_lufs) - float(mo_lufs), 3)
        except Exception as _mono_exc:
            summary["proxy_mono_delta_error"] = str(_mono_exc)[:180]
    except Exception as exc:
        summary["proxy_error"] = str(exc)[:300]
    try:
        _info = sf.info(str(original_path))
        summary["source_header_duration_sec"] = round(float(_info.frames) / max(float(_info.samplerate), 1.0), 3)
        summary["source_header_samplerate"] = int(_info.samplerate)
        summary["source_header_frames"] = int(_info.frames)
    except Exception:
        pass
    summary["analysis_duration_candidates"] = _extract_analysis_duration_candidates(features)[:8]
    return _jsonable(summary)


def _build_role_summary(stem_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, list[dict[str, Any]]] = {}
    for m in stem_metrics:
        roles.setdefault(str(m.get("role") or "unknown"), []).append(m)
    out: dict[str, Any] = {}
    for role, items in roles.items():
        out[role] = {
            "count": len(items),
            "mean_confidence": round(float(np.mean([float(x.get("role_confidence") or 0.0) for x in items])), 4),
            "mean_artifact_risk": round(float(np.mean([float(x.get("artifact_risk") or 0.0) for x in items])), 4),
            "filenames": [str(x.get("filename"))[:120] for x in items[:8]],
        }
    return out


def _v645_stem_neural_codec_residue_map(
    stem_metrics: list[dict[str, Any]] | None,
    stem_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    enabled = _env_on("BUSY_BAMIX_V645_STEM_RESIDUE_MAP", "1")
    report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".stem_neural_codec_residue_map_v645",
        "enabled": bool(enabled),
        "active": False,
    }
    if not enabled:
        report["reason"] = "disabled_by_env"
        return report
    metrics = [m for m in (stem_metrics or []) if isinstance(m, dict)]
    if not metrics:
        report["reason"] = "no_stem_metrics"
        return report
    plan_by_name: dict[str, dict[str, Any]] = {}
    for p in stem_plan or []:
        if isinstance(p, dict):
            plan_by_name[str(p.get("filename") or "")] = p
    rows: list[dict[str, Any]] = []
    for m in metrics:
        probe = m.get("v645_neural_codec_residue_probe") if isinstance(m.get("v645_neural_codec_residue_probe"), dict) else {}
        role = str((plan_by_name.get(str(m.get("filename") or "")) or {}).get("role") or m.get("role") or "unknown")
        side_hash = float(np.clip(float(probe.get("side_hf_hash_score") or 0.0), 0.0, 1.0))
        fakeprint = float(np.clip(float(probe.get("fakeprint_like_peak_confidence") or 0.0), 0.0, 1.0))
        hf_floor = float(np.clip(float(probe.get("hf_floor_witness") or 0.0), 0.0, 1.0))
        artifact = float(np.clip(float(m.get("artifact_risk") or 0.0), 0.0, 1.0))
        corr = float(np.clip(float(m.get("phase_correlation") or 0.0), -1.0, 1.0))
        role_l = role.lower()
        vocal_like = role_l in {"vocal", "vocals", "lead_vocal", "lead_vocals"} or "vocal" in role_l or "voice" in role_l
        aggressive_roles = {"drums", "kick", "snare", "hats", "percussion", "synth", "synths", "pad", "pads", "keys", "music_bed", "fx_ambience", "unknown"}
        score = float(np.clip(0.34 * side_hash + 0.24 * fakeprint + 0.22 * hf_floor + 0.14 * artifact + 0.06 * max(0.0, -corr), 0.0, 1.0))
        route = "vocal_safe_dynamic_eq_only" if vocal_like else ("instrumental_aggressive_residue_suppression" if role in aggressive_roles else "instrumental_moderate_residue_suppression")
        rows.append({
            "filename": str(m.get("filename") or "")[:160],
            "role": role,
            "route": route,
            "residue_score": round(score, 4),
            "side_hf_hash_score": round(side_hash, 4),
            "fakeprint_like_peak_confidence": round(fakeprint, 4),
            "hf_floor_witness": round(hf_floor, 4),
            "artifact_risk": round(artifact, 4),
            "phase_correlation": round(corr, 4),
            "vocal_safe": bool(vocal_like),
            "source_morphology_repair_needed": bool((plan_by_name.get(str(m.get("filename") or "")) or {}).get("source_morphology_repair_needed")),
        })
    rows.sort(key=lambda x: float(x.get("residue_score") or 0.0), reverse=True)
    max_score = max([float(r.get("residue_score") or 0.0) for r in rows] or [0.0])
    max_side = max([float(r.get("side_hf_hash_score") or 0.0) for r in rows] or [0.0])
    max_fake = max([float(r.get("fakeprint_like_peak_confidence") or 0.0) for r in rows] or [0.0])
    max_floor = max([float(r.get("hf_floor_witness") or 0.0) for r in rows] or [0.0])
    mean_score = float(np.mean([float(r.get("residue_score") or 0.0) for r in rows])) if rows else 0.0
    instrumental_rows = [r for r in rows if not bool(r.get("vocal_safe"))]
    vocal_rows = [r for r in rows if bool(r.get("vocal_safe"))]
    instrumental_scores = [float(r.get("residue_score") or 0.0) for r in instrumental_rows]
    vocal_scores = [float(r.get("residue_score") or 0.0) for r in vocal_rows]
    instrumental_max = max(instrumental_scores or [0.0])
    instrumental_mean = float(np.mean(instrumental_scores)) if instrumental_scores else 0.0
    vocal_max = max(vocal_scores or [0.0])
    instrumental_side = max([float(r.get("side_hf_hash_score") or 0.0) for r in instrumental_rows] or [0.0])
    instrumental_fake = max([float(r.get("fakeprint_like_peak_confidence") or 0.0) for r in instrumental_rows] or [0.0])
    instrumental_floor = max([float(r.get("hf_floor_witness") or 0.0) for r in instrumental_rows] or [0.0])
    vocal_side = max([float(r.get("side_hf_hash_score") or 0.0) for r in vocal_rows] or [0.0])
    vocal_fake = max([float(r.get("fakeprint_like_peak_confidence") or 0.0) for r in vocal_rows] or [0.0])
    vocal_floor = max([float(r.get("hf_floor_witness") or 0.0) for r in vocal_rows] or [0.0])
    active = bool(max_score >= _env_float("BUSY_BAMIX_V645_STEM_RESIDUE_ACTIVE_AT", 0.18, minimum=0.0, maximum=1.0))
    report.update({
        "active": active,
        "stem_count": len(rows),
        "summary": {
            "max_residue_score": round(max_score, 4),
            "mean_residue_score": round(mean_score, 4),
            "max_side_hf_hash_score": round(max_side, 4),
            "max_fakeprint_like_peak_confidence": round(max_fake, 4),
            "max_hf_floor_witness": round(max_floor, 4),
            "vocal_safe_route_count": int(sum(1 for r in rows if r.get("vocal_safe"))),
            "instrumental_aggressive_route_count": int(sum(1 for r in rows if str(r.get("route")) == "instrumental_aggressive_residue_suppression")),
            "instrumental_residue_score_max": round(instrumental_max, 4),
            "instrumental_residue_score_mean": round(instrumental_mean, 4),
            "instrumental_side_hf_hash_score_max": round(instrumental_side, 4),
            "instrumental_fakeprint_like_peak_confidence_max": round(instrumental_fake, 4),
            "instrumental_hf_floor_witness_max": round(instrumental_floor, 4),
            "vocal_residue_score_max": round(vocal_max, 4),
            "vocal_side_hf_hash_score_max": round(vocal_side, 4),
            "vocal_fakeprint_like_peak_confidence_max": round(vocal_fake, 4),
            "vocal_hf_floor_witness_max": round(vocal_floor, 4),
            "instrumental_cleanup_priority": bool(instrumental_max >= _env_float("BUSY_BAMIX_V645_INSTRUMENTAL_RESIDUE_PRIORITY_AT", 0.18, minimum=0.0, maximum=1.0)),
            "vocal_protection_priority": bool(vocal_max >= _env_float("BUSY_BAMIX_V645_VOCAL_RESIDUE_PROTECTION_AT", 0.22, minimum=0.0, maximum=1.0)),
        },
        "top_stems": rows[:12],
        "policy": "stem-level residue map routes deterministic suppression only; no phase inversion, spectral subtraction, vocoder, BWE or true-restoration claim",
    })
    return _jsonable(report)


def _v645_residue_pressure_from_map(residue_map: dict[str, Any] | None) -> dict[str, Any]:
    m = residue_map if isinstance(residue_map, dict) else {}
    s = m.get("summary") if isinstance(m.get("summary"), dict) else {}
    max_score = float(s.get("max_residue_score") or 0.0)
    side = float(s.get("max_side_hf_hash_score") or 0.0)
    fake = float(s.get("max_fakeprint_like_peak_confidence") or 0.0)
    floor = float(s.get("max_hf_floor_witness") or 0.0)
    instrumental = float(s.get("instrumental_residue_score_max") or 0.0)
    vocal = float(s.get("vocal_residue_score_max") or 0.0)
    inst_side = float(s.get("instrumental_side_hf_hash_score_max") or 0.0)
    inst_fake = float(s.get("instrumental_fakeprint_like_peak_confidence_max") or 0.0)
    inst_floor = float(s.get("instrumental_hf_floor_witness_max") or 0.0)
    vocal_side = float(s.get("vocal_side_hf_hash_score_max") or 0.0)
    vocal_fake = float(s.get("vocal_fakeprint_like_peak_confidence_max") or 0.0)
    vocal_floor = float(s.get("vocal_hf_floor_witness_max") or 0.0)
    broad_pressure = float(np.clip(max(max_score, 0.58 * side + 0.24 * fake + 0.18 * floor), 0.0, 1.0))
    instrumental_pressure = float(np.clip(max(instrumental, 0.58 * inst_side + 0.22 * inst_fake + 0.20 * inst_floor), 0.0, 1.0))
    vocal_pressure = float(np.clip(max(vocal, 0.58 * vocal_side + 0.22 * vocal_fake + 0.20 * vocal_floor), 0.0, 1.0))
    vocal_protect = bool(vocal_pressure > instrumental_pressure + _env_float("BUSY_BAMIX_V645_VOCAL_DOMINANCE_MARGIN", 0.10, minimum=0.0, maximum=0.6))
    if vocal_protect:
        effective_side = float(np.clip(inst_side, 0.0, 1.0))
        effective_fake = float(np.clip(inst_fake, 0.0, 1.0))
        effective_floor = float(np.clip(inst_floor, 0.0, 1.0))
        pressure = float(np.clip(max(instrumental_pressure, 0.58 * effective_side + 0.22 * effective_fake + 0.20 * effective_floor), 0.0, 1.0))
    else:
        effective_side = float(np.clip(side, 0.0, 1.0))
        effective_fake = float(np.clip(fake, 0.0, 1.0))
        effective_floor = float(np.clip(floor, 0.0, 1.0))
        pressure = max(broad_pressure, instrumental_pressure)
    return {
        "active": bool(m.get("active")) and pressure >= _env_float("BUSY_BAMIX_V645_RESIDUE_PRESSURE_ACTIVE_AT", 0.16, minimum=0.0, maximum=1.0),
        "residue_pressure": round(pressure, 4),
        "instrumental_residue_pressure": round(instrumental_pressure, 4),
        "vocal_residue_pressure": round(vocal_pressure, 4),
        "vocal_protection_active": vocal_protect,
        "side_hf_hash_pressure": round(float(np.clip(side, 0.0, 1.0)), 4),
        "fakeprint_pressure": round(float(np.clip(fake, 0.0, 1.0)), 4),
        "hf_floor_pressure": round(float(np.clip(floor, 0.0, 1.0)), 4),
        "effective_side_hf_hash_pressure": round(effective_side, 4),
        "effective_fakeprint_pressure": round(effective_fake, 4),
        "effective_hf_floor_pressure": round(effective_floor, 4),
        "source": "v645_stem_neural_codec_residue_map",
    }


def _rule_select_recipe(stem_metrics: list[dict[str, Any]], reference: dict[str, Any]) -> dict[str, Any]:
    roles = _build_role_summary(stem_metrics)
    mean_conf = float(np.mean([float(x.get("role_confidence") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    max_art = max([float(x.get("artifact_risk") or 0.0) for x in stem_metrics] or [0.0])
    mean_art = float(np.mean([float(x.get("artifact_risk") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    vocal_conf = float((roles.get("vocal") or {}).get("mean_confidence") or 0.0)
    bass_conf = max(float((roles.get("bass") or {}).get("mean_confidence") or 0.0), float((roles.get("kick") or {}).get("mean_confidence") or 0.0))
    drums_conf = max(float((roles.get("drums") or {}).get("mean_confidence") or 0.0), float((roles.get("kick") or {}).get("mean_confidence") or 0.0), float((roles.get("snare") or {}).get("mean_confidence") or 0.0), float((roles.get("hats") or {}).get("mean_confidence") or 0.0))
    bass_weight = float(reference.get("bass_weight_proxy") or 0.0)
    frontness = float(reference.get("vocal_frontness_proxy") or 0.0)
    onset = max([float(x.get("onset_density") or 0.0) for x in stem_metrics] or [0.0])
    recipe = "clean_balanced_professional"
    reasons: list[str] = []
    if max_art > _env_float("BUSY_AUTOMIX_ARTIFACT_CONSERVATIVE_AT", 0.90, minimum=0.5, maximum=0.99) and mean_conf < _env_float("BUSY_AUTOMIX_ARTIFACT_CONSERVATIVE_MEAN_CONF_BELOW", 0.50, minimum=0.1, maximum=0.9):
        recipe = "ai_artifact_conservative"
        reasons.append("severe_artifact_risk_and_low_role_confidence")
    elif vocal_conf >= 0.72 and frontness >= 1.35:
        recipe = "vocal_forward_bass_controlled" if bass_weight >= 0.24 or bass_conf >= 0.66 else "vocal_forward"
        reasons.append("reference_vocal_frontness_and_vocal_role_confidence")
    elif bass_weight >= 0.30 and bass_conf >= 0.60:
        recipe = "bass_controlled"
        reasons.append("strong_low_end_reference_and_bass_or_kick_confidence")
    elif drums_conf >= 0.70 and onset >= 1.7:
        recipe = "punch_preserved"
        reasons.append("drum_transient_density_and_drum_role_confidence")
    elif mean_conf < 0.52:
        recipe = "wide_bed_safe"
        reasons.append("low_mean_role_confidence")
    else:
        reasons.append("balanced_recipe_default_after_no_specialist_trigger")
    return {
        "selected_mix_recipe": recipe,
        "selector": "rule_based_fallback",
        "reasons": reasons,
        "scores": {
            "mean_role_confidence": round(mean_conf, 4),
            "mean_artifact_risk": round(mean_art, 4),
            "max_artifact_risk": round(max_art, 4),
            "vocal_confidence": round(vocal_conf, 4),
            "bass_confidence": round(bass_conf, 4),
            "drum_confidence": round(drums_conf, 4),
            "reference_bass_weight_proxy": round(bass_weight, 4),
            "reference_vocal_frontness_proxy": round(frontness, 4),
        },
    }


def _call_gpt_planner(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = _env_on("BUSY_AUTOMIX_AI_PLANNER", "1")
    if not enabled:
        return {"available": False, "reason": "BUSY_AUTOMIX_AI_PLANNER_disabled"}
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("BUSY_OPENAI_API_KEY")
    if not api_key:
        return {"available": False, "reason": "missing_openai_api_key"}
    if OpenAI is None:
        return {"available": False, "reason": "openai_package_missing"}
    model = os.environ.get("BUSY_AUTOMIX_AI_MODEL", "gpt-5.5") or "gpt-5.5"
    timeout = _env_float("BUSY_AUTOMIX_AI_TIMEOUT_SEC", 60.0, minimum=5.0, maximum=180.0)
    system = (
        "You are the Busy Auto Mixing v64.3 single-call producer-grade mix strategy consultant. "
        "Choose exactly one mix recipe, professional module emphasis, and a bounded deterministic stem-augmentation strategy before rendering. "
        "Return strict compact JSON only; no prose outside JSON. Do not request multiple full-length renders. "
        "Use stems as the intended mixing source; original stereo is only a reference anchor. "
        "You may recommend module enablement, deterministic stem-derived augmentation, strategy and relative intensity, but never unbounded raw DSP dB/threshold/limiter values. "
        "The deterministic validator owns numeric clamps and safety gates. Do not make a timid stem sum; professional bus moves are expected when evidence supports them. Stem augmentation must derive only from provided stems with bounded DSP, never external/generated musical content. "
        "If gemini_stem_mix_observation_advisory is available, treat it as weak producer/listener prior from SUM/L/R/MID/SIDE rough-stem observation: cross-check it against metrics, then translate supported findings into existing module emphasis and stem_augmentation decisions only."
    )
    allowed = ["clean_balanced_professional", "vocal_forward", "vocal_forward_bass_controlled", "bass_controlled", "punch_preserved", "dense_pop", "club_low_end_controlled", "wide_bed_safe", "acoustic_natural", "cinematic_wide", "ai_artifact_conservative"]
    module_names = ["glue", "vocal_pocket", "kick_bass", "drum_punch", "harmonic_density", "elliptical", "stereo_safety", "translation_qc"]
    augmentation_module_names = ["bass_harmonic_translation", "low_mid_body_fill", "center_anchor", "drum_parallel_density", "short_room_early_reflection", "vocal_support_body_layer", "transient_ghost", "side_texture_control"]
    user = {
        "task": "Select one Busy Auto Mixing recipe, one compact v63.3 professional mixing module strategy, and one bounded deterministic stem augmentation strategy.",
        "allowed_recipes": allowed,
        "allowed_modules": module_names,
        "allowed_stem_augmentation_modules": augmentation_module_names,
        "allowed_intensity_values": ["off", "light", "medium", "strong"],
        "required_json_shape": {
            "selected_mix_recipe": "one allowed recipe",
            "confidence": "0..1",
            "recipe_reasoning_summary": "short, max 240 chars",
            "bus_priority": ["vocal|drums|bass|music_bed|fx_ambience"],
            "module_emphasis": {
                "glue": "off|light|medium|strong",
                "vocal_pocket": "off|light|medium|strong",
                "kick_bass": "off|light|medium|strong",
                "drum_punch": "off|light|medium|strong",
                "harmonic_density": "off|light|medium|strong",
                "elliptical": "off|light|medium|strong",
                "stereo_safety": "off|light|medium|strong",
                "translation_qc": "off|light|medium|strong"
            },
            "module_strategy": {
                "vocal_pocket": "bed_dynamic_eq|off|protective",
                "kick_bass": "bass_low_band_duck|off|protective",
                "drum_punch": "soft_peak_rounding|transient_preserve|off",
                "harmonic_density": "drum_bus|mixbus|drum_and_mixbus|off",
                "stereo_safety": "neutral|wide_bed_safe|narrow_safe",
                "handoff_style": "transparent_dynamic|balanced_density|artifact_safe"
            },
            "stem_augmentation": {
                "strategy": "none|fill_center_body_and_translation|punch_and_density_support|artifact_safe_defer",
                "modules": {
                    "bass_harmonic_translation": {"decision": "enable|defer|reject", "intensity": "off|light|medium|strong", "reason": "short"},
                    "low_mid_body_fill": {"decision": "enable|enable_with_ducking|defer|reject", "intensity": "off|light|medium|strong", "reason": "short"},
                    "center_anchor": {"decision": "enable|defer|reject", "intensity": "off|light|medium|strong", "reason": "short"},
                    "drum_parallel_density": {"decision": "enable|defer|reject", "intensity": "off|light|medium|strong", "reason": "short"},
                    "short_room_early_reflection": {"decision": "defer|reject", "intensity": "off|light", "reason": "short"},
                    "vocal_support_body_layer": {"decision": "enable|enable_with_ducking|defer|reject", "intensity": "off|light|medium|strong", "reason": "short"},
                    "transient_ghost": {"decision": "enable|defer|reject", "intensity": "off|light|medium", "reason": "short"},
                    "side_texture_control": {"decision": "enable|defer|reject", "intensity": "off|light|medium", "reason": "short"}
                },
                "safety_notes": []
            },
            "gemini_advisory_use": {
                "used": "true|false",
                "accepted_findings": ["short"],
                "rejected_or_downweighted_findings": ["short"],
                "translated_to_modules": ["module names or short actions"]
            },
            "risk_flags": [],
            "avoid": ["static_vocal_boost|heavy_mixbus_compression|low_end_widening|hf_artifact_saturation"],
            "handoff_intent": {"crest_priority": "low|medium|high", "loudness_priority": "low|medium|high", "punch_priority": "low|medium|high"}
        },
        "metrics": payload,
    }
    try:
        client = OpenAI(api_key=api_key, timeout=timeout)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(_jsonable(user), ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        # Some models reject response_format on chat.completions. Retry without it.
        try:
            resp = client.chat.completions.create(**kwargs)
        except TypeError:
            kwargs.pop("response_format", None)
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "response_format" in str(exc).lower() or "json" in str(exc).lower():
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        text = resp.choices[0].message.content if getattr(resp, "choices", None) else ""
        parsed = _extract_json_from_text(text or "")
        recipe = str(parsed.get("selected_mix_recipe") or "").strip()
        if recipe not in allowed:
            return {"available": False, "reason": "invalid_recipe_from_ai", "model": model, "raw_preview": str(text)[:500], "parsed": parsed}
        return {"available": True, "model": model, "single_call": True, "ai_call_count": 1, "planner": parsed}
    except Exception as exc:
        return {"available": False, "reason": "ai_planner_exception", "model": model, "error": str(exc)[:500], "ai_call_count": 1}


def _clamp_recipe(ai: dict[str, Any], fallback: dict[str, Any], stem_metrics: list[dict[str, Any]], reference: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = fallback.get("selected_mix_recipe") or "clean_balanced_professional"
    planner_source = "rules"
    ai_payload = ai if isinstance(ai, dict) else {}
    if ai_payload.get("available") and isinstance(ai_payload.get("planner"), dict):
        candidate = str(ai_payload["planner"].get("selected_mix_recipe") or "").strip()
        if candidate:
            selected = candidate
            planner_source = "gpt_5_5_single_call"
    # Deterministic safety clamp: severe artifact + weak roles cannot use air/wide/punch-heavy plans.
    mean_conf = float(np.mean([float(x.get("role_confidence") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    max_art = max([float(x.get("artifact_risk") or 0.0) for x in stem_metrics] or [0.0])
    safety_clamps: list[str] = []
    if max_art >= _env_float("BUSY_AUTOMIX_FORCE_CONSERVATIVE_ARTIFACT_AT", 0.96, minimum=0.80, maximum=0.995) and mean_conf < _env_float("BUSY_AUTOMIX_FORCE_CONSERVATIVE_CONF_BELOW", 0.50, minimum=0.1, maximum=0.9) and selected not in {"ai_artifact_conservative", "clean_balanced_professional"}:
        selected = "ai_artifact_conservative"
        safety_clamps.append("extreme_artifact_low_confidence_forced_conservative")
    v631_strategy = _build_v631_mix_strategy(selected, stem_metrics, ai_payload=ai_payload, reference=reference)
    return {
        "selected_mix_recipe": selected,
        "planner_source": planner_source,
        "ai_planner": ai_payload,
        "rule_fallback": fallback,
        "safety_clamps": safety_clamps,
        "v631_mix_strategy": v631_strategy,
        "gpt_5_5_role": "mix_strategy_consultant_and_module_emphasis_planner",
        "policy": {
            "single_call_ai_only": True,
            "dual_planner_forbidden": True,
            "gpt_may_choose": ["recipe", "module_emphasis", "bus_priority", "relative_strategy", "stem_augmentation_strategy", "avoid_list", "handoff_intent"],
            "gpt_must_not_choose": ["unbounded_dsp_db_values", "limiter_settings", "render_count", "hard_safety_gates", "memory_strategy"],
            "final_authority": "deterministic_validator_clamp_then_dsp_renderer",
        },
    }





def _v6340_contract_genre_bucket(recipe: str | None) -> str:
    """Legacy/fallback bucket used only when private reference DB is unavailable."""
    r = str(recipe or "clean_balanced_professional").strip().lower()
    if r in {"club_low_end_controlled"}:
        return "edm_club"
    if r in {"dense_pop", "vocal_forward", "vocal_forward_bass_controlled", "bass_controlled"}:
        return "kpop_vocal_pop"
    if r in {"punch_preserved"}:
        return "rock_band"
    if r in {"acoustic_natural"}:
        return "ballad_acoustic"
    if r in {"wide_bed_safe", "cinematic_wide"}:
        return "wide_cinematic"
    if r in {"ai_artifact_conservative"}:
        return "ai_artifact_safe"
    return "balanced_pop"


_PRIVATE_REF_DB_CACHE: dict[str, tuple[dict[str, Any] | None, str | None, str | None]] = {}


def _norm_profile_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _v6341_load_private_reference_db(private_docs_zip_path: str | Path | None = None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Load the newest private genre mastering reference DB for BAMix handoff.

    master_job already downloads private_docs_encrypted.zip before BAMix.  v63.4.1
    consumes that same local ZIP instead of inventing a new genre/limiter table.
    The loader is intentionally cached and fail-soft; if private assets are absent
    the v63.4.0 fallback contract remains available.
    """
    candidates: list[str] = []
    if private_docs_zip_path:
        candidates.append(str(private_docs_zip_path))
    for name in (
        "BUSY_BAMIX_PRIVATE_DOCS_ZIP",
        "PRIVATE_DOCS_RESOLVED_PATH",
        "PRIVATE_DOCS_LOCAL_PATH",
        "PRIVATE_DOCS_ZIP",
    ):
        val = os.environ.get(name)
        if val:
            candidates.append(str(val))
    candidates.append("private_docs_encrypted.zip")
    password = os.environ.get("PRIVATE_DOCS_ZIP_PASSWORD")
    for raw in candidates:
        try:
            path = Path(str(raw))
            if not path.exists() or not path.is_file():
                continue
            key = str(path.resolve()) + "::" + ("pwd" if password else "nopwd")
            if key in _PRIVATE_REF_DB_CACHE:
                return _PRIVATE_REF_DB_CACHE[key]
            try:
                from ai_decision.private_rules import load_private_bundle  # type: ignore
                bundle = load_private_bundle(path, password)
                json_assets = bundle.get("json", {}) if isinstance(bundle, dict) else {}
            except Exception:
                json_assets = {}
                try:
                    import zipfile
                    pwd = password.encode("utf-8") if password else None
                    with zipfile.ZipFile(path, "r") as zf:
                        for name in zf.namelist():
                            if name.lower().endswith(".json"):
                                with zf.open(name, pwd=pwd) as fh:
                                    json_assets[name] = json.loads(fh.read().decode("utf-8", errors="replace"))
                except Exception:
                    json_assets = {}
            preferred = [
                "genre_mastering_reference_v9",
                "genre_mastering_reference_v7",
                "genre_mastering_reference_v6",
                "genre_mastering_reference",
            ]
            for marker in preferred:
                for asset_name, asset in sorted((json_assets or {}).items()):
                    low = str(asset_name).lower()
                    if marker in low and low.endswith(".json") and isinstance(asset, dict):
                        result = (asset, str(asset_name), str(path))
                        _PRIVATE_REF_DB_CACHE[key] = result
                        return result
            result = (None, None, str(path))
            _PRIVATE_REF_DB_CACHE[key] = result
        except Exception:
            continue
    return None, None, None


def _v6341_range_mid(r: Any, default: float) -> float:
    try:
        if isinstance(r, dict):
            a = float(r.get("min"))
            b = float(r.get("max"))
            return (a + b) / 2.0
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            return (float(r[0]) + float(r[1])) / 2.0
    except Exception:
        pass
    return float(default)


def _v6341_range_minmax(r: Any, default_min: float, default_max: float) -> tuple[float, float]:
    try:
        if isinstance(r, dict):
            return float(r.get("min")), float(r.get("max"))
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            return float(r[0]), float(r[1])
    except Exception:
        pass
    return float(default_min), float(default_max)


def _v6341_extract_decision_profile_candidates(original_decision: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(original_decision, dict):
        return []
    out: list[dict[str, Any]] = []

    def add(profile: Any, weight: float, source: str, role: str = "primary") -> None:
        key = _norm_profile_key(profile)
        if not key:
            return
        out.append({"profile": key, "weight": float(weight), "source": source, "role": role})

    rv7 = original_decision.get("reference_v7") if isinstance(original_decision.get("reference_v7"), dict) else {}
    integrity = original_decision.get("decision_integrity") if isinstance(original_decision.get("decision_integrity"), dict) else {}
    add(rv7.get("selected_profile"), 1.0, "decision.reference_v7.selected_profile", "primary")
    add(integrity.get("reference_v7_selected_profile"), 0.98, "decision.decision_integrity.reference_v7_selected_profile", "primary")
    add(original_decision.get("active_profile"), 0.95, "decision.active_profile", "primary")
    add(original_decision.get("genre_id"), 0.92, "decision.genre_id", "primary")
    add(original_decision.get("primary_profile"), 0.90, "decision.primary_profile", "primary")
    add(original_decision.get("selected_profile"), 0.85, "decision.selected_profile", "primary")

    # Treat secondary profile blends as limited trait grafts rather than full-chain
    # replacements.  They can gently bias limiter budget/crest/clipper choices,
    # but the reference_v7 selected profile stays dominant.
    if _env_on("BUSY_BAMIX_V6341_USE_PROFILE_BLEND", "1"):
        items: list[Any] = []
        for container_path in (
            (original_decision.get("profile_form_blend") or {}).get("items") if isinstance(original_decision.get("profile_form_blend"), dict) else None,
            (rv7.get("profile_form_blend") or {}).get("items") if isinstance(rv7.get("profile_form_blend"), dict) else None,
            original_decision.get("profile_blend"),
            original_decision.get("genre_mix"),
        ):
            if isinstance(container_path, list):
                items.extend(container_path[:8])
        seen_blend: set[str] = set()
        for idx, item in enumerate(items[:16]):
            if not isinstance(item, dict):
                continue
            prof = _norm_profile_key(item.get("profile"))
            if not prof or prof in seen_blend:
                continue
            seen_blend.add(prof)
            raw_w = item.get("profile_form_weight", item.get("weight", item.get("score", 0.12)))
            try:
                w = float(raw_w)
            except Exception:
                w = 0.12
            if w > 1.0:
                w = w / 100.0
            add(prof, max(0.03, min(0.35, w)), f"decision.profile_blend[{idx}]", "trait_graft")
    # stable de-dup: keep the strongest weight per normalized profile and source role
    best: dict[str, dict[str, Any]] = {}
    for row in out:
        key = str(row.get("profile"))
        if key not in best or float(row.get("weight") or 0.0) > float(best[key].get("weight") or 0.0):
            best[key] = row
    return sorted(best.values(), key=lambda x: float(x.get("weight") or 0.0), reverse=True)


def _v6341_resolve_profile_key(db: dict[str, Any] | None, raw_key: Any) -> tuple[str | None, dict[str, Any] | None, str]:
    if not isinstance(db, dict):
        return None, None, "no_db"
    key = _norm_profile_key(raw_key)
    if not key:
        return None, None, "empty"
    exact = db.get("exact_profiles") if isinstance(db.get("exact_profiles"), dict) else {}
    families = db.get("family_profiles") if isinstance(db.get("family_profiles"), dict) else {}
    aliases = db.get("genre_alias_map") if isinstance(db.get("genre_alias_map"), dict) else {}
    redirects = db.get("profile_redirects") if isinstance(db.get("profile_redirects"), dict) else {}

    def lookup(k: str) -> tuple[str | None, dict[str, Any] | None, str]:
        kk = _norm_profile_key(k)
        if not kk:
            return None, None, "empty"
        if kk in redirects:
            kk = _norm_profile_key(redirects.get(kk))
        if kk in aliases:
            kk = _norm_profile_key(aliases.get(kk))
        if kk in exact and isinstance(exact.get(kk), dict):
            return kk, exact.get(kk), "exact_or_alias"
        if kk in families and isinstance(families.get(kk), dict):
            return kk, families.get(kk), "family"
        return None, None, "missing"

    resolved = lookup(key)
    if resolved[1] is not None:
        return resolved

    # Common public/evidence labels that are broader than private exact IDs.
    manual = {
        "electro_house_bounce": "electro_house",
        "rock_band_pop_rock": "pop_rock",
        "kpop_rock": "pop_rock",
        "pop_rock_band": "pop_rock",
        "uk_garage_future_garage": "uk_garage_pop",
        "future_garage": "uk_garage_pop",
        "garage_pop": "uk_garage_pop",
        "trap_hiphop": "trap_hiphop_bass",
        "reggaeton_afrobeat_moombahton": "reggaeton_moombahton",
        "pop_ballad_vocal_pop": "kpop_vocal_ballad",
        "hyperpop_bright_digital_pop": "hyperpop_glitch_pop",
    }
    if key in manual:
        resolved = lookup(manual[key])
        if resolved[1] is not None:
            return resolved[0], resolved[1], "manual_alias"

    # Fuzzy containment: prefer the longest private exact profile contained in the
    # evidence key, e.g. electro_house_bounce -> electro_house.
    for cand in sorted(list(exact.keys()), key=len, reverse=True):
        ck = _norm_profile_key(cand)
        if ck and (ck in key or key in ck):
            prof = exact.get(cand)
            if isinstance(prof, dict):
                return cand, prof, "fuzzy_exact_contains"
    fam = None
    for cand in sorted(list(families.keys()), key=len, reverse=True):
        ck = _norm_profile_key(cand)
        if ck and (ck in key or key in ck):
            fam = cand
            break
    if fam and isinstance(families.get(fam), dict):
        return fam, families.get(fam), "fuzzy_family_contains"
    return None, None, "not_found"


def _v6341_mode_from_decision(original_decision: dict[str, Any] | None, profile_env: str) -> str:
    if profile_env in {"stream", "streaming", "streaming_normalized", "streaming-safe", "streaming_safe"}:
        return "streaming_safe"
    if profile_env in {"club", "club_hot", "hot"}:
        return "club_hot"
    if isinstance(original_decision, dict):
        rv7 = original_decision.get("reference_v7") if isinstance(original_decision.get("reference_v7"), dict) else {}
        mode = _norm_profile_key(rv7.get("delivery_mode") or original_decision.get("delivery_mode") or original_decision.get("mode"))
        if mode in {"streaming", "streaming_normalized", "streaming_safe"}:
            return "streaming_safe"
        if mode in {"club_hot", "club"}:
            return "club_hot"
    return "commercial_loud"


def _v6341_profile_contract_from_reference_db(
    db: dict[str, Any] | None,
    *,
    original_decision: dict[str, Any] | None,
    recipe: str | None,
    profile: str,
) -> dict[str, Any] | None:
    if not isinstance(db, dict) or not _env_on("BUSY_BAMIX_V6341_REFERENCE_DB_CONTRACT", "1"):
        return None
    mode = _v6341_mode_from_decision(original_decision, profile)
    candidates = _v6341_extract_decision_profile_candidates(original_decision)
    if not candidates:
        candidates = [{"profile": _v6340_contract_genre_bucket(recipe), "weight": 1.0, "source": "fallback_recipe_bucket", "role": "fallback"}]

    resolved_rows: list[dict[str, Any]] = []
    primary_seen = False
    trait_total_cap = _env_float("BUSY_BAMIX_V6341_TRAIT_BLEND_TOTAL_WEIGHT", 0.35, minimum=0.0, maximum=0.8)
    trait_weights: list[float] = []
    for row in candidates:
        key, prof, source = _v6341_resolve_profile_key(db, row.get("profile"))
        if not isinstance(prof, dict) or not key:
            continue
        role = str(row.get("role") or "primary")
        weight = float(row.get("weight") or 0.0)
        if role == "trait_graft":
            trait_weights.append(max(0.0, weight))
        else:
            if primary_seen:
                weight = min(weight, 0.20)
                role = "secondary_primary_candidate"
            else:
                weight = max(weight, 1.0)
                primary_seen = True
        resolved_rows.append({"key": key, "profile": prof, "weight_raw": weight, "role": role, "source": row.get("source"), "resolve_source": source, "input_profile": row.get("profile")})

    if not resolved_rows:
        key, prof, source = _v6341_resolve_profile_key(db, "universal_safe_master")
        if not isinstance(prof, dict) or not key:
            return None
        resolved_rows = [{"key": key, "profile": prof, "weight_raw": 1.0, "role": "fallback", "source": "universal_safe_master", "resolve_source": source, "input_profile": "universal_safe_master"}]

    # Cap trait-graft influence so secondary profiles modify, not replace, the
    # reference_v7 primary handoff target.
    total_trait = sum(float(r.get("weight_raw") or 0.0) for r in resolved_rows if r.get("role") == "trait_graft")
    if total_trait > trait_total_cap and total_trait > 1e-9:
        scale = trait_total_cap / total_trait
        for r in resolved_rows:
            if r.get("role") == "trait_graft":
                r["weight_raw"] = float(r.get("weight_raw") or 0.0) * scale
    total_w = sum(max(0.0, float(r.get("weight_raw") or 0.0)) for r in resolved_rows) or 1.0

    def mode_profile(prof: dict[str, Any]) -> dict[str, Any]:
        mps = prof.get("mode_profiles") if isinstance(prof.get("mode_profiles"), dict) else {}
        mp = mps.get(mode)
        if not isinstance(mp, dict):
            mp = mps.get("commercial_loud") if isinstance(mps.get("commercial_loud"), dict) else None
        if not isinstance(mp, dict):
            mp = mps.get("streaming_safe") if isinstance(mps.get("streaming_safe"), dict) else {}
        return mp if isinstance(mp, dict) else {}

    vals: list[dict[str, Any]] = []
    for r in resolved_rows:
        prof = r["profile"]
        mp = mode_profile(prof)
        dyn = prof.get("dynamics_target") if isinstance(prof.get("dynamics_target"), dict) else {}
        clip = prof.get("clipping_limiter_profile") if isinstance(prof.get("clipping_limiter_profile"), dict) else {}
        safe = prof.get("safety_constraints") if isinstance(prof.get("safety_constraints"), dict) else {}
        w = max(0.0, float(r.get("weight_raw") or 0.0)) / total_w
        lmin, lmax = _v6341_range_minmax(mp.get("integrated_lufs_range"), -10.8, -8.2)
        cmin, cmax = _v6341_range_minmax(dyn.get("crest_factor_range"), 7.0, 10.0)
        lra_min, lra_max = _v6341_range_minmax(dyn.get("lra_range"), 3.0, 6.0)
        max_gr = safe.get("max_limiter_gr_db") or safe.get("max_limiter_gain_reduction_db")
        try:
            max_gr = float(max_gr)
        except Exception:
            # Some family profiles omit max_limiter_gr_db; infer from limiter strength.
            strength = str(dyn.get("limiter_strength") or "adaptive").lower()
            max_gr = 5.0 if strength == "strong" else 3.5
        vals.append({
            "w": w,
            "profile_id": r.get("key"),
            "role": r.get("role"),
            "source": r.get("source"),
            "resolve_source": r.get("resolve_source"),
            "delivery_lufs_min": lmin,
            "delivery_lufs_max": lmax,
            "delivery_lufs_mid": (lmin + lmax) / 2.0,
            "final_tp_ceiling": float(mp.get("true_peak_ceiling_dbtp") or -0.8),
            "crest_min": cmin,
            "crest_max": cmax,
            "crest_mid": (cmin + cmax) / 2.0,
            "lra_min": lra_min,
            "lra_max": lra_max,
            "lra_mid": (lra_min + lra_max) / 2.0,
            "max_gr": max_gr,
            "clipper_before_limiter": bool(clip.get("clipper_before_limiter")),
            "oversampling_min": int(float(clip.get("oversampling_min") or 4)),
            "max_soft_clip_drive_db": float((safe.get("max_soft_clip_drive_db") if safe.get("max_soft_clip_drive_db") is not None else safe.get("max_clip_drive_db") if safe.get("max_clip_drive_db") is not None else _v6341_range_mid(clip.get("soft_clipper_drive_db_range"), 0.8))),
            "limiter_strength": dyn.get("limiter_strength"),
            "transient_preservation_priority": dyn.get("transient_preservation_priority"),
            "already_limited_behavior": clip.get("already_limited_behavior"),
            "intersample_peak_risk": clip.get("intersample_peak_risk"),
        })

    def wavg(name: str, default: float) -> float:
        try:
            return sum(float(v["w"]) * float(v.get(name, default)) for v in vals)
        except Exception:
            return float(default)

    max_gr = max(1.0, min(6.5, wavg("max_gr", 3.5)))
    target_margin = _env_float("BUSY_BAMIX_V6341_LIMITER_BUDGET_TARGET_MARGIN_LU", 0.85, minimum=0.0, maximum=2.5)
    min_margin = _env_float("BUSY_BAMIX_V6341_LIMITER_BUDGET_MIN_MARGIN_LU", 1.75, minimum=0.0, maximum=3.5)
    budget_max = max_gr
    budget_target = max(0.6, budget_max - target_margin)
    budget_min = max(0.2, budget_max - min_margin)
    if budget_min > budget_target:
        budget_min = max(0.2, budget_target - 0.4)

    # Delivery loudness: env / existing commercial target remains authoritative,
    # while private DB supplies the valid reference range and dynamics budget.
    if mode == "streaming_safe":
        delivery_default = _v6341_range_mid({"min": wavg("delivery_lufs_min", -14.5), "max": wavg("delivery_lufs_max", -13.5)}, -14.0)
    else:
        delivery_default = _env_float("BUSY_BAMIX_V632_COMMERCIAL_TARGET_LUFS", _env_float("BUSY_BAMIX_V6319_COMMERCIAL_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0), minimum=-12.5, maximum=-8.0)
    delivery_target = _env_float("BUSY_BAMIX_V6340_DELIVERY_TARGET_LUFS", float(delivery_default), minimum=-16.0, maximum=-6.0)

    final_tp = wavg("final_tp_ceiling", -0.8)
    tp_offset = _env_float("BUSY_BAMIX_V6341_PREMASTER_TP_TARGET_OFFSET_DB", 1.85 if mode != "streaming_safe" else 2.0, minimum=0.8, maximum=3.2)
    tp_max_offset = _env_float("BUSY_BAMIX_V6341_PREMASTER_TP_MAX_OFFSET_DB", 1.25 if mode != "streaming_safe" else 1.45, minimum=0.6, maximum=2.6)
    tp_target = max(-5.5, min(-1.6, final_tp - tp_offset))
    tp_max = max(-4.8, min(-1.2, final_tp - tp_max_offset))
    if tp_max < tp_target:
        tp_max = min(-1.2, tp_target + 0.55)
    tp_min = max(-7.0, min(-3.4, tp_target - 2.0))

    crest_offset = _env_float("BUSY_BAMIX_V6341_PREMASTER_CREST_OFFSET_DB", 1.0, minimum=0.0, maximum=3.5)
    crest_min = wavg("crest_min", 7.0) + crest_offset
    crest_target = wavg("crest_mid", 8.8) + crest_offset
    crest_max = wavg("crest_max", 11.0) + max(crest_offset, 1.4)

    return _jsonable({
        "schema_version": _SCHEMA + ".reference_db_contract_v6341",
        "active": True,
        "source": "private_genre_mastering_reference_db_v6341",
        "mode": mode,
        "reference_profile_ids": [{k: v for k, v in row.items() if k in {"profile_id", "role", "source", "resolve_source", "w"}} for row in vals],
        "primary_reference_profile_id": vals[0].get("profile_id") if vals else None,
        "delivery_reference_lufs_range": [round(wavg("delivery_lufs_min", -10.8), 3), round(wavg("delivery_lufs_max", -8.2), 3)],
        "delivery_target_lufs": round(float(delivery_target), 3),
        "final_tp_ceiling_dbtp_reference": round(float(final_tp), 3),
        "safety_constraints_max_limiter_gr_db": round(float(max_gr), 3),
        "final_limiter_gain_budget_lu": [round(float(budget_min), 3), round(float(budget_target), 3), round(float(budget_max), 3)],
        "premaster_lufs_min": round(float(delivery_target - budget_max), 3),
        "premaster_lufs_target": round(float(delivery_target - budget_target), 3),
        "premaster_lufs_max": round(float(delivery_target - max(0.1, budget_min)), 3),
        "premaster_tp_target_dbtp": round(float(tp_target), 3),
        "premaster_tp_min_dbtp": round(float(tp_min), 3),
        "premaster_tp_max_dbtp": round(float(tp_max), 3),
        "premaster_crest_min_db": round(float(crest_min), 3),
        "premaster_crest_target_db": round(float(crest_target), 3),
        "premaster_crest_max_db": round(float(crest_max), 3),
        "premaster_lra_min_lu": round(float(wavg("lra_min", 3.0)), 3),
        "premaster_lra_target_lu": round(float(wavg("lra_mid", 4.8)), 3),
        "premaster_lra_max_lu": round(float(wavg("lra_max", 7.0)), 3),
        "clipping_limiter_profile": {
            "clipper_before_limiter": bool(any(v.get("clipper_before_limiter") for v in vals)),
            "oversampling_min": max(int(v.get("oversampling_min") or 4) for v in vals) if vals else 4,
            "max_soft_clip_drive_db": round(max(float(v.get("max_soft_clip_drive_db") or 0.8) for v in vals), 3) if vals else 0.8,
            "already_limited_behavior": vals[0].get("already_limited_behavior") if vals else None,
            "intersample_peak_risk": vals[0].get("intersample_peak_risk") if vals else None,
        },
        "limiter_strength": vals[0].get("limiter_strength") if vals else None,
        "transient_preservation_priority": vals[0].get("transient_preservation_priority") if vals else None,
        "policy": "reference_v7/private_v9 selected profile drives limiter budget and premaster handoff; recipe bucket is fallback only",
    })


def _v6340_premaster_handoff_contract(
    recipe: str | None,
    stem_metrics: list[dict[str, Any]] | None = None,
    *,
    original_decision: dict[str, Any] | None = None,
    reference_db: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v63.4.1 reference-DB-driven premaster handoff contract.

    The v63.4.0 contract is retained as a fallback, but v63.4.1 first consumes
    the existing pre-BAMix genre/profile decision and private reference DB
    (genre_mastering_reference_v9/v7/v6).  This avoids inventing a second genre
    table inside BAMix and keeps limiter budgets aligned with the prior stage.
    """
    profile = str(os.environ.get("BUSY_BAMIX_V6340_PROFILE", os.environ.get("BUSY_BAMIX_PROFILE", "competitive_loud"))).strip().lower()
    ref_contract = _v6341_profile_contract_from_reference_db(reference_db, original_decision=original_decision, recipe=recipe, profile=profile)
    if isinstance(ref_contract, dict) and ref_contract.get("active"):
        contract = dict(ref_contract)
    else:
        bucket = _v6340_contract_genre_bucket(recipe)
        if profile in {"stream", "streaming", "streaming_normalized", "streaming-normalized"}:
            profile_norm = "streaming_normalized"
        else:
            profile_norm = "competitive_loud"
        if profile_norm == "streaming_normalized":
            delivery_default = -14.0
            budget_table = {
                "edm_club": (1.0, 2.0, 3.0),
                "kpop_vocal_pop": (1.0, 2.0, 3.0),
                "rock_band": (0.8, 1.6, 2.5),
                "ballad_acoustic": (0.3, 1.0, 2.0),
                "wide_cinematic": (0.3, 1.0, 2.0),
                "ai_artifact_safe": (0.4, 1.2, 2.3),
                "balanced_pop": (1.0, 2.0, 3.0),
            }
            tp_target, tp_max, tp_min = -3.0, -2.2, -5.0
            crest_table = {
                "edm_club": (9.0, 10.5, 12.0),
                "kpop_vocal_pop": (10.0, 11.0, 13.0),
                "rock_band": (11.0, 12.0, 14.0),
                "ballad_acoustic": (13.0, 14.5, 17.0),
                "wide_cinematic": (12.0, 14.0, 17.0),
                "ai_artifact_safe": (12.0, 14.0, 17.0),
                "balanced_pop": (10.0, 11.5, 13.5),
            }
        else:
            delivery_default = _env_float("BUSY_BAMIX_V632_COMMERCIAL_TARGET_LUFS", _env_float("BUSY_BAMIX_V6319_COMMERCIAL_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0), minimum=-12.5, maximum=-8.0)
            budget_table = {
                "edm_club": (2.5, 3.8, 5.0),
                "kpop_vocal_pop": (2.0, 3.3, 4.3),
                "rock_band": (2.0, 3.1, 4.2),
                "ballad_acoustic": (1.0, 2.0, 3.2),
                "wide_cinematic": (1.0, 2.0, 3.2),
                "ai_artifact_safe": (1.0, 2.1, 3.5),
                "balanced_pop": (2.0, 3.4, 4.4),
            }
            tp_target, tp_max, tp_min = -2.6, -2.0, -4.6
            crest_table = {
                "edm_club": (7.0, 8.2, 10.0),
                "kpop_vocal_pop": (8.0, 9.4, 11.5),
                "rock_band": (9.0, 10.2, 12.5),
                "ballad_acoustic": (11.0, 12.5, 15.0),
                "wide_cinematic": (10.5, 12.5, 15.5),
                "ai_artifact_safe": (11.0, 13.0, 16.0),
                "balanced_pop": (8.5, 9.8, 12.2),
            }
        delivery_target = _env_float("BUSY_BAMIX_V6340_DELIVERY_TARGET_LUFS", float(delivery_default), minimum=-16.0, maximum=-7.5)
        budget_min, budget_target, budget_max = budget_table.get(bucket, budget_table["balanced_pop"])
        crest_min, crest_target, crest_max = crest_table.get(bucket, crest_table["balanced_pop"])
        contract = {
            "schema_version": _SCHEMA + ".fallback_bucket_contract_v6340_compat",
            "active": True,
            "profile": profile_norm,
            "mode": "streaming_safe" if profile_norm == "streaming_normalized" else "commercial_loud",
            "genre_bucket": bucket,
            "source": "fallback_bucket_contract_v6340_compat_no_private_reference_db",
            "delivery_target_lufs": round(float(delivery_target), 3),
            "premaster_lufs_min": round(float(delivery_target - budget_max), 3),
            "premaster_lufs_target": round(float(delivery_target - budget_target), 3),
            "premaster_lufs_max": round(float(delivery_target - max(0.1, budget_min)), 3),
            "premaster_tp_target_dbtp": round(float(tp_target), 3),
            "premaster_tp_min_dbtp": round(float(tp_min), 3),
            "premaster_tp_max_dbtp": round(float(tp_max), 3),
            "premaster_crest_min_db": round(float(crest_min), 3),
            "premaster_crest_target_db": round(float(crest_target), 3),
            "premaster_crest_max_db": round(float(crest_max), 3),
            "final_limiter_gain_budget_lu": [round(float(budget_min), 3), round(float(budget_target), 3), round(float(budget_max), 3)],
        }

    delivery_target = _env_float("BUSY_BAMIX_V6340_DELIVERY_TARGET_LUFS", float(contract.get("delivery_target_lufs") or -9.25), minimum=-16.0, maximum=-6.0)
    budget = list(contract.get("final_limiter_gain_budget_lu") or [2.0, 3.4, 4.4])
    while len(budget) < 3:
        budget.append(budget[-1] if budget else 4.4)
    budget_min = _env_float("BUSY_BAMIX_V6340_LIMITER_BUDGET_MIN_LU", float(budget[0]), minimum=0.0, maximum=6.5)
    budget_target = _env_float("BUSY_BAMIX_V6340_LIMITER_BUDGET_TARGET_LU", float(budget[1]), minimum=0.0, maximum=7.0)
    budget_max = _env_float("BUSY_BAMIX_V6340_LIMITER_BUDGET_MAX_LU", float(budget[2]), minimum=1.0, maximum=8.0)
    contract["delivery_target_lufs"] = round(float(delivery_target), 3)
    contract["final_limiter_gain_budget_lu"] = [round(float(budget_min), 3), round(float(budget_target), 3), round(float(budget_max), 3)]
    contract["premaster_lufs_min"] = round(float(delivery_target - budget_max), 3)
    contract["premaster_lufs_target"] = round(float(delivery_target - budget_target), 3)
    contract["premaster_lufs_max"] = round(float(delivery_target - max(0.1, budget_min)), 3)

    max_art = 0.0
    mean_art = 0.0
    if stem_metrics:
        try:
            vals = [float(x.get("artifact_risk") or 0.0) for x in stem_metrics if isinstance(x, dict)]
            max_art = max(vals) if vals else 0.0
            mean_art = float(np.mean(vals)) if vals else 0.0
        except Exception:
            max_art = mean_art = 0.0
    artifact_soft = max_art >= _env_float("BUSY_BAMIX_V6340_ARTIFACT_SOFTEN_AT", 0.78, minimum=0.3, maximum=0.98) or mean_art >= _env_float("BUSY_BAMIX_V6340_MEAN_ARTIFACT_SOFTEN_AT", 0.60, minimum=0.3, maximum=0.98)
    if artifact_soft:
        contract["premaster_lufs_min"] = round(float(min(float(contract.get("premaster_lufs_min") or delivery_target - 5.0), delivery_target - 5.0)), 3)
        contract["premaster_lufs_target"] = round(float(min(float(contract.get("premaster_lufs_target") or delivery_target - 4.0), delivery_target - 4.0)), 3)
        contract["premaster_lufs_max"] = round(float(min(float(contract.get("premaster_lufs_max") or delivery_target - 2.5), delivery_target - 2.5)), 3)
        contract["premaster_tp_target_dbtp"] = round(float(min(float(contract.get("premaster_tp_target_dbtp") or -3.0), -3.0)), 3)
        contract["premaster_tp_max_dbtp"] = round(float(min(float(contract.get("premaster_tp_max_dbtp") or -2.4), -2.4)), 3)
        contract["premaster_crest_max_db"] = round(float(max(float(contract.get("premaster_crest_max_db") or 15.5), 15.5)), 3)
    contract["schema_version"] = _SCHEMA + ".premaster_handoff_contract_v6341"
    contract["active"] = _env_on("BUSY_BAMIX_V6340_HANDOFF_CONTRACT", "1")
    contract["artifact_softened"] = bool(artifact_soft)
    contract["mean_artifact_risk"] = round(float(mean_art), 4)
    contract["max_artifact_risk"] = round(float(max_art), 4)
    contract["final_limiter_budget_policy"] = "private reference DB max_limiter_gr_db caps final limiter workload; excess routes to upstream density/peak-relief/body before final limiting"
    return _jsonable(contract)

def _recipe_premaster_targets(
    recipe: str | None = None,
    stem_metrics: list[dict[str, Any]] | None = None,
    *,
    original_decision: dict[str, Any] | None = None,
    reference_db: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v63.0.3: BAMix premaster target matrix.

    The v63.0.2 estimator could make a technically safe but under-driven premaster
    (-23 LUFS / -8 dBTP).  This target matrix is intentionally more like a
    professional premaster handoff: not mastered, but dense enough that the
    downstream mastering stage does not need a destructive +10~13 LUFS rescue push.
    """
    recipe = str(recipe or "clean_balanced_professional").strip().lower()
    aliases = {
        "clean_balanced": "clean_balanced_professional",
        "ai_artifact_safe": "ai_artifact_conservative",
    }
    recipe = aliases.get(recipe, recipe)
    table: dict[str, dict[str, float]] = {
        "clean_balanced_professional": {"lufs_min": -16.2, "lufs_target": -14.6, "lufs_max": -13.0, "tp_target": -2.35, "tp_min": -4.0, "tp_max": -1.6, "crest_min": 10.2, "crest_target": 12.2, "crest_max": 15.8, "max_trim": 7.5},
        "vocal_forward": {"lufs_min": -15.8, "lufs_target": -14.2, "lufs_max": -12.8, "tp_target": -2.3, "tp_min": -3.9, "tp_max": -1.5, "crest_min": 9.8, "crest_target": 11.5, "crest_max": 14.8, "max_trim": 7.5},
        "vocal_forward_bass_controlled": {"lufs_min": -15.8, "lufs_target": -14.2, "lufs_max": -12.8, "tp_target": -2.35, "tp_min": -4.0, "tp_max": -1.6, "crest_min": 10.0, "crest_target": 11.8, "crest_max": 15.0, "max_trim": 7.5},
        "bass_controlled": {"lufs_min": -15.8, "lufs_target": -14.2, "lufs_max": -12.8, "tp_target": -2.35, "tp_min": -4.0, "tp_max": -1.6, "crest_min": 10.0, "crest_target": 11.8, "crest_max": 15.0, "max_trim": 7.5},
        # Keep punch dynamic but not timid.  We do not force -14 LUFS; we allow it
        # when the peak/density state supports it without crest collapse.
        "punch_preserved": {"lufs_min": -15.2, "lufs_target": -13.8, "lufs_max": -12.6, "tp_target": -2.25, "tp_min": -3.8, "tp_max": -1.5, "crest_min": 10.8, "crest_target": 12.8, "crest_max": 16.4, "max_trim": 7.5},
        "dense_pop": {"lufs_min": -14.8, "lufs_target": -13.2, "lufs_max": -12.0, "tp_target": -2.15, "tp_min": -3.7, "tp_max": -1.4, "crest_min": 9.2, "crest_target": 10.8, "crest_max": 14.4, "max_trim": 7.0},
        "club_low_end_controlled": {"lufs_min": -14.8, "lufs_target": -13.2, "lufs_max": -12.0, "tp_target": -2.15, "tp_min": -3.7, "tp_max": -1.4, "crest_min": 9.4, "crest_target": 11.0, "crest_max": 14.8, "max_trim": 7.0},
        "wide_bed_safe": {"lufs_min": -18.0, "lufs_target": -16.5, "lufs_max": -15.0, "tp_target": -3.2, "tp_min": -4.8, "tp_max": -2.2, "crest_min": 11.5, "crest_target": 13.5, "crest_max": 15.5, "max_trim": 7.5},
        "acoustic_natural": {"lufs_min": -19.0, "lufs_target": -17.5, "lufs_max": -15.5, "tp_target": -3.5, "tp_min": -5.0, "tp_max": -2.5, "crest_min": 12.5, "crest_target": 14.0, "crest_max": 17.0, "max_trim": 7.5},
        "cinematic_wide": {"lufs_min": -19.0, "lufs_target": -17.0, "lufs_max": -15.0, "tp_target": -3.5, "tp_min": -5.0, "tp_max": -2.4, "crest_min": 12.0, "crest_target": 14.0, "crest_max": 17.0, "max_trim": 7.5},
        "ai_artifact_conservative": {"lufs_min": -19.0, "lufs_target": -17.0, "lufs_max": -15.5, "tp_target": -4.0, "tp_min": -5.5, "tp_max": -2.5, "crest_min": 12.0, "crest_target": 14.0, "crest_max": 16.5, "max_trim": 7.5},
    }
    base = dict(table.get(recipe, table["clean_balanced_professional"]))
    mean_art = 0.0
    if stem_metrics:
        try:
            mean_art = float(np.mean([float(x.get("artifact_risk") or 0.0) for x in stem_metrics]))
        except Exception:
            mean_art = 0.0
    decision_policy = ""
    if isinstance(original_decision, dict):
        aip = original_decision.get("auto_intensity_policy") if isinstance(original_decision.get("auto_intensity_policy"), dict) else {}
        decision_policy = " ".join(str(x or "") for x in [
            original_decision.get("mode"),
            original_decision.get("mastering_intensity"),
            original_decision.get("selected_mode"),
            original_decision.get("requested_mode"),
            original_decision.get("delivery_mode"),
            aip.get("final_mode") if isinstance(aip, dict) else None,
            aip.get("requested_mode") if isinstance(aip, dict) else None,
            aip.get("selected_mode") if isinstance(aip, dict) else None,
        ]).strip().lower()
    if not decision_policy:
        decision_policy = " ".join(str(os.environ.get(k) or "") for k in (
            "BUSY_MASTERING_INTENSITY",
            "BUSY_AUTO_MASTERING_INTENSITY_SELECTED",
            "BUSY_AUTO_MASTERING_INTENSITY_REQUESTED",
            "BUSY_BAMIX_DELIVERY_POLICY",
        )).strip().lower()
    commercial_delivery = any(tok in decision_policy for tok in ("commercial", "maximum", "loudest", "hot_master", "auto_maximum"))
    maximum_delivery = any(tok in decision_policy for tok in ("maximum", "loudest", "auto_maximum"))
    # Artifact-heavy cases should not be driven as hot, but still should not fall
    # into the -23 LUFS under-driven failure mode.
    if mean_art >= _env_float("BUSY_AUTOMIX_ARTIFACT_TARGET_SOFTEN_AT", 0.68, minimum=0.3, maximum=0.95):
        base["lufs_target"] = min(float(base["lufs_target"]), -17.0)
        base["lufs_min"] = min(float(base["lufs_min"]), -19.0)
        base["tp_target"] = min(float(base["tp_target"]), -3.5)
        base["crest_max"] = max(float(base["crest_max"]), 16.0)
        base["artifact_softened"] = True
    else:
        base["artifact_softened"] = False
    # v63.2: make BAMix an actual commercial premaster builder, not a quiet
    # organization pass.  Healthy stems should arrive at mastering around
    # -15~-12 LUFS so the final limiter/commercial finish completes 2~4 LU,
    # instead of rescuing a -17 LUFS premaster by almost 7 LU.
    base["v632_commercial_premaster_density_enabled"] = _env_on("BUSY_BAMIX_V632_COMMERCIAL_PREMASTER_DENSITY", "1")
    if base["v632_commercial_premaster_density_enabled"] and not bool(base.get("artifact_softened")):
        _r = recipe
        if _r not in {"ai_artifact_conservative", "acoustic_natural", "cinematic_wide"}:
            # For negative LUFS, a hotter minimum is numerically larger (e.g. -14.0 > -14.8).
            # Use max() so an env-tuned cap can tighten the premaster window instead of
            # accidentally making it more permissive.
            base["lufs_min"] = max(float(base["lufs_min"]), _env_float("BUSY_BAMIX_V632_PREMASTER_LUFS_MIN_CAP", -14.8, minimum=-18.5, maximum=-12.0)) if _r in {"dense_pop", "club_low_end_controlled"} else float(base["lufs_min"])
            # Keep the table values as the primary target, but expose an explicit
            # mode flag and bounded target/loudness floor for QC/correction.
            base["commercial_premaster_builder"] = True
            base["expected_mastering_gain_window_lu"] = [2.0, 4.6]
        else:
            base["commercial_premaster_builder"] = False
    # v63.4.0: research-based handoff contract.  The old matrix was still
    # centered around -14~-15 LUFS for many recipes, which forced the final
    # limiter to perform +5~7 LU rescue gain.  The contract derives the
    # premaster window from the chosen final target minus a bounded final-limiter
    # workload budget.
    contract = _v6340_premaster_handoff_contract(recipe, stem_metrics or [], original_decision=original_decision, reference_db=reference_db)
    if bool(contract.get("active")) and not bool(base.get("artifact_softened")):
        base["lufs_min"] = float(contract.get("premaster_lufs_min"))
        base["lufs_target"] = float(contract.get("premaster_lufs_target"))
        base["lufs_max"] = float(contract.get("premaster_lufs_max"))
        base["tp_target"] = float(contract.get("premaster_tp_target_dbtp"))
        base["tp_min"] = float(contract.get("premaster_tp_min_dbtp"))
        base["tp_max"] = float(contract.get("premaster_tp_max_dbtp"))
        base["crest_min"] = float(contract.get("premaster_crest_min_db"))
        base["crest_target"] = float(contract.get("premaster_crest_target_db"))
        base["crest_max"] = float(contract.get("premaster_crest_max_db"))
        base["commercial_premaster_builder"] = True
        base["expected_mastering_gain_window_lu"] = list(contract.get("final_limiter_gain_budget_lu") or [2.0, 3.4, 4.4])[:3]
        if len(base["expected_mastering_gain_window_lu"]) >= 3:
            # existing QC expects [min,max]; keep the readable full budget in the
            # contract and expose the operational [min,max] window here.
            base["expected_mastering_gain_window_lu"] = [base["expected_mastering_gain_window_lu"][0], base["expected_mastering_gain_window_lu"][2]]
        base["v6340_contract_applied"] = True
        base["v6340_premaster_handoff_contract"] = contract
        base["v6341_reference_db_handoff_contract"] = contract
    else:
        base["v6340_contract_applied"] = False
        base["v6340_premaster_handoff_contract"] = contract
        base["v6341_reference_db_handoff_contract"] = contract
    if commercial_delivery and not bool(base.get("artifact_softened")) and _env_on("BUSY_BAMIX_V643_UNSHACKLE_COMMERCIAL_PREMASTER", "1"):
        # v64.3: healthy stem premasters should be dense enough that mastering
        # completes a commercial -0.1 dBTP WAV instead of rescuing a quiet mix.
        min_floor = _env_float(
            "BUSY_BAMIX_V643_COMMERCIAL_PREMASTER_LUFS_MIN_FLOOR",
            -12.9 if maximum_delivery else -13.4,
            minimum=-16.0,
            maximum=-10.0,
        )
        target_floor = _env_float(
            "BUSY_BAMIX_V643_COMMERCIAL_PREMASTER_LUFS_TARGET_FLOOR",
            -11.7 if maximum_delivery else -12.3,
            minimum=-15.0,
            maximum=-8.8,
        )
        max_floor = _env_float(
            "BUSY_BAMIX_V643_COMMERCIAL_PREMASTER_LUFS_MAX_FLOOR",
            -10.4 if maximum_delivery else -10.9,
            minimum=-14.0,
            maximum=-8.0,
        )
        base["lufs_min"] = max(float(base["lufs_min"]), float(min_floor))
        base["lufs_target"] = max(float(base["lufs_target"]), float(target_floor))
        base["lufs_max"] = max(float(base["lufs_max"]), float(max_floor))
        base["tp_target"] = max(float(base["tp_target"]), _env_float("BUSY_BAMIX_V643_COMMERCIAL_PREMASTER_TP_TARGET_FLOOR_DBTP", -1.15 if maximum_delivery else -1.35, minimum=-2.5, maximum=-0.55))
        base["tp_max"] = max(float(base["tp_max"]), _env_float("BUSY_BAMIX_V643_COMMERCIAL_PREMASTER_TP_MAX_FLOOR_DBTP", -0.70 if maximum_delivery else -0.85, minimum=-2.0, maximum=-0.35))
        base["commercial_premaster_builder"] = True
        base["v643_commercial_premaster_unshackled"] = True
        base["v643_commercial_premaster_policy"] = "commercial_or_maximum_mode_uses_hotter_premaster_density_window_before_final_minus_0_1dbtp_mastering"
    else:
        base["v643_commercial_premaster_unshackled"] = False
    # Env overrides are bounded so deployment can tune without making BAMix a limiter.
    hot_contract = bool(base.get("v6340_contract_applied"))
    hot_delivery = bool(commercial_delivery and not bool(base.get("artifact_softened")))
    lufs_max_bound = -8.0 if maximum_delivery else -8.6 if hot_delivery else -8.8 if hot_contract else -10.0
    lufs_target_max_bound = -8.8 if maximum_delivery else -9.3 if hot_delivery else -9.5 if hot_contract else -12.0
    lufs_min_max_bound = -9.8 if maximum_delivery else -10.2 if hot_delivery else -10.5 if hot_contract else -12.0
    base["lufs_min"] = _env_float("BUSY_AUTOMIX_PREMASTER_LUFS_MIN", float(base["lufs_min"]), minimum=-24.0, maximum=lufs_min_max_bound)
    base["lufs_target"] = _env_float("BUSY_AUTOMIX_PREMASTER_LUFS_TARGET", float(base["lufs_target"]), minimum=-22.0, maximum=lufs_target_max_bound)
    base["lufs_max"] = _env_float("BUSY_AUTOMIX_PREMASTER_LUFS_MAX", float(base["lufs_max"]), minimum=-20.0, maximum=lufs_max_bound)
    base["tp_target"] = _env_float("BUSY_AUTOMIX_PREMASTER_TARGET_TP_DBTP", float(base["tp_target"]), minimum=-7.0, maximum=-0.55 if hot_delivery else -1.0)
    base["tp_min"] = _env_float("BUSY_AUTOMIX_PREMASTER_TP_MIN_DBTP", float(base["tp_min"]), minimum=-10.0, maximum=-1.5)
    base["tp_max"] = _env_float("BUSY_AUTOMIX_PREMASTER_TP_MAX_DBTP", float(base["tp_max"]), minimum=-6.0, maximum=-0.35 if hot_delivery else -0.5)
    base["crest_min"] = _env_float("BUSY_AUTOMIX_PREMASTER_CREST_MIN_DB", float(base["crest_min"]), minimum=6.0, maximum=16.0)
    base["crest_target"] = _env_float("BUSY_AUTOMIX_PREMASTER_CREST_TARGET_DB", float(base["crest_target"]), minimum=7.0, maximum=18.0)
    base["crest_max"] = _env_float("BUSY_AUTOMIX_PREMASTER_CREST_MAX_DB", float(base["crest_max"]), minimum=8.0, maximum=22.0)
    base["max_trim"] = abs(_env_float("BUSY_AUTOMIX_PRERENDER_MAX_TRIM_DB", float(base["max_trim"]), minimum=0.0, maximum=14.0))
    return _jsonable({"schema_version": _SCHEMA + ".premaster_target_matrix", "recipe": recipe, **base})

def _role_gain_db(role: str, recipe: str, artifact: float, confidence: float) -> float:
    role = str(role or "unknown")
    recipe = str(recipe or "clean_balanced_professional")
    # These are deliberate, audible mix moves, not timid Stage-M control weights.
    base = {
        "vocal": 0.9,
        "kick": 0.2,
        "snare": 0.1,
        "drums": 0.0,
        "hats": -0.2,
        "bass": -0.1,
        "music_bed": -0.35,
        "fx_ambience": -0.65,
    }.get(role, -0.35)
    if recipe == "vocal_forward":
        if role == "vocal": base += 1.1
        if role in {"music_bed", "fx_ambience"}: base -= 0.8
        if role in {"hats"}: base -= 0.3
    elif recipe == "vocal_forward_bass_controlled":
        if role == "vocal": base += 1.0
        if role == "bass": base -= 0.45
        if role == "kick": base += 0.15
        if role in {"music_bed", "fx_ambience"}: base -= 0.75
    elif recipe == "bass_controlled":
        if role == "bass": base -= 0.75
        if role == "kick": base += 0.25
        if role == "vocal": base += 0.35
    elif recipe == "punch_preserved":
        if role in {"drums", "kick", "snare"}: base += 0.55
        if role in {"music_bed", "fx_ambience"}: base -= 0.35
    elif recipe == "wide_bed_safe":
        if role in {"music_bed", "fx_ambience"}: base += 0.10
        if role == "vocal": base += 0.55
    elif recipe == "ai_artifact_conservative":
        # Still uses stems, but avoids foregrounding damaged HF/side residue.
        if role == "vocal": base += 0.35
        if role in {"hats", "fx_ambience"}: base -= 1.1
        if role == "music_bed": base -= 0.35
    if artifact > 0.70 and role in {"hats", "fx_ambience", "music_bed"}:
        base -= min(1.0, (artifact - 0.70) * 1.6)
    if confidence < 0.45:
        base *= 0.72
    return float(np.clip(base, -3.0, 3.0))


def _width_scalar(role: str, recipe: str, artifact: float) -> float:
    if role in {"kick", "bass", "vocal", "snare"}:
        return 0.0 if role in {"kick", "bass"} else 0.22
    if recipe == "wide_bed_safe" and artifact < 0.65:
        return 1.06
    if recipe == "ai_artifact_conservative" or artifact > 0.75:
        return 0.72
    if role == "fx_ambience":
        return 0.95
    return 0.88 if role == "music_bed" else 0.82


def _apply_width(x: np.ndarray, width: float) -> np.ndarray:
    y = _ensure_stereo(x)
    if width <= 0.02:
        m = np.mean(y, axis=1, keepdims=True)
        return np.repeat(m, 2, axis=1).astype(np.float32, copy=False)
    mid = (y[:, 0] + y[:, 1]) * 0.5
    side = (y[:, 0] - y[:, 1]) * 0.5 * float(width)
    return np.stack([mid + side, mid - side], axis=1).astype(np.float32, copy=False)


def _v6463_select_adaptive_block_size(*, max_frames: int, target_sr: int, stem_count: int, smr_stem_count: int) -> tuple[int, dict[str, Any]]:
    fixed = _env_int("BUSY_AUTOMIX_RENDER_BLOCK_SIZE", 16384, minimum=1024, maximum=262144)
    auto = _env_on("BUSY_AUTOMIX_RENDER_BLOCK_SIZE_AUTO", "1")
    hard_lock = _env_on("BUSY_AUTOMIX_RENDER_BLOCK_SIZE_HARD_LOCK", "0")
    duration = float(max(0, int(max_frames))) / max(float(target_sr), 1.0)
    mem_mb = _env_int("BUSY_AUTOMIX_MEMORY_LIMIT_MB", _env_int("MEMORY_LIMIT_MB", 8192, minimum=512, maximum=65536), minimum=512, maximum=65536)
    cpu = _env_float("BUSY_AUTOMIX_CPU_LIMIT", float(os.environ.get("CPU_LIMIT", "2") or 2), minimum=0.25, maximum=64.0)
    reason = "fixed_env"
    ignored_stale = False
    if hard_lock or not auto:
        block = fixed
    else:
        reason = "adaptive_cloudrun_memory_cpu_duration_stem_count"
        ignored_stale = bool(str(os.environ.get("BUSY_AUTOMIX_RENDER_BLOCK_SIZE", "")).strip() in {"16384", "16384.0"})
        if mem_mb >= 8192 and cpu >= 2.0 and stem_count <= 8 and smr_stem_count <= 2 and duration <= 260.0:
            block = 131072
        elif mem_mb >= 6144 and stem_count <= 10 and smr_stem_count <= 4 and duration <= 360.0:
            block = 65536
        elif mem_mb >= 4096 and stem_count <= 12 and duration <= 420.0:
            block = 65536
        else:
            block = 32768
        if duration >= 540.0 or stem_count >= 16 or smr_stem_count >= 6:
            block = min(block, 65536)
        if mem_mb <= 3072:
            block = min(block, 32768)
    block = int(max(1024, min(262144, block)))
    est_blocks = int(math.ceil(float(max_frames) / float(block))) if block > 0 and max_frames > 0 else 0
    old_blocks = int(math.ceil(float(max_frames) / 16384.0)) if max_frames > 0 else 0
    report = {
        "schema_version": _SCHEMA + ".adaptive_block_sizing_v6463",
        "active": bool(auto and not hard_lock),
        "mode": "hard_lock" if hard_lock else ("fixed_env" if not auto else "auto"),
        "selected_block_size": int(block),
        "selected_block_ms": round(float(block) * 1000.0 / max(float(target_sr), 1.0), 3),
        "estimated_render_blocks": int(est_blocks),
        "old_16384_estimated_blocks": int(old_blocks),
        "boundary_reduction_ratio": round(float(old_blocks) / float(max(est_blocks, 1)), 3) if old_blocks else 1.0,
        "duration_sec": round(float(duration), 3),
        "stem_count": int(stem_count),
        "smr_stem_count": int(smr_stem_count),
        "memory_limit_mb": int(mem_mb),
        "cpu_limit": round(float(cpu), 3),
        "fixed_env_block_size": int(fixed),
        "ignored_stale_fixed_env": bool(ignored_stale),
        "reason": reason,
    }
    return block, report


def _v6463_seam_smooth_block(block: np.ndarray, prev_tail: np.ndarray | None, *, sr: int, state: dict[str, Any], label: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    x = _ensure_stereo(block).astype(np.float32, copy=False)
    edge_ms = _env_float("BUSY_BAMIX_V6463_SEAM_SMOOTH_MS", 2.0, minimum=0.0, maximum=12.0)
    n = int(round(float(sr) * edge_ms / 1000.0))
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema_version", _SCHEMA + f".{label}_seam_smoother_v6463")
    state["active"] = bool(edge_ms > 0.0)
    state["edge_ms"] = round(float(edge_ms), 3)
    state["processed_count"] = int(state.get("processed_count") or 0) + 1
    if edge_ms <= 0.0 or prev_tail is None or x.shape[0] < 2:
        tail_n = max(1, min(x.shape[0], max(1, n)))
        return x, x[-tail_n:].copy(), state
    m = min(int(n), int(x.shape[0]), int(prev_tail.shape[0]))
    if m > 1:
        fade = np.linspace(0.0, 1.0, m, dtype=np.float32)[:, None]
        bridge = prev_tail[-m:].astype(np.float32, copy=False) * (1.0 - fade) + x[:m] * fade
        before_jump = float(np.max(np.abs(x[0] - prev_tail[-1]))) if prev_tail.size else 0.0
        y = x.copy()
        y[:m] = bridge
        x = y.astype(np.float32, copy=False)
        state["applied"] = True
        state["applied_count"] = int(state.get("applied_count") or 0) + 1
        state["last_edge_samples"] = int(m)
        state["max_boundary_jump_before"] = round(max(float(state.get("max_boundary_jump_before") or 0.0), before_jump), 7)
    tail_n = max(1, min(x.shape[0], max(1, int(n))))
    return x, x[-tail_n:].copy(), state


def _v6464_scale_reported_intensity(state: dict[str, Any], raw: float, effective: float, reason: str) -> dict[str, Any]:
    out = dict(state) if isinstance(state, dict) else {}
    out["v6464_raw_requested_intensity"] = round(float(raw), 4)
    out["v6464_effective_intensity"] = round(float(effective), 4)
    out["v6464_ownership_scale"] = round(float(effective) / max(float(raw), 1e-9), 4) if raw > 0.0 else 1.0
    out["v6464_ownership_reason"] = reason
    return out


def _v6464_transient_hf_ownership_plan(*, drum_den_int: float, transient_int: float, side_tex_int: float, erb_int: float, residue_pressure: dict[str, Any]) -> dict[str, Any]:
    active = _env_on("BUSY_BAMIX_V6464_TRANSIENT_HF_OWNERSHIP_LOCK", "1")
    out = {
        "schema_version": _SCHEMA + ".transient_hf_ownership_lock_v6464",
        "active": bool(active),
        "raw": {
            "drum_parallel_density": round(float(drum_den_int), 4),
            "transient_ghost": round(float(transient_int), 4),
            "side_texture_control": round(float(side_tex_int), 4),
            "erb_ms_dynamic_resonance_suppressor": round(float(erb_int), 4),
        },
        "effective": {},
        "locks": [],
    }
    if not active:
        out["effective"] = dict(out["raw"])
        out["reason"] = "disabled_by_env"
        return out
    eff_drum = float(drum_den_int)
    eff_trans = float(transient_int)
    eff_side = float(side_tex_int)
    eff_erb = float(erb_int)
    if eff_drum > 0.0 and eff_trans > 0.0:
        eff_trans *= _env_float("BUSY_BAMIX_V6464_TRANSIENT_GHOST_ASSIST_SCALE", 0.34, minimum=0.05, maximum=1.0)
        eff_drum *= _env_float("BUSY_BAMIX_V6464_DRUM_DENSITY_SHARED_SCALE", 0.92, minimum=0.5, maximum=1.0)
        out["locks"].append("drum_parallel_density_owns_drum_body_transient_ghost_assist_only")
    residue_active = bool(residue_pressure.get("active")) if isinstance(residue_pressure, dict) else False
    if eff_side > 0.0 and eff_erb > 0.0:
        scale = _env_float("BUSY_BAMIX_V6464_ERB_AFTER_SIDE_TEXTURE_SCALE", 0.42, minimum=0.1, maximum=1.0)
        if residue_active:
            scale = max(scale, _env_float("BUSY_BAMIX_V6464_ERB_RESIDUE_MIN_SCALE", 0.48, minimum=0.1, maximum=1.0))
        eff_erb *= scale
        out["locks"].append("side_texture_control_owns_side_hf_hash_erb_resonance_assist_only")
    out["effective"] = {
        "drum_parallel_density": round(float(eff_drum), 4),
        "transient_ghost": round(float(eff_trans), 4),
        "side_texture_control": round(float(eff_side), 4),
        "erb_ms_dynamic_resonance_suppressor": round(float(eff_erb), 4),
    }
    out["transient_owner"] = "drum_parallel_density" if drum_den_int > 0.0 else ("transient_ghost" if transient_int > 0.0 else "none")
    out["side_hf_owner"] = "side_texture_control" if side_tex_int > 0.0 else ("erb_ms_dynamic_resonance_suppressor" if erb_int > 0.0 else "none")
    out["policy"] = "coordinate overlapping transient/HF modules so the same micro-transient band is not repeatedly emphasized inside one block render"
    return out


def _v6464_hf_ratio(x: np.ndarray, sr: int, *, cutoff_hz: float = 6500.0) -> float:
    y = _ensure_stereo(x)
    if y.shape[0] < 32:
        return 0.0
    mono = np.mean(y, axis=1).astype(np.float32, copy=False)
    try:
        spec = np.fft.rfft(mono)
        mag = np.abs(spec)
        freqs = np.fft.rfftfreq(mono.shape[0], d=1.0 / max(float(sr), 1.0))
        total = float(np.sum(mag * mag)) + 1e-12
        high = float(np.sum((mag[freqs >= float(cutoff_hz)] ** 2))) if mag.size == freqs.size else 0.0
        return float(np.clip(high / total, 0.0, 1.0))
    except Exception:
        return 0.0


def _v6464_assist_delta_consolidator(pre: np.ndarray, post: np.ndarray, *, sr: int, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema_version", _SCHEMA + ".assist_delta_consolidator_v6464")
    state["active"] = bool(_env_on("BUSY_BAMIX_V6464_ASSIST_DELTA_CONSOLIDATOR", "1"))
    state["processed_count"] = int(state.get("processed_count") or 0) + 1
    if not state["active"]:
        state["reason"] = "disabled_by_env"
        return post, state
    a = _ensure_stereo(pre).astype(np.float32, copy=False)
    b = _ensure_stereo(post).astype(np.float32, copy=False)
    if a.shape != b.shape:
        state["reason"] = "shape_mismatch"
        return post, state
    delta = (b - a).astype(np.float32, copy=False)
    delta_rms = _rms(delta)
    min_delta = _amp(_env_float("BUSY_BAMIX_V6464_ASSIST_MIN_DELTA_RMS_DB", -70.0, minimum=-120.0, maximum=-24.0))
    if delta_rms <= min_delta:
        state["reason"] = "assist_delta_below_floor"
        return post, state
    hf_ratio = _v6464_hf_ratio(delta, sr, cutoff_hz=_env_float("BUSY_BAMIX_V6464_ASSIST_HF_CUTOFF_HZ", 6500.0, minimum=2500.0, maximum=16000.0))
    threshold = _env_float("BUSY_BAMIX_V6464_ASSIST_HF_RATIO_TRIGGER", 0.34, minimum=0.02, maximum=0.95)
    state["last_hf_ratio"] = round(float(hf_ratio), 5)
    state["max_hf_ratio"] = round(max(float(state.get("max_hf_ratio") or 0.0), float(hf_ratio)), 5)
    if hf_ratio <= threshold:
        state["reason"] = "assist_delta_within_hf_budget"
        return post, state
    excess = float(np.clip((hf_ratio - threshold) / max(1.0 - threshold, 1e-6), 0.0, 1.0))
    min_scale = _env_float("BUSY_BAMIX_V6464_ASSIST_DELTA_MIN_SCALE", 0.72, minimum=0.25, maximum=1.0)
    scale = 1.0 - (1.0 - min_scale) * excess
    y = (a + delta * np.float32(scale)).astype(np.float32, copy=False)
    state["applied"] = True
    state["applied_count"] = int(state.get("applied_count") or 0) + 1
    state["scale_min"] = round(min(float(state.get("scale_min") or 1.0), float(scale)), 4)
    state["last_scale"] = round(float(scale), 4)
    state["reason"] = "assist_delta_hf_micro_transient_budget"
    return y, state


def _iter_blocks_for_stem(path: Path, *, target_frames: int, target_sr: int, block_size: int) -> Any:
    with sf.SoundFile(str(path), "r") as f:
        src_sr = int(f.samplerate)
        frames_written = 0
        while frames_written < target_frames:
            need = min(block_size, target_frames - frames_written)
            if src_sr == target_sr:
                data = f.read(need, dtype="float32", always_2d=True)
                if data.shape[0] < need:
                    out = np.zeros((need, 2), dtype=np.float32)
                    if data.size:
                        out[:data.shape[0]] = _ensure_stereo(data)
                    data = out
                else:
                    data = _ensure_stereo(data)
                frames_written += need
                yield data
            else:
                # Runtime-safe but conservative resampling path.  It resamples a source chunk
                # large enough to cover the requested output block. Boundary continuity is not
                # perfect in v1, but it avoids loading full stems.
                if resample_poly is None:
                    data = np.zeros((need, 2), dtype=np.float32)
                    frames_written += need
                    yield data
                    continue
                ratio = float(src_sr) / float(target_sr)
                src_need = int(math.ceil(need * ratio)) + 32
                src = f.read(src_need, dtype="float32", always_2d=True)
                if src.size:
                    import math as _math
                    g = _math.gcd(src_sr, target_sr)
                    data = resample_poly(_ensure_stereo(src), target_sr // g, src_sr // g, axis=0).astype(np.float32, copy=False)
                    if data.shape[0] < need:
                        out = np.zeros((need, 2), dtype=np.float32)
                        out[:data.shape[0]] = data
                        data = out
                    else:
                        data = data[:need]
                else:
                    data = np.zeros((need, 2), dtype=np.float32)
                frames_written += need
                yield data.astype(np.float32, copy=False)


def _bamix_smr_cache_key(path: Path, *, idx: int, role: str, target_frames: int, target_sr: int, block_size: int, gain_db: float = 0.0, width_scalar: float = 1.0) -> str:
    h = hashlib.sha1()
    try:
        st = path.stat()
        stamp = f"{st.st_size}:{getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))}"
    except Exception:
        stamp = "missing"
    for part in [
        "busy_bamix_smr_cache_v8_5_3_64_4_4_1",
        str(idx),
        str(path),
        str(stamp),
        str(role),
        str(int(target_frames)),
        str(int(target_sr)),
        str(int(block_size)),
        f"{float(gain_db):.6f}",
        f"{float(width_scalar):.6f}",
    ]:
        h.update(part.encode("utf-8", "ignore"))
        h.update(b"\0")
    return h.hexdigest()[:20]


def _prepare_bamix_smr_cached_stem(
    stem: dict[str, Any],
    metric: dict[str, Any],
    *,
    idx: int,
    role: str,
    cache_dir: Path | None,
    target_frames: int,
    target_sr: int,
    block_size: int,
    gain_db: float = 0.0,
    width_scalar: float = 1.0,
    original_decision: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Build or reuse a post-role-gain/width SMR stem cache for correction rerenders."""
    src = Path(str(stem.get("local_path") or ""))
    base_report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".source_morphology_repair_stem_cache_v6444_1",
        "enabled": bool(_env_on("BUSY_BAMIX_SMR_STEM_CACHE", "1")),
        "source_path": str(src),
        "filename": stem.get("filename"),
        "role": role,
        "stem_index": int(idx),
        "target_frames": int(target_frames),
        "target_sr": int(target_sr),
        "block_size": int(block_size),
        "gain_db": round(float(gain_db), 6),
        "width_scalar": round(float(width_scalar), 6),
        "cache_used": False,
        "cache_hit": False,
        "cache_built": False,
        "cache_domain": "post_role_gain_width_pre_global_gain",
        "policy": "cache stores the original inline SMR domain after role gain/width and before global correction gain; correction rerenders reuse it only when that domain matches",
    }
    if not base_report["enabled"]:
        base_report["reason"] = "BUSY_BAMIX_SMR_STEM_CACHE_disabled"
        return src, base_report
    if cache_dir is None:
        base_report["reason"] = "cache_dir_missing"
        return src, base_report
    if not src.exists():
        base_report["reason"] = "source_path_missing"
        return src, base_report
    est_bytes = int(max(0, int(target_frames)) * 2 * 4)
    max_mb = _env_float("BUSY_BAMIX_SMR_CACHE_MAX_STEM_MB", 256.0, minimum=16.0, maximum=2048.0)
    base_report["estimated_cache_mb"] = round(float(est_bytes) / (1024.0 * 1024.0), 3)
    base_report["max_cache_stem_mb"] = round(float(max_mb), 3)
    if est_bytes > int(float(max_mb) * 1024.0 * 1024.0):
        base_report["reason"] = "estimated_cache_size_exceeds_stem_limit"
        return src, base_report
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = _bamix_smr_cache_key(src, idx=idx, role=role, target_frames=target_frames, target_sr=target_sr, block_size=block_size, gain_db=gain_db, width_scalar=width_scalar)
        cached = cache_dir / f"smr_stem_{idx:02d}_{key}.wav"
        meta = cache_dir / f"smr_stem_{idx:02d}_{key}.json"
        base_report.update({"cache_key": key, "cache_path": str(cached), "meta_path": str(meta)})
        if cached.exists() and meta.exists():
            try:
                cached_meta = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                cached_meta = {}
            rep = {**base_report, **(cached_meta if isinstance(cached_meta, dict) else {})}
            rep.update({
                "cache_used": True,
                "cache_hit": True,
                "cache_built": False,
                "reused_block_count": int(rep.get("processed_block_count") or rep.get("block_count") or 0),
                "reason": "cached_smr_stem_reused",
            })
            return cached, rep

        tmp = cache_dir / f".smr_stem_{idx:02d}_{key}.tmp.wav"
        processed = 0
        active = 0
        rollback = 0
        examples: list[dict[str, Any]] = []
        actions_seen: dict[str, int] = {}
        smr_prev_tail: np.ndarray | None = None
        smr_seam_state: dict[str, Any] = {}
        with sf.SoundFile(str(tmp), "w", samplerate=target_sr, channels=2, subtype="FLOAT", format="WAV") as out:
            for b in _iter_blocks_for_stem(src, target_frames=target_frames, target_sr=target_sr, block_size=block_size):
                processed += 1
                dry = _ensure_stereo(b).astype(np.float32, copy=False)
                dry = _apply_width(dry, float(width_scalar))
                dry = (dry * np.float32(_amp(float(gain_db)))).astype(np.float32, copy=False)
                try:
                    repaired, smr_rep = apply_source_morphology_repair(
                        dry,
                        target_sr,
                        metric,
                        original_decision if isinstance(original_decision, dict) else {},
                        role=role,
                        stem_metric=metric,
                    )
                except Exception as exc:
                    smr_rep = {"active": False, "reason": "exception", "error": str(exc)[:140]}
                    repaired = dry
                if isinstance(smr_rep, dict) and smr_rep.get("active"):
                    active += 1
                    write_block = _ensure_stereo(repaired).astype(np.float32, copy=False)
                    for a in (smr_rep.get("actions") or []):
                        if isinstance(a, dict) and a.get("action"):
                            actions_seen[str(a.get("action"))] = int(actions_seen.get(str(a.get("action")), 0)) + 1
                else:
                    write_block = dry
                    if isinstance(smr_rep, dict) and smr_rep.get("rolled_back_to_dry"):
                        rollback += 1
                if len(examples) < 12 and isinstance(smr_rep, dict):
                    examples.append({
                        "filename": stem.get("filename"),
                        "role": role,
                        "active": bool(smr_rep.get("active")),
                        "needed": bool(smr_rep.get("needed")),
                        "reason": smr_rep.get("reason"),
                        "actions": [a.get("action") for a in (smr_rep.get("actions") or []) if isinstance(a, dict)][:4],
                    })
                write_block, smr_prev_tail, smr_seam_state = _v6463_seam_smooth_block(
                    write_block,
                    smr_prev_tail,
                    sr=target_sr,
                    state=smr_seam_state,
                    label="smr_stem_cache",
                )
                out.write(write_block)
        try:
            os.replace(str(tmp), str(cached))
        except Exception:
            shutil.move(str(tmp), str(cached))
        built_report = {
            **base_report,
            "cache_used": True,
            "cache_hit": False,
            "cache_built": True,
            "reason": "cached_smr_stem_built",
            "processed_block_count": int(processed),
            "active_block_count": int(active),
            "dry_rollback_block_count": int(rollback),
            "active": bool(active > 0),
            "actions_seen": actions_seen,
            "v6463_smr_stem_cache_seam_smoother": _jsonable(smr_seam_state),
            "examples": examples,
        }
        meta.write_text(json.dumps(_jsonable(built_report), ensure_ascii=False, indent=2), encoding="utf-8")
        return cached, built_report
    except Exception as exc:
        base_report.update({"reason": "cache_exception_inline_repair_fallback", "error": str(exc)[:220]})
        return src, base_report



def _read_aligned_proxy_segment(path: Path, *, start_sec: float, duration_sec: float, target_sr: int) -> np.ndarray:
    """v63.1.3: read a short aligned stem segment for the pre-render estimator.

    This is analysis-only and intentionally small.  It prevents the old worst-case
    stem-peak summation from predicting +12 dBFS when the actual aligned stem sum
    is far lower, which forced a -7.5 dB trim and then a correction rerender.
    """
    n_target = max(1, int(round(float(duration_sec) * int(target_sr))))
    try:
        with sf.SoundFile(str(path), "r") as f:
            src_sr = int(f.samplerate)
            src_frames = int(f.frames)
            src_start = int(max(0, min(src_frames, round(float(start_sec) * src_sr))))
            src_need = max(1, int(round(float(duration_sec) * src_sr)))
            f.seek(src_start)
            data = f.read(src_need, dtype="float32", always_2d=True)
            y = _ensure_stereo(data).astype(np.float32, copy=False)
            if src_sr != int(target_sr) and y.size and resample_poly is not None:
                import math as _math
                g = _math.gcd(src_sr, int(target_sr))
                y = resample_poly(y, int(target_sr) // g, src_sr // g, axis=0).astype(np.float32, copy=False)
            if y.shape[0] < n_target:
                out = np.zeros((n_target, 2), dtype=np.float32)
                if y.size:
                    out[: min(n_target, y.shape[0])] = y[: min(n_target, y.shape[0])]
                y = out
            else:
                y = y[:n_target]
            return y.astype(np.float32, copy=False)
    except Exception:
        return np.zeros((n_target, 2), dtype=np.float32)


def _estimate_aligned_proxy_sum(stems: list[dict[str, Any]], stem_metrics: list[dict[str, Any]], recipe: str, *, target_sr: int = TARGET_SR) -> dict[str, Any]:
    """v63.1.3: aligned proxy-sum estimator.

    It sums short aligned windows after role gain/width, then estimates peak/RMS
    from the actual interaction of stems.  The older contribution-sum upper bound
    remains in the report as a safety reference, but no longer dominates the trim
    unless proxy estimation is unavailable.
    """
    enabled = bool(_env_on("BUSY_BAMIX_V6313_ALIGNED_PROXY_ESTIMATOR", "1"))
    if not enabled:
        return {"enabled": False, "reason": "BUSY_BAMIX_V6313_ALIGNED_PROXY_ESTIMATOR_disabled"}
    if not stems:
        return {"enabled": False, "reason": "no_stems"}
    window_sec = _env_float("BUSY_BAMIX_V6313_PROXY_WINDOW_SEC", 8.0, minimum=2.0, maximum=20.0)
    window_count = _env_int("BUSY_BAMIX_V6313_PROXY_WINDOW_COUNT", 5, minimum=1, maximum=9)
    # Determine a safe common duration hint without opening/reading entire files.
    durations: list[float] = []
    for s in stems:
        try:
            p = Path(str(s.get("local_path") or ""))
            with sf.SoundFile(str(p), "r") as f:
                if f.samplerate > 0 and f.frames > 0:
                    durations.append(float(f.frames) / float(f.samplerate))
        except Exception:
            pass
    dur = max(durations) if durations else window_sec
    max_start = max(0.0, dur - window_sec)
    if window_count <= 1 or max_start <= 0.01:
        starts = [0.0]
    else:
        ratios = np.linspace(0.06, 0.88, int(window_count))
        starts = [float(max_start * float(r)) for r in ratios]
    windows: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    for w_idx, start_sec in enumerate(starts):
        n_target = max(1, int(round(window_sec * int(target_sr))))
        mix = np.zeros((n_target, 2), dtype=np.float32)
        active = 0
        for idx, st in enumerate(stems):
            metric = stem_metrics[idx] if idx < len(stem_metrics) else {}
            role = str(metric.get("role") or "unknown")
            conf = float(metric.get("role_confidence") or 0.0)
            art = float(metric.get("artifact_risk") or 0.0)
            gain_db = _role_gain_db(role, recipe, art, conf)
            width = _width_scalar(role, recipe, art)
            seg = _read_aligned_proxy_segment(Path(str(st.get("local_path") or "")), start_sec=start_sec, duration_sec=window_sec, target_sr=target_sr)
            if seg.size and _peak(seg) > 1e-8:
                active += 1
            seg = _apply_width(seg, float(width)) * _amp(float(gain_db))
            mix[: seg.shape[0]] += seg[:n_target]
        peak = _peak(mix)
        rms = _rms(mix)
        crest = _db(peak / max(rms, 1e-9)) if peak > 0 and rms > 0 else None
        row = {
            "window_index": int(w_idx),
            "start_sec": round(float(start_sec), 3),
            "duration_sec": round(float(window_sec), 3),
            "active_stem_count": int(active),
            "peak_dbfs": round(_db(peak), 3),
            "rms_dbfs": round(_db(rms), 3),
            "crest_db": round(float(crest), 3) if crest is not None and math.isfinite(float(crest)) else None,
        }
        windows.append(row)
        # Peak is the authority for headroom; RMS is a tiebreaker for dense windows.
        if best is None or float(row["peak_dbfs"]) > float(best.get("peak_dbfs", -999.0)) or (
            abs(float(row["peak_dbfs"]) - float(best.get("peak_dbfs", -999.0))) < 0.75 and float(row["rms_dbfs"]) > float(best.get("rms_dbfs", -999.0))
        ):
            best = row
    if best is None:
        return {"enabled": False, "reason": "no_proxy_windows"}
    return _jsonable({
        "enabled": True,
        "method": "aligned_role_gain_width_proxy_sum_windows_v6313",
        "target_sr": int(target_sr),
        "duration_hint_sec": round(float(dur), 3),
        "window_count": int(len(windows)),
        "selected_window": best,
        "windows_preview": windows[:9],
        "policy": "actual aligned proxy sum is primary; worst-case stem peak summation remains a safety reference only",
    })


def _bamix_common_duration(stems: list[dict[str, Any]], fallback: float = 30.0) -> float:
    durations: list[float] = []
    for st in stems:
        try:
            sr = float(st.get("sample_rate") or 0.0)
            frames = float(st.get("frames") or 0.0)
            if sr > 0.0 and frames > 0.0:
                durations.append(frames / sr)
                continue
        except Exception:
            pass
        try:
            p = Path(str(st.get("local_path") or ""))
            with sf.SoundFile(str(p), "r") as f:
                if f.samplerate > 0 and f.frames > 0:
                    durations.append(float(f.frames) / float(f.samplerate))
        except Exception:
            pass
    return max(durations) if durations else float(fallback)


def _bamix_advisory_window_starts(duration: float, original_features: dict[str, Any] | None, *, window_sec: float, count: int) -> list[dict[str, Any]]:
    duration = max(float(duration or 0.0), float(window_sec))
    max_start = max(0.0, duration - float(window_sec))
    candidates: list[tuple[str, float]] = [
        ("early_after_intro", min(max_start, max(0.0, duration * 0.12))),
        ("middle_groove", min(max_start, max(0.0, duration * 0.45))),
    ]
    seg = (original_features or {}).get("segment_analysis") if isinstance(original_features, dict) else {}
    summary = seg.get("summary") if isinstance(seg, dict) else {}
    if isinstance(summary, dict):
        for key in ("loudest_10s", "most_compressed_section", "brightest_10s", "most_bass_heavy_10s"):
            item = summary.get(key) if isinstance(summary.get(key), dict) else {}
            if item.get("start_sec") is not None:
                try:
                    candidates.append((key, min(max_start, max(0.0, float(item.get("start_sec"))))))
                except Exception:
                    pass
    candidates.append(("late_chorus_or_drop", min(max_start, max(0.0, duration * 0.72))))

    out: list[dict[str, Any]] = []
    min_gap = max(1.0, float(window_sec) * 0.55)
    for label, start in candidates:
        start = float(np.clip(start, 0.0, max_start))
        if all(abs(start - float(prev.get("start_sec") or 0.0)) >= min_gap for prev in out):
            out.append({"label": label, "start_sec": round(start, 3), "duration_sec": round(float(window_sec), 3)})
        if len(out) >= int(count):
            break
    idx = 0
    while len(out) < int(count):
        frac = (len(out) + 1) / float(int(count) + 1)
        start = float(np.clip(max_start * frac, 0.0, max_start))
        if all(abs(start - float(prev.get("start_sec") or 0.0)) >= 0.75 for prev in out):
            out.append({"label": f"fallback_{idx}", "start_sec": round(start, 3), "duration_sec": round(float(window_sec), 3)})
        idx += 1
        if idx > 12:
            break
    return out[: max(1, int(count))]


def _rough_mix_window_from_stems(
    stems: list[dict[str, Any]],
    stem_metrics: list[dict[str, Any]],
    recipe: str,
    *,
    start_sec: float,
    duration_sec: float,
    target_sr: int,
) -> np.ndarray:
    n_target = max(1, int(round(float(duration_sec) * int(target_sr))))
    mix = np.zeros((n_target, 2), dtype=np.float32)
    for idx, st in enumerate(stems):
        metric = stem_metrics[idx] if idx < len(stem_metrics) and isinstance(stem_metrics[idx], dict) else {}
        role = str(metric.get("role") or "unknown")
        conf = float(metric.get("role_confidence") or 0.0)
        art = float(metric.get("artifact_risk") or 0.0)
        gain_db = _role_gain_db(role, recipe, art, conf)
        width = _width_scalar(role, recipe, art)
        seg = _read_aligned_proxy_segment(Path(str(st.get("local_path") or "")), start_sec=start_sec, duration_sec=duration_sec, target_sr=target_sr)
        if seg.shape[0] < n_target:
            pad = np.zeros((n_target, 2), dtype=np.float32)
            if seg.size:
                pad[: min(n_target, seg.shape[0])] = _ensure_stereo(seg)[: min(n_target, seg.shape[0])]
            seg = pad
        seg = _apply_width(_ensure_stereo(seg[:n_target]), float(width)) * _amp(float(gain_db))
        mix += seg.astype(np.float32, copy=False)
    peak = _peak(mix)
    if peak > 0.98:
        mix *= float(0.92 / max(peak, 1e-9))
    elif 0.0 < peak < 0.06:
        mix *= min(6.0, float(0.18 / max(peak, 1e-9)))
    return np.nan_to_num(mix, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)


def _normalize_observation_view(x: np.ndarray, *, target_peak: float = 0.78) -> tuple[np.ndarray, dict[str, Any]]:
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    rms = _rms(arr)
    gain = 1.0
    if peak > 1e-7:
        gain = min(12.0, max(0.15, float(target_peak) / peak))
        if peak * gain > 0.96:
            gain = 0.96 / peak
    out = np.clip(arr * float(gain), -0.98, 0.98).astype(np.float32, copy=False)
    return out, {
        "pre_norm_peak_dbfs": round(_db(peak), 3),
        "pre_norm_rms_dbfs": round(_db(rms), 3),
        "listening_gain_db": round(20.0 * math.log10(max(float(gain), 1e-9)), 3),
    }


def _encode_bamix_observation_audio(audio: np.ndarray, sr: int, work_dir: Path) -> tuple[str, str, dict[str, Any]]:
    bitrate = str(os.environ.get("BUSY_BAMIX_GEMINI_OBSERVATION_MP3_BITRATE", "96k") or "96k")
    prefer_mp3 = _env_on("BUSY_BAMIX_GEMINI_OBSERVATION_MP3", "1")
    ffmpeg = shutil.which(str(os.environ.get("BUSY_FFMPEG_BINARY", "ffmpeg") or "ffmpeg"))
    encode_report: dict[str, Any] = {
        "preferred_format": "mp3" if prefer_mp3 else "wav",
        "mp3_bitrate": bitrate,
        "ffmpeg_available": bool(ffmpeg),
    }
    if prefer_mp3 and ffmpeg:
        try:
            with tempfile.TemporaryDirectory(dir=str(work_dir)) as td:
                wav_path = Path(td) / "bamix_gemini_observation.wav"
                mp3_path = Path(td) / "bamix_gemini_observation.mp3"
                sf.write(str(wav_path), audio.astype(np.float32, copy=False), int(sr), format="WAV", subtype="PCM_16")
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path), "-ac", "1", "-ar", str(int(sr)), "-b:a", bitrate, str(mp3_path)]
                subprocess.run(cmd, check=True, timeout=_env_float("BUSY_BAMIX_GEMINI_OBSERVATION_FFMPEG_TIMEOUT_SEC", 45.0, minimum=5.0, maximum=180.0))
                data = mp3_path.read_bytes()
                encode_report.update({"format": "mp3", "mime_type": "audio/mp3", "bytes": len(data), "encoded_with": "ffmpeg"})
                return base64.b64encode(data).decode("ascii"), "audio/mp3", encode_report
        except Exception as exc:
            encode_report.update({"mp3_error": str(exc)[:400], "fallback": "wav_pcm16"})
    bio = io.BytesIO()
    sf.write(bio, audio.astype(np.float32, copy=False), int(sr), format="WAV", subtype="PCM_16")
    data = bio.getvalue()
    encode_report.update({"format": "wav", "mime_type": "audio/wav", "bytes": len(data), "encoded_with": "soundfile"})
    return base64.b64encode(data).decode("ascii"), "audio/wav", encode_report


def _build_bamix_gemini_observation(
    stems: list[dict[str, Any]],
    stem_metrics: list[dict[str, Any]],
    recipe: str,
    original_features: dict[str, Any] | None,
    work_dir: Path,
) -> tuple[str | None, str | None, dict[str, Any]]:
    if not stems:
        return None, None, {"available": False, "reason": "no_stems"}
    target_sr = _env_int("BUSY_BAMIX_GEMINI_OBSERVATION_SR", 24000, minimum=12000, maximum=48000)
    window_sec = _env_float("BUSY_BAMIX_GEMINI_OBSERVATION_WINDOW_SEC", 8.0, minimum=3.0, maximum=20.0)
    window_count = _env_int("BUSY_BAMIX_GEMINI_OBSERVATION_WINDOWS", 4, minimum=1, maximum=8)
    duration = _bamix_common_duration(stems, fallback=window_sec)
    windows = _bamix_advisory_window_starts(duration, original_features, window_sec=window_sec, count=window_count)
    rough_windows: list[np.ndarray] = []
    for w in windows:
        rough_windows.append(_rough_mix_window_from_stems(stems, stem_metrics, recipe, start_sec=float(w.get("start_sec") or 0.0), duration_sec=float(w.get("duration_sec") or window_sec), target_sr=target_sr))
    gap = np.zeros(int(round(float(target_sr) * 0.18)), dtype=np.float32)
    view_specs = [
        ("sum_fold_down", lambda y: np.mean(y, axis=1)),
        ("left_only", lambda y: y[:, 0]),
        ("right_only", lambda y: y[:, 1]),
        ("mid_signal", lambda y: (y[:, 0] + y[:, 1]) * 0.5),
        ("side_signal", lambda y: (y[:, 0] - y[:, 1]) * 0.5),
    ]
    pieces: list[np.ndarray] = []
    manifest_views: list[dict[str, Any]] = []
    cursor = 0.0
    for view_name, fn in view_specs:
        if pieces:
            pieces.append(gap * 0.0)
            cursor += len(gap) / float(target_sr)
        view_pieces: list[np.ndarray] = []
        stats: list[dict[str, Any]] = []
        view_start = cursor
        for idx, rough in enumerate(rough_windows):
            mono, st = _normalize_observation_view(fn(rough), target_peak=0.76 if view_name != "side_signal" else 0.82)
            st.update({"source_window_index": idx, "source_start_sec": windows[idx].get("start_sec"), "source_duration_sec": windows[idx].get("duration_sec")})
            stats.append(st)
            if view_pieces:
                view_pieces.append(gap)
                cursor += len(gap) / float(target_sr)
            view_pieces.append(mono)
            cursor += len(mono) / float(target_sr)
        view_audio = np.concatenate(view_pieces).astype(np.float32, copy=False) if view_pieces else np.zeros(1, dtype=np.float32)
        pieces.append(view_audio)
        manifest_views.append({
            "view": view_name,
            "start_sec_in_file": round(float(view_start), 3),
            "duration_sec_in_file": round(float(len(view_audio) / max(target_sr, 1)), 3),
            "normalization": stats,
        })
    observation = np.concatenate(pieces).astype(np.float32, copy=False)
    peak = float(np.max(np.abs(observation))) if observation.size else 0.0
    if peak > 0.98:
        observation *= float(0.96 / max(peak, 1e-9))
    b64, mime, enc = _encode_bamix_observation_audio(observation, target_sr, work_dir)
    summary = {
        "available": True,
        "schema_version": _SCHEMA + ".gemini_rough_stem_mix_observation_v643",
        "observation_kind": "rough_stem_mix_sum_l_r_mid_side",
        "source_policy": "representative aligned rough stem mix windows; not the mastered output and not injected into audio path",
        "recipe_used_for_rough_mix": recipe,
        "target_sr": int(target_sr),
        "channels": 1,
        "total_audio_sec": round(float(len(observation) / max(target_sr, 1)), 3),
        "source_duration_hint_sec": round(float(duration), 3),
        "windows": windows,
        "views": manifest_views,
        "encoding": enc,
        "mime_type": mime,
        "cost_policy": "mp3 observation when ffmpeg is available; wav fallback remains small because this is representative-window observation by default",
    }
    return b64, mime, _jsonable(summary)


def _call_gemini_bamix_mix_advisory(
    *,
    audio_base64: str | None,
    mime_type: str | None,
    observation_summary: dict[str, Any],
    stem_metrics: list[dict[str, Any]],
    reference: dict[str, Any],
    fallback_recipe: dict[str, Any],
    original_features: dict[str, Any] | None,
) -> dict[str, Any]:
    enabled = _env_on("BUSY_BAMIX_GEMINI_MIX_ADVISORY", os.environ.get("BUSY_GEMINI_AUDIO_JUDGE", "1"))
    base_report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".gemini_producer_mix_advisory_v643",
        "enabled": bool(enabled),
        "available": False,
        "advisory_role": "weak_producer_mix_prior_for_gpt55_planner",
        "direct_dsp_authority": False,
    }
    if not enabled:
        base_report["reason"] = "disabled_by_env"
        return base_report
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("BUSY_GEMINI_API_KEY")
    if not api_key:
        base_report["reason"] = "missing_gemini_api_key"
        return base_report
    if not audio_base64 or not mime_type:
        base_report["reason"] = "missing_observation_audio"
        base_report["observation_summary"] = observation_summary
        return base_report
    model = os.environ.get("BUSY_BAMIX_GEMINI_MIX_ADVISORY_MODEL", os.environ.get("BUSY_GEMINI_AUDIO_MODEL", "gemini-3.1-flash-lite")) or "gemini-3.1-flash-lite"
    gemini_base = os.environ.get("BUSY_GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    prompt = {
        "task": "Listen to the rough stem mix observation and advise a producer-grade Busy Auto Mixing premaster strategy before mastering.",
        "rules": [
            "Return compact JSON only.",
            "The audio file is one concatenated diagnostic observation, not the final master.",
            "Use the manifest to compare SUM, LEFT, RIGHT, MID, and SIDE views.",
            "Do not request new stems, external generation, DAW plugins, or multiple full-length candidate renders.",
            "Give producer/mix-bus advice that can be translated into existing BAMix deterministic modules.",
            "Treat your advice as weak prior only; deterministic measurements and GPT-5.5 planner will make the final strategy.",
            "Focus on what must be fixed in the premaster so the final limiter can reach a commercial -0.1 dBTP WAV without tearing, hollowness, or empty density.",
        ],
        "observation_summary": observation_summary,
        "stem_metric_summary": [
            {
                "filename": str(m.get("filename") or "")[:120],
                "role": m.get("role"),
                "role_confidence": m.get("role_confidence"),
                "artifact_risk": m.get("artifact_risk"),
                "rms_db": m.get("rms_db"),
                "peak_dbfs": m.get("peak_dbfs"),
                "crest_factor_db": m.get("crest_factor_db"),
                "phase_correlation": m.get("phase_correlation"),
                "bands": m.get("bands"),
            }
            for m in stem_metrics[:16] if isinstance(m, dict)
        ],
        "reference_anchor": reference,
        "rule_recommendation": fallback_recipe,
        "input_mastering_metrics": {
            "integrated_lufs": (original_features or {}).get("integrated_lufs") if isinstance(original_features, dict) else None,
            "true_peak_dbtp": (original_features or {}).get("approx_true_peak_dbfs") if isinstance(original_features, dict) else None,
            "crest_factor_db": (original_features or {}).get("crest_factor_db") if isinstance(original_features, dict) else None,
        },
        "required_json_shape": {
            "mix_readiness": "0.0-1.0",
            "overall_problem": "short",
            "premaster_goal": "short",
            "mix_balance_priorities": [{"target": "vocal|drums|bass|music_bed|fx_ambience|mixbus", "action": "short", "priority": "0.0-1.0"}],
            "module_advice": {
                "glue": "off|light|medium|strong",
                "vocal_pocket": "off|light|medium|strong",
                "kick_bass": "off|light|medium|strong",
                "drum_punch": "off|light|medium|strong",
                "harmonic_density": "off|light|medium|strong",
                "elliptical": "off|light|medium|strong",
                "stereo_safety": "off|light|medium|strong",
                "translation_qc": "off|light|medium|strong"
            },
            "stem_augmentation_advice": {
                "low_mid_body_fill": "off|light|medium|strong",
                "vocal_support_body_layer": "off|light|medium|strong",
                "center_anchor": "off|light|medium|strong",
                "bass_harmonic_translation": "off|light|medium|strong",
                "drum_parallel_density": "off|light|medium|strong",
                "transient_ghost": "off|light|medium",
                "side_texture_control": "off|light|medium"
            },
            "left_right_mid_side_findings": {
                "left_right_imbalance": "0.0-1.0",
                "mid_hollow": "0.0-1.0",
                "side_hash_or_fizz": "0.0-1.0",
                "side_overwide_or_phase_risk": "0.0-1.0"
            },
            "limiter_handoff_warning": ["what will break first if final limiter is pushed"],
            "preserve": ["short"],
            "control": ["short"],
            "confidence": "0.0-1.0"
        },
    }
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))},
                {"inline_data": {"mime_type": str(mime_type), "data": audio_base64}},
            ],
        }],
        "generationConfig": {
            "temperature": _env_float("BUSY_BAMIX_GEMINI_MIX_ADVISORY_TEMPERATURE", 0.12, minimum=0.0, maximum=1.0),
            "topP": _env_float("BUSY_BAMIX_GEMINI_MIX_ADVISORY_TOP_P", 0.85, minimum=0.1, maximum=1.0),
            "maxOutputTokens": _env_int("BUSY_BAMIX_GEMINI_MIX_ADVISORY_MAX_OUTPUT_TOKENS", 2200, minimum=512, maximum=8192),
            "responseMimeType": "application/json",
        },
    }
    try:
        url = f"{gemini_base}/models/{model}:generateContent?key={api_key}"
        timeout = _env_float("BUSY_BAMIX_GEMINI_MIX_ADVISORY_TIMEOUT_SEC", 90.0, minimum=10.0, maximum=240.0)
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            return {**base_report, "model": model, "reason": "gemini_api_error", "error": f"{resp.status_code}:{resp.text[:700]}", "observation_summary": observation_summary}
        raw = resp.json()
        text_parts: list[str] = []
        for cand in raw.get("candidates", []) or []:
            content = cand.get("content", {}) if isinstance(cand, dict) else {}
            for part in content.get("parts", []) or []:
                if isinstance(part, dict) and part.get("text"):
                    text_parts.append(str(part.get("text") or ""))
        text = "".join(text_parts).strip()
        parsed = _extract_json_from_text(text)
        ok = isinstance(parsed, dict) and not bool(parsed.get("parse_error"))
        return _jsonable({
            **base_report,
            "available": bool(ok),
            "model": model,
            "reason": None if ok else "json_parse_error",
            "parsed": parsed if ok else {},
            "raw_text": text[:5000],
            "api_usage": raw.get("usageMetadata", {}) if isinstance(raw, dict) else {},
            "observation_summary": observation_summary,
            "fusion_policy": {
                "gemini_is_weak_prior_only": True,
                "gpt55_planner_must_cross_check_with_metrics": True,
                "deterministic_clamps_remain_final_authority": True,
            },
        })
    except Exception as exc:
        return {**base_report, "model": model, "reason": "exception", "error": str(exc)[:700], "observation_summary": observation_summary}


def _build_and_run_bamix_gemini_mix_advisory(
    stems: list[dict[str, Any]],
    stem_metrics: list[dict[str, Any]],
    recipe: str,
    *,
    original_features: dict[str, Any] | None,
    reference: dict[str, Any],
    fallback_recipe: dict[str, Any],
    work_dir: Path,
    log_callback: Any | None = None,
) -> dict[str, Any]:
    if not _env_on("BUSY_BAMIX_GEMINI_MIX_ADVISORY", os.environ.get("BUSY_GEMINI_AUDIO_JUDGE", "1")):
        return {"schema_version": _SCHEMA + ".gemini_producer_mix_advisory_v643", "enabled": False, "available": False, "reason": "disabled_by_env"}
    def _log_local(event: str, **fields: Any) -> None:
        if callable(log_callback):
            try:
                log_callback(event, **_jsonable(fields))
            except Exception:
                pass
    try:
        _log_local("busy_auto_mixing_gemini_observation_build_start", recipe=recipe)
        b64, mime, summary = _build_bamix_gemini_observation(stems, stem_metrics, recipe, original_features, work_dir)
        _log_local("busy_auto_mixing_gemini_observation_build_done", available=summary.get("available"), seconds=summary.get("total_audio_sec"), mime=mime, bytes=((summary.get("encoding") or {}).get("bytes") if isinstance(summary.get("encoding"), dict) else None))
    except Exception as exc:
        return {"schema_version": _SCHEMA + ".gemini_producer_mix_advisory_v643", "enabled": True, "available": False, "reason": "observation_build_exception", "error": str(exc)[:700]}
    _log_local("busy_auto_mixing_gemini_mix_advisory_start", mime=mime)
    advisory = _call_gemini_bamix_mix_advisory(
        audio_base64=b64,
        mime_type=mime,
        observation_summary=summary,
        stem_metrics=stem_metrics,
        reference=reference,
        fallback_recipe=fallback_recipe,
        original_features=original_features,
    )
    _log_local("busy_auto_mixing_gemini_mix_advisory_done", available=advisory.get("available"), reason=advisory.get("reason"), model=advisory.get("model"))
    return advisory


def _estimate_initial_premix_gain_db(stems: list[dict[str, Any]], stem_metrics: list[dict[str, Any]], recipe: str, *, original_decision: dict[str, Any] | None = None, reference_db: dict[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
    if not _env_on("BUSY_AUTOMIX_PRERENDER_GAIN_ESTIMATOR", "1"):
        return 0.0, {"enabled": False}
    targets = _recipe_premaster_targets(recipe, stem_metrics, original_decision=original_decision, reference_db=reference_db)
    preferred = float(targets.get("tp_target") or _env_float("BUSY_AUTOMIX_PREMASTER_TARGET_TP_DBTP", -3.0, minimum=-7.0, maximum=-1.0))
    margin = _env_float("BUSY_AUTOMIX_PRERENDER_GAIN_MARGIN_DB", 0.25, minimum=0.0, maximum=2.0)
    max_trim = abs(float(targets.get("max_trim") or 7.5))
    predicted_sum = 0.0
    parts: list[dict[str, Any]] = []
    for idx, s in enumerate(stems):
        metric = stem_metrics[idx] if idx < len(stem_metrics) else {}
        role = str(metric.get("role") or "unknown")
        conf = float(metric.get("role_confidence") or 0.0)
        art = float(metric.get("artifact_risk") or 0.0)
        gain_db = _role_gain_db(role, recipe, art, conf)
        peak_db = metric.get("peak_dbfs")
        try:
            peak_amp = _amp(float(peak_db))
        except Exception:
            peak_amp = 0.35
        contrib = peak_amp * _amp(gain_db)
        predicted_sum += max(0.0, contrib)
        parts.append({"filename": s.get("filename"), "role": role, "peak_dbfs": peak_db, "gain_db": round(gain_db, 3), "predicted_peak_contribution": round(contrib, 6)})
    worstcase_db = _db(predicted_sum)
    aligned = _estimate_aligned_proxy_sum(stems, stem_metrics, recipe, target_sr=TARGET_SR)
    use_db = worstcase_db
    authority = "worstcase_stem_peak_sum_fallback"
    proxy_safety_pad = _env_float("BUSY_BAMIX_V6313_PROXY_SAFETY_PAD_DB", 0.55, minimum=0.0, maximum=4.0)
    legacy_max_proxy_gap = _env_float("BUSY_BAMIX_V6313_MAX_PROXY_TO_UPPER_BOUND_DB", 3.25, minimum=0.5, maximum=9.0)
    high_conf_gap = _env_float("BUSY_BAMIX_V6313_2_PROXY_HIGH_CONFIDENCE_UPPER_SAFETY_DB", 0.85, minimum=0.25, maximum=3.0)
    low_conf_gap = _env_float("BUSY_BAMIX_V6313_2_PROXY_LOW_CONFIDENCE_UPPER_SAFETY_DB", 1.65, minimum=0.5, maximum=4.0)
    calibrated_cap = _env_float("BUSY_BAMIX_V6313_2_PROXY_UPPER_SAFETY_CAP_DB", 1.85, minimum=0.5, maximum=4.0)
    proxy_db = None
    proxy_confidence = 0.0
    calibrated_proxy_gap = None
    if isinstance(aligned, dict) and aligned.get("enabled"):
        sw = aligned.get("selected_window") if isinstance(aligned.get("selected_window"), dict) else {}
        try:
            proxy_db = float(sw.get("peak_dbfs")) + float(proxy_safety_pad)
        except Exception:
            proxy_db = None
        try:
            rows = aligned.get("windows_preview") if isinstance(aligned.get("windows_preview"), list) else []
            peaks = [float(r.get("peak_dbfs")) for r in rows if isinstance(r, dict) and r.get("peak_dbfs") is not None]
            active_counts = [float(r.get("active_stem_count") or 0.0) for r in rows if isinstance(r, dict)]
            peak_spread = (max(peaks) - min(peaks)) if peaks else 99.0
            avg_active = (sum(active_counts) / max(len(active_counts), 1)) if active_counts else 0.0
            proxy_confidence = 0.35
            if len(peaks) >= 3:
                proxy_confidence += 0.20
            if avg_active >= max(2.0, min(float(len(stems)), 4.0) * 0.55):
                proxy_confidence += 0.25
            if peak_spread <= 5.0:
                proxy_confidence += 0.12
            if proxy_db is not None and proxy_db > -1.5:
                proxy_confidence += 0.08
            proxy_confidence = float(np.clip(proxy_confidence, 0.0, 1.0))
        except Exception:
            proxy_confidence = 0.35
    if proxy_db is not None and math.isfinite(float(proxy_db)):
        # v63.1.3.2: the aligned proxy is the authority when it is available.
        # The old stem-peak sum remains visible as a legacy safety reference but
        # no longer adds a fixed +3.25 dB upper-bound pad that repeatedly caused
        # first renders to land under-driven.  A calibrated confidence-weighted
        # gap keeps headroom without returning to worst-case summation.
        calibrated_proxy_gap = float(low_conf_gap - (low_conf_gap - high_conf_gap) * float(proxy_confidence))
        calibrated_proxy_gap = float(np.clip(calibrated_proxy_gap, min(high_conf_gap, low_conf_gap), float(calibrated_cap)))
        use_db = max(float(proxy_db), min(float(worstcase_db), float(proxy_db) + float(calibrated_proxy_gap)))
        authority = "aligned_proxy_sum_primary_with_calibrated_upper_safety_v6313_2"
    gain = 0.0
    if math.isfinite(float(use_db)):
        gain = min(0.0, preferred - margin - float(use_db))
    raw_gain = float(gain)
    gain = float(np.clip(gain, -max_trim, 0.0))
    # v63.1.3: if aligned proxy says the old upper-bound is very conservative,
    # avoid landing at the exact max-trim clamp unless the proxy also predicts risk.
    unclamp_delta = None
    if proxy_db is not None and math.isfinite(float(proxy_db)) and bool(abs(raw_gain - gain) > 1e-6):
        proxy_gain = min(0.0, preferred - margin - float(proxy_db))
        unclamp_delta = float(proxy_gain - gain)
        if proxy_gain > gain:
            gain = float(max(gain, proxy_gain))
            authority += "+clamp_relaxed_by_aligned_proxy"
    return gain, _jsonable({
        "enabled": True,
        "schema_version": _SCHEMA + ".prerender_gain_estimator_v6313_2",
        "method": "aligned_proxy_sum_primary_calibrated_upper_safety_v6313_2",
        "premaster_targets": targets,
        "preferred_true_peak_dbtp": preferred,
        "margin_db": margin,
        "max_trim_db": max_trim,
        "raw_estimated_gain_db": round(raw_gain, 3),
        "trim_was_clamped": bool(abs(raw_gain - gain) > 1e-6),
        "authority": authority,
        "worstcase_predicted_sum_peak_dbfs": round(worstcase_db, 3),
        "aligned_proxy": aligned,
        "aligned_proxy_safety_pad_db": round(float(proxy_safety_pad), 3),
        "legacy_max_proxy_to_upper_bound_db": round(float(legacy_max_proxy_gap), 3),
        "calibrated_proxy_confidence": round(float(proxy_confidence), 4),
        "calibrated_proxy_upper_safety_db": round(float(calibrated_proxy_gap), 3) if calibrated_proxy_gap is not None else None,
        "predicted_uncorrected_sum_peak_dbfs": round(float(use_db), 3),
        "initial_premix_gain_db": round(gain, 3),
        "clamp_relaxation_db": round(float(unclamp_delta), 3) if unclamp_delta is not None else None,
        "parts_preview": parts[:16],
        "policy": "v63.1.3.2 uses aligned proxy-sum authority with calibrated confidence-weighted upper safety; worst-case stem peak sum is report-only unless proxy estimation is unavailable",
    })


def _v6342_peak_capped_rms_density(x: np.ndarray, *, drive_db: float, wet: float, before_peak: float) -> tuple[np.ndarray, dict[str, Any]]:
    """Peak-capped RMS/crest density engine for v63.4.2.

    v63.3.8/v63.3.9 intentionally preserved RMS while creating peak relief.
    That fixed hot TP, but left the premaster under-dense.  This deterministic
    helper spends only a bounded part of the peak relief on low/mid-level body:
    it applies a soft upward curve to quieter samples, blends a saturated
    parallel body copy, then clamps the result to keep at least a small amount
    of peak relief.  It is a premaster handoff density primitive, not a final
    limiter or loudness chaser.
    """
    y = np.asarray(x, dtype=np.float32)
    if y.size == 0:
        return y, {"active": False, "reason": "empty"}
    try:
        peak0 = float(max(before_peak, _peak(y), 1e-9))
    except Exception:
        peak0 = float(max(_peak(y), 1e-9))
    # Use a robust level so one rogue sample does not prevent upward density.
    try:
        p_ref = float(np.percentile(np.abs(y), 97.5))
    except Exception:
        p_ref = peak0
    p_ref = max(p_ref, peak0 * 0.22, 1e-9)
    env = np.clip(np.abs(y) / p_ref, 0.0, 1.0).astype(np.float32, copy=False)
    amount = _env_float("BUSY_BAMIX_V6342_RMS_DENSITY_AMOUNT", 0.18, minimum=0.0, maximum=0.55) * float(np.clip(drive_db / 3.0, 0.35, 1.35))
    gamma = _env_float("BUSY_BAMIX_V6342_RMS_DENSITY_GAMMA", 1.75, minimum=0.6, maximum=4.0)
    upward_gain = 1.0 + float(amount) * np.power(np.maximum(1.0 - env, 0.0), float(gamma))
    up = (y * upward_gain.astype(np.float32, copy=False)).astype(np.float32, copy=False)
    # Add a very low parallel saturated copy.  The copy is level-bound by the
    # later peak cap, so it can add density without reintroducing rogue peaks.
    body_drive = _amp(min(max(float(drive_db) * 0.55, 0.35), 2.4))
    sat = np.tanh(y * np.float32(body_drive)) / max(float(body_drive), 1e-6)
    sat_wet = _env_float("BUSY_BAMIX_V6342_PARALLEL_SAT_WET", 0.16, minimum=0.0, maximum=0.5) * float(np.clip(drive_db / 3.0, 0.4, 1.25))
    out = (up * (1.0 - sat_wet) + sat.astype(np.float32, copy=False) * sat_wet).astype(np.float32, copy=False)
    keep_relief_db = _env_float("BUSY_BAMIX_V6342_RMS_DENSITY_KEEP_PEAK_RELIEF_DB", 0.18, minimum=0.0, maximum=2.0)
    target_peak = peak0 * _amp(-float(keep_relief_db))
    pk = _peak(out)
    peak_cap_scale = 1.0
    if pk > target_peak > 1e-9:
        peak_cap_scale = float(target_peak / max(pk, 1e-9))
        out = (out * np.float32(peak_cap_scale)).astype(np.float32, copy=False)
    return out.astype(np.float32, copy=False), {
        "active": True,
        "schema_version": _SCHEMA + ".rms_density_engine_v6342",
        "amount": round(float(amount), 4),
        "gamma": round(float(gamma), 3),
        "parallel_sat_wet": round(float(sat_wet), 4),
        "target_peak_dbfs": round(float(_db(target_peak)), 3),
        "peak_cap_scale": round(float(peak_cap_scale), 4),
        "policy": "upward/parallel density under a peak cap; consumes only bounded peak relief before handoff makeup",
    }


def _v6350_density_limiter_workload_chain(x: np.ndarray, *, drive_db: float, before_peak: float, before_rms: float) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.5.0 peak-efficient density / limiter workload chain.

    This is the first priority deep-research upgrade: build more perceived
    density before the final limiter by chaining deterministic soft peak shaving,
    upward/parallel density, harmonic saturation and a hard peak-reserve guard.
    It is not activated by a single blocker name; the correction router invokes
    it when the premaster handoff contract predicts excess final-limiter work.
    """
    y = np.asarray(x, dtype=np.float32)
    if y.size == 0:
        return y, {"active": False, "reason": "empty"}
    if not _env_on("BUSY_BAMIX_V6350_DENSITY_LIMITER_WORKLOAD_CHAIN", "1"):
        return y, {"active": False, "reason": "disabled_by_env"}
    try:
        drive = float(np.clip(float(drive_db or 0.0), 0.0, 6.0))
    except Exception:
        drive = 0.0
    if drive <= 1e-6:
        return y, {"active": False, "reason": "zero_drive"}
    peak0 = float(max(float(before_peak or 0.0), _peak(y), 1e-9))
    rms0 = float(max(float(before_rms or 0.0), _rms(y), 1e-12))
    out = y.astype(np.float32, copy=True)
    stage_reports: list[dict[str, Any]] = []

    # Stage 1: deterministic clipper-before-limiter proxy.  It only rounds stray
    # tops and is later peak-capped, so it shifts transient load away from the
    # downstream limiter without becoming the final limiter itself.
    clip_drive_db = min(_env_float("BUSY_BAMIX_V6350_CLIPPER_DRIVE_DB", 0.55, minimum=0.0, maximum=2.4) + drive * 0.18, _env_float("BUSY_BAMIX_V6350_CLIPPER_DRIVE_MAX_DB", 1.55, minimum=0.0, maximum=3.0))
    clip_mix = _env_float("BUSY_BAMIX_V6350_CLIPPER_MIX", 0.20, minimum=0.0, maximum=0.55) * float(np.clip(drive / 3.0, 0.45, 1.25))
    pk_before_clip = _peak(out)
    if clip_drive_db > 1e-6 and clip_mix > 1e-6:
        drv = _amp(float(clip_drive_db))
        denom = max(float(np.tanh(drv)), 1e-6)
        clipped = (np.tanh(out * np.float32(drv)) / np.float32(max(float(drv), 1e-6))).astype(np.float32, copy=False)
        # v63.5.0.1: keep small-signal gain at unity. The previous tanh(drive)
        # normalization expanded most of the waveform and then relied on the
        # reserve cap, which was not a true clipper-before-limiter behaviour.
        out = (out * (1.0 - clip_mix) + clipped * clip_mix).astype(np.float32, copy=False)
    pk_after_clip = _peak(out)
    stage_reports.append({
        "stage": "stage1_clipper_before_limiter_proxy",
        "drive_db": round(float(clip_drive_db), 3),
        "mix": round(float(clip_mix), 4),
        "clip_shave_db": round(float(max(0.0, _db(pk_before_clip) - _db(pk_after_clip))), 3),
        "normalization": "unity_small_signal_tanh_over_drive_v6350_1",
    })

    # Stage 2: upward/parallel density.  Low and mid-level material is raised
    # more than near-peak material, preserving punch better than a broad gain push.
    abs_mono = np.max(np.abs(out), axis=1) if out.ndim == 2 else np.abs(out)
    try:
        ref = float(np.percentile(abs_mono[abs_mono > 1e-9], 96.0))
    except Exception:
        ref = float(_peak(out) * 0.62)
    ref = max(ref, peak0 * 0.18, 1e-9)
    env = np.clip(abs_mono / ref, 0.0, 1.0).astype(np.float32, copy=False)
    gate = np.clip((abs_mono - ref * _env_float("BUSY_BAMIX_V6350_UPWARD_GATE_REF_RATIO", 0.035, minimum=0.0, maximum=0.25)) / max(ref * 0.20, 1e-9), 0.0, 1.0).astype(np.float32, copy=False)
    amount = _env_float("BUSY_BAMIX_V6350_UPWARD_DENSITY_AMOUNT", 0.16, minimum=0.0, maximum=0.55) * float(np.clip(drive / 3.0, 0.45, 1.35))
    gamma = _env_float("BUSY_BAMIX_V6350_UPWARD_GAMMA", 1.55, minimum=0.6, maximum=4.0)
    up_gain = 1.0 + float(amount) * np.power(np.maximum(1.0 - env, 0.0), float(gamma)) * gate
    if out.ndim == 2:
        out = (out * up_gain[:, None]).astype(np.float32, copy=False)
    else:
        out = (out * up_gain).astype(np.float32, copy=False)
    stage_reports.append({
        "stage": "stage2_upward_parallel_density",
        "amount": round(float(amount), 4),
        "gamma": round(float(gamma), 3),
        "active_sample_ratio": round(float(np.mean((up_gain - 1.0) > 0.005)) if np.size(up_gain) else 0.0, 6),
    })

    # Stage 3: bounded harmonic saturation.  Blend the saturated body copy very
    # low; the following peak reserve decides whether any RMS gain is allowed.
    sat_drive_db = min(_env_float("BUSY_BAMIX_V6350_HARMONIC_SAT_DRIVE_DB", 0.85, minimum=0.0, maximum=3.0) + drive * 0.22, _env_float("BUSY_BAMIX_V6350_HARMONIC_SAT_DRIVE_MAX_DB", 2.05, minimum=0.0, maximum=4.0))
    sat_mix = _env_float("BUSY_BAMIX_V6350_HARMONIC_SAT_MIX", 0.13, minimum=0.0, maximum=0.45) * float(np.clip(drive / 3.0, 0.45, 1.25))
    if sat_drive_db > 1e-6 and sat_mix > 1e-6:
        drv = _amp(float(sat_drive_db))
        sat = (np.tanh(out * np.float32(drv)) / np.float32(max(float(drv), 1e-6))).astype(np.float32, copy=False)
        # v63.5.0.1: this stage should add bounded saturation density, not
        # broad-band gain expansion. Harmonic translation-specific asymmetric
        # generation belongs to the later bass_harmonic_translation module.
        out = (out * (1.0 - sat_mix) + sat * sat_mix).astype(np.float32, copy=False)
    stage_reports.append({
        "stage": "stage3_bounded_harmonic_saturation",
        "drive_db": round(float(sat_drive_db), 3),
        "mix": round(float(sat_mix), 4),
        "normalization": "unity_small_signal_tanh_over_drive_v6350_1",
    })

    # Stage 4: reserve peak relief for downstream true-peak safety.  This keeps
    # the chain from silently spending all relief and pushing the limiter back
    # into rescue mode.
    reserve_db = _env_float("BUSY_BAMIX_V6350_PEAK_RELIEF_RESERVE_DB", 0.25, minimum=0.0, maximum=2.0)
    target_peak = peak0 * _amp(-float(reserve_db))
    pk = _peak(out)
    cap_scale = 1.0
    if pk > target_peak > 1e-9:
        cap_scale = float(target_peak / max(pk, 1e-9))
        out = (out * np.float32(cap_scale)).astype(np.float32, copy=False)
    rms_after = _rms(out)
    peak_after = _peak(out)
    stage_reports.append({
        "stage": "stage4_peak_reserve_guard",
        "reserve_db": round(float(reserve_db), 3),
        "target_peak_dbfs": round(float(_db(target_peak)), 3),
        "cap_scale": round(float(cap_scale), 5),
    })
    return np.nan_to_num(out.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0), {
        "active": True,
        "schema_version": _SCHEMA + ".density_limiter_workload_chain_v6350_1",
        "drive_db": round(float(drive), 3),
        "before_peak_dbfs": round(float(_db(peak0)), 3),
        "after_peak_dbfs": round(float(_db(peak_after)), 3),
        "peak_relief_db": round(float(_db(peak0) - _db(peak_after)), 3),
        "before_rms_db": round(float(_db(rms0)), 3),
        "after_rms_db": round(float(_db(rms_after)), 3),
        "rms_delta_db": round(float(_db(rms_after) - _db(rms0)), 3),
        "stages": stage_reports,
        "policy": "priority-1 density/limiter workload architecture hotfix: clipper/saturation stages use unity-small-signal normalization so they shave peaks/build density without hidden gain expansion before the reserve guard.",
    }

def _apply_density_drive_block(mix: np.ndarray, density_drive_db: float) -> tuple[np.ndarray, dict[str, Any]]:
    """Deterministic in-BAMix density correction.

    v63.3.9 keeps the v63.3.8 peak-efficient density curve from peak-normalized tanh to a
    peak-efficient, crest-reducing curve.  The v63.3.7 normalized curve could
    improve LUFS but also raise premaster true peak, leaving no safe room for
    mono/crest repair downstream.  The new default preserves musical RMS as much
    as peak relief allows, but does not chase a final loudness target or create
    another candidate render.
    """
    try:
        drive_db = float(density_drive_db or 0.0)
    except Exception:
        drive_db = 0.0
    drive_db = float(np.clip(drive_db, 0.0, _env_float("BUSY_AUTOMIX_DENSITY_DRIVE_MAX_DB", 4.8, minimum=0.0, maximum=8.0)))
    if drive_db <= 1e-6:
        return mix, {"active": False, "density_drive_db": 0.0}

    x = mix.astype(np.float32, copy=False)
    wet = _env_float("BUSY_AUTOMIX_DENSITY_DRIVE_WET", 0.55, minimum=0.05, maximum=1.0)
    drive = _amp(drive_db)
    before_peak_db = _db(_peak(x))
    before_rms_db = _db(_rms(x))

    peak_efficient = _env_on("BUSY_BAMIX_V6338_PEAK_EFFICIENT_DENSITY_DRIVE", "1")
    if peak_efficient:
        # Unity-small-signal soft saturation: low-level body is preserved, while
        # isolated high peaks are rounded downward.  This creates premaster
        # headroom for downstream finish stages instead of consuming it.
        soft = np.tanh(x * float(drive)) / max(float(drive), 1e-6)
        out = (x * (1.0 - wet) + soft.astype(np.float32, copy=False) * wet).astype(np.float32, copy=False)
        rms_density_report = {"active": False}
        if _env_on("BUSY_BAMIX_V6342_RMS_DENSITY_ENGINE", "1"):
            out, rms_density_report = _v6342_peak_capped_rms_density(out, drive_db=float(drive_db), wet=float(wet), before_peak=_peak(x))
        v6350_workload_report = {"active": False}
        if _env_on("BUSY_BAMIX_V6350_DENSITY_LIMITER_WORKLOAD_CHAIN", "1"):
            out, v6350_workload_report = _v6350_density_limiter_workload_chain(out, drive_db=float(drive_db), before_peak=_peak(x), before_rms=_rms(x))
        after_peak_pre_makeup_db = _db(_peak(out))
        after_rms_pre_makeup_db = _db(_rms(out))
        try:
            rms_loss_db = max(0.0, float(before_rms_db) - float(after_rms_pre_makeup_db))
            peak_relief_db = max(0.0, float(before_peak_db) - float(after_peak_pre_makeup_db))
        except Exception:
            rms_loss_db = 0.0
            peak_relief_db = 0.0
        keep_relief_db = _env_float("BUSY_BAMIX_V6338_DENSITY_KEEP_PEAK_RELIEF_DB", 0.35, minimum=0.0, maximum=3.0)
        makeup_cap_db = _env_float("BUSY_BAMIX_V6338_DENSITY_RMS_MAKEUP_CAP_DB", 1.15, minimum=0.0, maximum=3.0)
        makeup_db = min(rms_loss_db, max(0.0, peak_relief_db - keep_relief_db), makeup_cap_db)
        if makeup_db > 0.03:
            out = (out * np.float32(_amp(makeup_db))).astype(np.float32, copy=False)
        after_peak_db = _db(_peak(out))
        after_rms_db = _db(_rms(out))
        return out, {
            "active": True,
            "schema_version": _SCHEMA + ".density_drive_block",
            "density_drive_db": round(float(drive_db), 3),
            "wet": round(float(wet), 3),
            "curve": "v6350_density_limiter_workload_chain_after_v6338_peak_efficient_tanh",
            "before_peak_dbfs": round(float(before_peak_db), 3),
            "after_peak_dbfs": round(float(after_peak_db), 3),
            "peak_relief_db": round(float(before_peak_db - after_peak_db), 3),
            "before_rms_db": round(float(before_rms_db), 3),
            "after_rms_db": round(float(after_rms_db), 3),
            "rms_delta_db": round(float(after_rms_db - before_rms_db), 3),
            "rms_makeup_db": round(float(makeup_db), 3),
            "v6342_rms_density_report": _jsonable(rms_density_report),
            "v6350_density_limiter_workload_chain": _jsonable(v6350_workload_report),
            "policy": "single deterministic correction rerender; v63.5.0 routes excess final-limiter workload into a peak-efficient upstream density chain before handoff makeup",
        }

    # Legacy diagnostic mode kept behind env for A/B only.
    denom = max(float(np.tanh(drive)), 1e-6)
    soft = np.tanh(x * float(drive)) / denom
    out = (x * (1.0 - wet) + soft.astype(np.float32, copy=False) * wet).astype(np.float32, copy=False)
    return out, {
        "active": True,
        "schema_version": _SCHEMA + ".density_drive_block",
        "density_drive_db": round(float(drive_db), 3),
        "wet": round(float(wet), 3),
        "curve": "legacy_v6337_normalized_tanh_rms_density_with_soft_peak_rounding",
        "before_peak_dbfs": round(float(before_peak_db), 3),
        "after_peak_dbfs": round(float(_db(_peak(out))), 3),
        "before_rms_db": round(float(before_rms_db), 3),
        "after_rms_db": round(float(_db(_rms(out))), 3),
        "policy": "legacy env-only path; use BUSY_BAMIX_V6338_PEAK_EFFICIENT_DENSITY_DRIVE=0 for comparison",
    }



def _intensity_scalar(value: Any, default: str = "medium") -> float:
    v = str(value if value is not None else default).strip().lower().replace("-", "_")
    table = {
        "off": 0.0,
        "none": 0.0,
        "disabled": 0.0,
        "very_low": 0.25,
        "low": 0.45,
        "light": 0.45,
        "gentle": 0.45,
        "medium": 0.68,
        "moderate": 0.68,
        "med": 0.68,
        "high": 0.88,
        "strong": 0.88,
        "aggressive": 1.0,
    }
    return float(table.get(v, table.get(default, 0.68)))


def _intensity_label(value: Any, default: str = "medium") -> str:
    v = str(value if value is not None else default).strip().lower().replace("-", "_")
    if v in {"off", "none", "disabled"}:
        return "off"
    if v in {"very_low", "low", "light", "gentle"}:
        return "light"
    if v in {"medium", "moderate", "med"}:
        return "medium"
    if v in {"high", "strong", "aggressive"}:
        return "strong"
    return default


def _intensity_from_need(need: float, *, medium_at: float = 0.42, strong_at: float = 0.72, max_label: str = "strong") -> str:
    """Convert a 0..1 evidence score into an audible-but-bounded label.

    v63.3.6: previous deterministic fallback enabled many augmentation modules
    only at ``light``. That was safe, but it made repeated patches look
    inaudible whenever GPT was absent/conservative.  This helper keeps the same
    qualitative clamp labels while allowing evidence-driven medium/strong
    activation before the module-level peak/TP guards run.
    """
    try:
        n = float(np.clip(float(need), 0.0, 1.0))
    except Exception:
        n = 0.0
    label = "strong" if n >= float(strong_at) else "medium" if n >= float(medium_at) else "light"
    return _clamp_intensity_to_max(label, max_label)


def _recipe_module_defaults(recipe: str) -> dict[str, str]:
    r = str(recipe or "clean_balanced_professional").strip().lower()
    table: dict[str, dict[str, str]] = {
        "clean_balanced_professional": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "strong"},
        "clean_balanced": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "medium"},
        "vocal_forward": {"glue": "medium", "vocal_pocket": "strong", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "medium"},
        "vocal_forward_bass_controlled": {"glue": "medium", "vocal_pocket": "strong", "kick_bass": "medium", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "medium"},
        "bass_controlled": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "strong", "drum_punch": "light", "harmonic_density": "light", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "medium"},
        "punch_preserved": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "medium", "drum_punch": "strong", "harmonic_density": "medium", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "strong"},
        "dense_pop": {"glue": "strong", "vocal_pocket": "medium", "kick_bass": "medium", "drum_punch": "medium", "harmonic_density": "strong", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "strong"},
        "club_low_end_controlled": {"glue": "strong", "vocal_pocket": "medium", "kick_bass": "strong", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "strong"},
        "wide_bed_safe": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "medium"},
        "acoustic_natural": {"glue": "light", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "off", "elliptical": "light", "stereo_safety": "medium", "translation_qc": "medium"},
        "cinematic_wide": {"glue": "light", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "medium"},
        "ai_artifact_conservative": {"glue": "light", "vocal_pocket": "light", "kick_bass": "light", "drum_punch": "off", "harmonic_density": "off", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "strong"},
    }
    return dict(table.get(r, table["clean_balanced_professional"]))




def _recipe_module_maxima(recipe: str) -> dict[str, str]:
    """Maximum module intensities allowed by recipe before artifact/confidence scaling.

    GPT-5.5 may recommend emphasis, but it must not be able to escalate an
    artifact-safe or natural recipe into dense-pop style processing.  These
    maxima are intentionally qualitative; numeric DSP values are still owned by
    the deterministic module clamp tables.
    """
    r = str(recipe or "clean_balanced_professional").strip().lower()
    table: dict[str, dict[str, str]] = {
        "clean_balanced_professional": {"glue": "strong", "vocal_pocket": "strong", "kick_bass": "medium", "drum_punch": "strong", "harmonic_density": "strong", "elliptical": "strong", "stereo_safety": "strong", "translation_qc": "strong"},
        "clean_balanced": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "medium", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "strong"},
        "vocal_forward": {"glue": "medium", "vocal_pocket": "strong", "kick_bass": "medium", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "strong"},
        "vocal_forward_bass_controlled": {"glue": "medium", "vocal_pocket": "strong", "kick_bass": "strong", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "strong"},
        "bass_controlled": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "strong", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "strong"},
        "punch_preserved": {"glue": "strong", "vocal_pocket": "medium", "kick_bass": "medium", "drum_punch": "strong", "harmonic_density": "strong", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "strong"},
        "dense_pop": {"glue": "strong", "vocal_pocket": "strong", "kick_bass": "strong", "drum_punch": "strong", "harmonic_density": "strong", "elliptical": "strong", "stereo_safety": "strong", "translation_qc": "strong"},
        "club_low_end_controlled": {"glue": "strong", "vocal_pocket": "medium", "kick_bass": "strong", "drum_punch": "strong", "harmonic_density": "strong", "elliptical": "strong", "stereo_safety": "medium", "translation_qc": "strong"},
        "wide_bed_safe": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "medium", "drum_punch": "medium", "harmonic_density": "medium", "elliptical": "strong", "stereo_safety": "strong", "translation_qc": "strong"},
        "acoustic_natural": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "light", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "strong"},
        "cinematic_wide": {"glue": "medium", "vocal_pocket": "medium", "kick_bass": "light", "drum_punch": "light", "harmonic_density": "medium", "elliptical": "medium", "stereo_safety": "strong", "translation_qc": "strong"},
        "ai_artifact_conservative": {"glue": "light", "vocal_pocket": "light", "kick_bass": "light", "drum_punch": "off", "harmonic_density": "off", "elliptical": "medium", "stereo_safety": "medium", "translation_qc": "strong"},
    }
    return dict(table.get(r, table["clean_balanced_professional"]))


def _clamp_intensity_to_max(label: str, max_label: str) -> str:
    if _intensity_scalar(label, "off") <= _intensity_scalar(max_label, "off") + 1e-9:
        return _intensity_label(label, "medium")
    return _intensity_label(max_label, "medium")

def _extract_ai_module_recommendations(ai_payload: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(ai_payload, dict):
        return out
    planner = ai_payload.get("planner") if isinstance(ai_payload.get("planner"), dict) else ai_payload
    candidates = []
    for key in ["modules", "module_emphasis", "module_plan", "professional_module_plan", "bamix_modules"]:
        if isinstance(planner.get(key), dict):
            candidates.append(planner.get(key) or {})
    for obj in candidates:
        for raw_key, raw_val in obj.items():
            key = str(raw_key).lower().replace("-", "_").replace(" ", "_")
            key = {
                "mixbus_glue": "glue",
                "glue_comp": "glue",
                "vocal_pocket_engine": "vocal_pocket",
                "vocal_pocketing": "vocal_pocket",
                "kick_bass_control": "kick_bass",
                "kick_bass_engine": "kick_bass",
                "drum_punch_engine": "drum_punch",
                "saturation": "harmonic_density",
                "harmonic_density_engine": "harmonic_density",
                "elliptical_low_end": "elliptical",
                "elliptical_low_end_engine": "elliptical",
                "width": "stereo_safety",
                "stereo": "stereo_safety",
                "stereo_depth": "stereo_safety",
                "translation": "translation_qc",
                "premaster_qc": "translation_qc",
            }.get(key, key)
            if key not in {"glue", "vocal_pocket", "kick_bass", "drum_punch", "harmonic_density", "elliptical", "stereo_safety", "translation_qc"}:
                continue
            if isinstance(raw_val, dict):
                if raw_val.get("enabled") is False:
                    out[key] = "off"
                else:
                    out[key] = _intensity_label(raw_val.get("intensity") or raw_val.get("emphasis") or raw_val.get("strength") or raw_val.get("level") or "medium")
            else:
                out[key] = _intensity_label(raw_val, "medium")
    return out




def _augmentation_module_names() -> set[str]:
    return {
        "bass_harmonic_translation",
        "low_mid_body_fill",
        "center_anchor",
        "drum_parallel_density",
        "short_room_early_reflection",
        "vocal_support_body_layer",
        "transient_ghost",
        "side_texture_control",
    }


def _normalize_augmentation_module_name(name: Any) -> str:
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "bass_harmonic": "bass_harmonic_translation",
        "bass_translation": "bass_harmonic_translation",
        "mid_bass_translation": "bass_harmonic_translation",
        "lowmid_body": "low_mid_body_fill",
        "body_fill": "low_mid_body_fill",
        "low_mid_fill": "low_mid_body_fill",
        "mono_anchor": "center_anchor",
        "center_mono_anchor": "center_anchor",
        "drum_density": "drum_parallel_density",
        "parallel_drum_density": "drum_parallel_density",
        "early_reflection": "short_room_early_reflection",
        "short_room": "short_room_early_reflection",
        "vocal_body": "vocal_support_body_layer",
        "vocal_support": "vocal_support_body_layer",
        "vocal_center_body": "vocal_support_body_layer",
        "lead_vocal_body": "vocal_support_body_layer",
        "vocal_fundamental_support": "vocal_support_body_layer",
        "transient_support": "transient_ghost",
        "punch_support": "transient_ghost",
        "side_texture": "side_texture_control",
        "ms_texture_control": "side_texture_control",
    }
    return aliases.get(key, key)


def _extract_ai_augmentation_recommendations(ai_payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Parse GPT-5.5 stem-augmentation advice without granting numeric DSP authority.

    The existing single GPT-5.5 mixing call may now include a stem_augmentation
    section.  This parser intentionally accepts only decision/intensity/reason
    style fields.  The deterministic planner/clamp below owns all actual DSP
    ranges and may override or reject the model.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(ai_payload, dict):
        return out
    planner = ai_payload.get("planner") if isinstance(ai_payload.get("planner"), dict) else ai_payload
    candidates: list[dict[str, Any]] = []
    for key in ["stem_augmentation", "stem_augmentation_assessment", "augmentation", "derived_stems", "assist_stems"]:
        obj = planner.get(key) if isinstance(planner, dict) else None
        if isinstance(obj, dict):
            if isinstance(obj.get("modules"), dict):
                candidates.append(obj.get("modules") or {})
            else:
                candidates.append(obj)
    allowed = _augmentation_module_names()
    for obj in candidates:
        for raw_key, raw_val in obj.items():
            key = _normalize_augmentation_module_name(raw_key)
            if key not in allowed:
                continue
            rec: dict[str, Any] = {}
            if isinstance(raw_val, dict):
                decision = str(raw_val.get("decision") or raw_val.get("state") or raw_val.get("action") or "enable").strip().lower()
                if raw_val.get("enabled") is False:
                    decision = "reject"
                intensity = _intensity_label(raw_val.get("intensity") or raw_val.get("strength") or raw_val.get("level") or ("off" if decision in {"reject", "defer", "off"} else "light"), "light")
                reason = str(raw_val.get("reason") or raw_val.get("rationale") or raw_val.get("why") or "gpt_5_5_augmentation_recommendation")[:240]
            else:
                decision = "enable"
                intensity = _intensity_label(raw_val, "light")
                reason = "gpt_5_5_compact_augmentation_recommendation"
            rec.update({"decision": decision, "intensity": intensity, "reason": reason, "source": "gpt_5_5_single_call"})
            out[key] = rec
    return out


def _stem_augmentation_problem_summary(stem_metrics: list[dict[str, Any]], reference: dict[str, Any] | None = None) -> dict[str, Any]:
    reference = reference if isinstance(reference, dict) else {}
    roles = _build_role_summary(stem_metrics)
    def _role_items(role_names: set[str]) -> list[dict[str, Any]]:
        return [m for m in stem_metrics if str(m.get("role") or "") in role_names]
    drums = _role_items({"drums", "kick", "snare", "hats"})
    bass = _role_items({"bass"})
    music = _role_items({"music_bed", "music", "unknown"})
    vocal = _role_items({"vocal"})
    max_drum_crest = max([float(m.get("crest_factor_db") or 0.0) for m in drums] or [0.0])
    mean_drum_rms = float(np.mean([float(m.get("rms_db") or -120.0) for m in drums])) if drums else -120.0
    bass_sublow = max([float((m.get("bands") or {}).get("sub", 0.0)) + float((m.get("bands") or {}).get("low", 0.0)) for m in bass] or [0.0])
    bass_mid = max([float((m.get("bands") or {}).get("lowmid", 0.0)) + float((m.get("bands") or {}).get("mid", 0.0)) for m in bass] or [0.0])
    bass_translation_deficit = float(np.clip((bass_sublow - bass_mid * 1.4) / 0.55, 0.0, 1.0))
    # v63.8.0: bass translation is not sub boost.  Route only when the bass has
    # more sub/fundamental energy than mid harmonics, or when low-side leakage or
    # vocal/bass overlap means the existing low end will not survive small-speaker
    # and mono playback.  The renderer owns bounded harmonic amount and guards.
    try:
        ref_low_side = float(reference.get("proxy_low_side_over_mid_low_db"))
    except Exception:
        ref_low_side = None
    low_side_need = float(np.clip(((ref_low_side if ref_low_side is not None else -18.0) - _env_float("BUSY_BAMIX_V6380_LOW_SIDE_START_DB", -14.0, minimum=-36.0, maximum=0.0)) / _env_float("BUSY_BAMIX_V6380_LOW_SIDE_RANGE_DB", 12.0, minimum=2.0, maximum=36.0), 0.0, 1.0)) if ref_low_side is not None else 0.0
    music_lowmid = float(np.mean([float((m.get("bands") or {}).get("lowmid", 0.0)) for m in music])) if music else 0.0
    vocal_lowmid = float(np.mean([float((m.get("bands") or {}).get("lowmid", 0.0)) for m in vocal])) if vocal else 0.0
    vocal_presence = float(np.mean([float((m.get("bands") or {}).get("presence", 0.0)) for m in vocal])) if vocal else 0.0
    vocal_mid = float(np.mean([float((m.get("bands") or {}).get("mid", 0.0)) for m in vocal])) if vocal else 0.0
    vocal_art = max([float(m.get("artifact_risk") or 0.0) for m in vocal] or [0.0])
    vocal_conf_mean = float(np.mean([float(m.get("role_confidence") or 0.0) for m in vocal])) if vocal else 0.0
    ref_corr = reference.get("proxy_phase_correlation")
    ref_bands = reference.get("proxy_bands") if isinstance(reference.get("proxy_bands"), dict) else {}
    try:
        ref_mono_delta = float(reference.get("proxy_stereo_minus_mono_lufs_db"))
    except Exception:
        ref_mono_delta = None
    center_corr_need = float(np.clip((0.58 - float(ref_corr if ref_corr is not None else 0.58)) / 0.75, 0.0, 1.0))
    center_mono_need = float(np.clip(((ref_mono_delta or 0.0) - _env_float("BUSY_BAMIX_V6332_CENTER_MONO_DELTA_OK_DB", 2.15, minimum=0.5, maximum=4.5)) / _env_float("BUSY_BAMIX_V6332_CENTER_MONO_DELTA_RANGE_DB", 1.35, minimum=0.3, maximum=4.0), 0.0, 1.0)) if ref_mono_delta is not None else 0.0
    # v63.7.0: route center/body/vocal hollow as a complete capability rather
    # than asking DML or the final limiter to rescue low-mid support.  These are
    # conservative role/proxy signals only; the DSP layer below owns all amounts.
    vocal_foundation = vocal_lowmid + vocal_mid * 0.35
    vocal_frontness_balance = vocal_presence / max(vocal_foundation, 1e-9) if vocal else 0.0
    vocal_body_need = float(np.clip((0.26 - vocal_foundation) / 0.26, 0.0, 1.0)) if vocal else 0.0
    vocal_hollow_need = float(np.clip((1.45 - vocal_frontness_balance) / 1.45, 0.0, 1.0)) if vocal else 0.0
    center_body_need = float(np.clip((0.155 - float(ref_bands.get("lowmid", 0.0) or 0.0)) / 0.155, 0.0, 1.0))
    vocal_support_need = float(np.clip(max(vocal_body_need, vocal_hollow_need * 0.65, max(center_corr_need, center_mono_need) * 0.45) * min(1.0, max(vocal_conf_mean, 0.35) / 0.70), 0.0, 1.0)) if vocal else 0.0
    vocal_bass_conflict_need = float(np.clip(((vocal_lowmid * 0.70 + vocal_mid * 0.20) - max(bass_mid, 1e-9) * 0.35) / 0.22, 0.0, 1.0)) if (vocal and bass) else 0.0
    bass_translation_need = float(np.clip(max(bass_translation_deficit, low_side_need * 0.65, vocal_bass_conflict_need * 0.35), 0.0, 1.0))
    return _jsonable({
        "schema_version": _SCHEMA + ".stem_augmentation_problem_summary",
        "role_summary": roles,
        "drum_density_need_proxy": round(float(np.clip((max_drum_crest - 13.5) / 7.0, 0.0, 1.0)), 4),
        "max_drum_crest_db": round(max_drum_crest, 3),
        "mean_drum_rms_db": round(mean_drum_rms, 3),
        "bass_sublow_fraction_max": round(bass_sublow, 6),
        "bass_lowmid_mid_fraction_max": round(bass_mid, 6),
        "bass_harmonic_translation_deficit_proxy": round(float(bass_translation_deficit), 4),
        "bass_low_side_leakage_need_proxy": round(float(low_side_need), 4),
        "v6380_vocal_bass_conflict_need_proxy": round(float(vocal_bass_conflict_need), 4),
        "bass_harmonic_translation_need_proxy": round(float(bass_translation_need), 4),
        "v6380_bass_harmonic_translation_need_proxy": round(float(bass_translation_need), 4),
        "music_lowmid_fraction_mean": round(music_lowmid, 6),
        "low_mid_body_need_proxy": round(float(np.clip((0.18 - music_lowmid) / 0.18, 0.0, 1.0)), 4),
        "reference_phase_correlation": ref_corr,
        "reference_stereo_minus_mono_lufs_db": round(float(ref_mono_delta), 3) if ref_mono_delta is not None else None,
        "center_anchor_need_from_correlation": round(center_corr_need, 4),
        "center_anchor_need_from_mono_delta": round(center_mono_need, 4),
        "center_anchor_need_proxy": round(max(center_corr_need, center_mono_need), 4),
        "reference_side_high_over_mid_high_db": round(float(reference.get("proxy_side_high_over_mid_high_db") or 0.0), 3) if reference.get("proxy_side_high_over_mid_high_db") is not None else None,
        "reference_side_high_fraction": round(float(reference.get("proxy_side_high_fraction") or 0.0), 6) if reference.get("proxy_side_high_fraction") is not None else None,
        "side_texture_need_proxy": round(float(np.clip(((float(reference.get("proxy_side_high_over_mid_high_db") or 0.0)) - _env_float("BUSY_BAMIX_V6334_SIDE_TEXTURE_EXCESS_START_DB", 1.5, minimum=-3.0, maximum=8.0)) / _env_float("BUSY_BAMIX_V6334_SIDE_TEXTURE_EXCESS_RANGE_DB", 6.0, minimum=1.0, maximum=14.0), 0.0, 1.0)), 4) if reference.get("proxy_side_high_over_mid_high_db") is not None else 0.0,
        "reference_lowmid_fraction": round(float(ref_bands.get("lowmid", 0.0) or 0.0), 6),
        "vocal_lowmid_fraction_mean": round(float(vocal_lowmid), 6),
        "vocal_mid_fraction_mean": round(float(vocal_mid), 6),
        "vocal_presence_fraction_mean": round(float(vocal_presence), 6),
        "vocal_frontness_balance_proxy": round(float(vocal_frontness_balance), 4),
        "v6370_center_body_need_proxy": round(float(center_body_need), 4),
        "v6370_vocal_body_need_proxy": round(float(vocal_body_need), 4),
        "v6370_vocal_hollow_need_proxy": round(float(vocal_hollow_need), 4),
        "v6370_vocal_support_need_proxy": round(float(vocal_support_need), 4),
        "vocal_artifact_risk_max": round(vocal_art, 4),
        "vocal_role_confidence_mean": round(float(vocal_conf_mean), 4),
    })


def _build_stem_augmentation_plan(recipe: str, stem_metrics: list[dict[str, Any]], ai_payload: dict[str, Any] | None = None, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = _stem_augmentation_problem_summary(stem_metrics, reference)
    ai_aug = _extract_ai_augmentation_recommendations(ai_payload)
    roles = summary.get("role_summary") if isinstance(summary.get("role_summary"), dict) else {}
    max_art = max([float(x.get("artifact_risk") or 0.0) for x in stem_metrics] or [0.0])
    mean_conf = float(np.mean([float(x.get("role_confidence") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    modules: dict[str, dict[str, Any]] = {m: {"decision": "defer", "intensity": "off", "reason": "not_needed_by_default", "source": "deterministic_fallback"} for m in _augmentation_module_names()}
    clamps: list[str] = []

    bass_need = float(summary.get("bass_harmonic_translation_need_proxy") or 0.0)
    body_need = float(summary.get("low_mid_body_need_proxy") or 0.0)
    center_need = float(summary.get("center_anchor_need_proxy") or 0.0)
    side_need = float(summary.get("side_texture_need_proxy") or 0.0)
    drum_need = float(summary.get("drum_density_need_proxy") or 0.0)
    vocal_support_need = float(summary.get("v6370_vocal_support_need_proxy") or 0.0)
    center_body_need = float(summary.get("v6370_center_body_need_proxy") or 0.0)
    v645_residue_map = _v645_stem_neural_codec_residue_map(stem_metrics)
    v645_pressure = _v645_residue_pressure_from_map(v645_residue_map)
    if bool(v645_pressure.get("active")):
        side_need = max(side_need, float(v645_pressure.get("effective_side_hf_hash_pressure", v645_pressure.get("side_hf_hash_pressure")) or 0.0) * _env_float("BUSY_BAMIX_V645_SIDE_NEED_PRESSURE_SCALE", 0.92, minimum=0.0, maximum=1.5))
        body_need = max(body_need, float(v645_pressure.get("residue_pressure") or 0.0) * _env_float("BUSY_BAMIX_V645_BODY_NEED_PRESSURE_SCALE", 0.38, minimum=0.0, maximum=1.0))
        summary["v645_stem_neural_codec_residue_map"] = v645_residue_map
        summary["v645_residue_pressure"] = v645_pressure

    if roles.get("bass") and bass_need >= 0.18:
        modules["bass_harmonic_translation"] = {"decision": "enable", "intensity": _intensity_from_need(bass_need, medium_at=0.42, strong_at=0.72), "reason": "v6380_bass_translation_need_from_sub_harmonic_gap_low_side_or_vocal_conflict", "source": "deterministic_evidence_fallback_v6380", "need_score": round(float(bass_need), 4)}
    if roles.get("music_bed") and body_need >= 0.20:
        modules["low_mid_body_fill"] = {"decision": "enable_with_ducking", "intensity": _intensity_from_need(body_need, medium_at=0.42, strong_at=0.72), "reason": "music_bed_low_mid_body_is_sparse", "source": "deterministic_evidence_fallback_v6336"}
    if roles.get("vocal") and vocal_support_need >= _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_ENABLE", 0.16, minimum=0.0, maximum=1.0):
        modules["vocal_support_body_layer"] = {"decision": "enable_with_ducking", "intensity": _intensity_from_need(vocal_support_need, medium_at=0.38, strong_at=0.72), "reason": "vocal_or_center_hollow_body_support_needed", "source": "deterministic_evidence_fallback_v6370", "need_score": round(float(vocal_support_need), 4)}
    if center_need >= 0.12:
        modules["center_anchor"] = {"decision": "enable", "intensity": _intensity_from_need(center_need, medium_at=0.40, strong_at=0.75), "reason": "reference_center_anchor_or_mono_compatibility_need_detected", "source": "deterministic_evidence_fallback_v6336"}
    if side_need >= _env_float("BUSY_BAMIX_V6334_SIDE_TEXTURE_NEED_ENABLE", 0.12, minimum=0.0, maximum=1.0):
        modules["side_texture_control"] = {"decision": "enable", "intensity": _intensity_from_need(side_need, medium_at=0.42, strong_at=0.85, max_label="medium"), "reason": "high_side_texture_or_v645_stem_residue_witness_requires_ms_texture_control", "source": "deterministic_evidence_fallback_v645" if bool(v645_pressure.get("active")) else "deterministic_evidence_fallback_v6336", "need_score": round(float(side_need), 4)}
    if (roles.get("drums") or roles.get("kick") or roles.get("snare")) and drum_need >= 0.10:
        modules["drum_parallel_density"] = {"decision": "enable", "intensity": _intensity_from_need(drum_need, medium_at=0.35, strong_at=0.65), "reason": "drum_crest_decay_density_support_needed", "source": "deterministic_evidence_fallback_v6336"}
    # v63.3.6: persistent crest/transient loss should not wait for an extreme
    # drum-density proxy.  The layer is source-derived, bounded by per-layer peak
    # ratio, and then checked by the effective-delta bus guard.
    if (roles.get("drums") or roles.get("kick") or roles.get("snare")) and drum_need >= _env_float("BUSY_BAMIX_V6336_TRANSIENT_GHOST_NEED_ENABLE", 0.20, minimum=0.05, maximum=1.0):
        modules["transient_ghost"] = {"decision": "enable", "intensity": _intensity_from_need(drum_need, medium_at=0.38, strong_at=0.75, max_label="medium"), "reason": "drum_attack_support_needed_for_persistent_crest_transient_loss", "source": "deterministic_evidence_fallback_v6336"}

    # Merge GPT-5.5 choices as advice only.  The following clamp section may still
    # reduce or reject them based on role availability, artifact risk and env gates.
    for name, rec in ai_aug.items():
        if name not in modules:
            continue
        decision = str(rec.get("decision") or "enable").lower()
        intensity = _intensity_label(rec.get("intensity"), "light")
        if decision in {"reject", "defer", "off", "disable", "bypass"}:
            modules[name] = {**rec, "decision": decision, "intensity": "off"}
        else:
            modules[name] = {**rec, "decision": decision, "intensity": intensity}

    # v63.3.6: let deterministic evidence set a minimum audible floor even when
    # GPT is absent or overly conservative.  Missing-role and artifact gates below
    # still have final authority, so this is not an unsafe bypass.
    def _evidence_floor(name: str, need: float, *, enable_at: float, medium_at: float, strong_at: float, max_label: str = "strong", decision: str = "enable", reason: str = "evidence_floor") -> None:
        if float(need) < float(enable_at):
            return
        rec = modules.get(name, {})
        floor_label = _intensity_from_need(need, medium_at=medium_at, strong_at=strong_at, max_label=max_label)
        cur_dec = str(rec.get("decision") or "").lower()
        if cur_dec in {"reject", "defer", "off", "disable", "bypass", ""} or _intensity_scalar(rec.get("intensity"), "off") < _intensity_scalar(floor_label, "off"):
            modules[name] = {**rec, "decision": decision, "intensity": floor_label, "reason": reason, "source": "deterministic_evidence_floor_v6336", "need_score": round(float(need), 4)}
            clamps.append(f"{name}_raised_to_{floor_label}_by_evidence_floor")

    _evidence_floor("bass_harmonic_translation", bass_need, enable_at=_env_float("BUSY_BAMIX_V6380_BASS_TRANSLATION_ENABLE", 0.16, minimum=0.0, maximum=1.0), medium_at=0.40, strong_at=0.70, max_label="medium", reason="v6380_bass_translation_need_survives_conservative_ai")
    _evidence_floor("low_mid_body_fill", body_need, enable_at=0.20, medium_at=0.42, strong_at=0.72, decision="enable_with_ducking", reason="low_mid_body_need_survives_conservative_ai")
    _evidence_floor("vocal_support_body_layer", vocal_support_need, enable_at=_env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_ENABLE", 0.16, minimum=0.0, maximum=1.0), medium_at=0.38, strong_at=0.72, decision="enable_with_ducking", reason="vocal_center_body_support_need_survives_conservative_ai")
    _evidence_floor("center_anchor", max(center_need, center_body_need * 0.55), enable_at=0.12, medium_at=0.40, strong_at=0.75, reason="center_translation_need_survives_conservative_ai")
    _evidence_floor("side_texture_control", side_need, enable_at=_env_float("BUSY_BAMIX_V6334_SIDE_TEXTURE_NEED_ENABLE", 0.12, minimum=0.0, maximum=1.0), medium_at=0.42, strong_at=0.85, max_label="medium", reason="side_texture_need_survives_conservative_ai")
    _evidence_floor("drum_parallel_density", drum_need, enable_at=0.10, medium_at=0.35, strong_at=0.65, reason="drum_density_need_survives_conservative_ai")
    _evidence_floor("transient_ghost", drum_need, enable_at=_env_float("BUSY_BAMIX_V6336_TRANSIENT_GHOST_NEED_ENABLE", 0.20, minimum=0.05, maximum=1.0), medium_at=0.38, strong_at=0.75, max_label="medium", reason="transient_ghost_need_survives_conservative_ai")

    # v63.3.10: if the stem evidence says the arrangement is highly drum-density
    # driven while low-mid/body support is present but only barely above the old
    # threshold, do not leave the body layer at a nearly inaudible light floor.
    # This is still evidence-gated and role/artifact clamps below keep authority.
    if _env_on("BUSY_BAMIX_V63310_AUG_EFFECTIVENESS_FLOOR", "1"):
        def _raise_aug_floor(name: str, floor_label: str, reason: str) -> None:
            rec = modules.get(name, {})
            cur_dec = str(rec.get("decision") or "").lower()
            if cur_dec in {"reject", "off", "disable", "bypass"}:
                return
            if _intensity_scalar(rec.get("intensity"), "off") < _intensity_scalar(floor_label, "off"):
                modules[name] = {**rec, "decision": rec.get("decision") or "enable", "intensity": floor_label, "reason": reason, "source": "deterministic_effectiveness_floor_v63310", "body_need_score": round(float(body_need), 4), "drum_need_score": round(float(drum_need), 4), "bass_need_score": round(float(bass_need), 4)}
                clamps.append(f"{name}_raised_to_{floor_label}_by_v63310_effectiveness_floor")
        if roles.get("music_bed") and body_need >= _env_float("BUSY_BAMIX_V63310_BODY_MEDIUM_ENABLE", 0.20, minimum=0.0, maximum=1.0) and max(drum_need, bass_need) >= _env_float("BUSY_BAMIX_V63310_DENSITY_CONTEXT_ENABLE", 0.18, minimum=0.0, maximum=1.0):
            _raise_aug_floor("low_mid_body_fill", "medium", "low_mid_body_fill_effectiveness_floor_for_density_handoff")
        if (roles.get("drums") or roles.get("kick") or roles.get("snare")) and drum_need >= _env_float("BUSY_BAMIX_V63310_TRANSIENT_MEDIUM_ENABLE", 0.55, minimum=0.0, maximum=1.0):
            _raise_aug_floor("transient_ghost", "medium", "transient_ghost_effectiveness_floor_for_persistent_crest_loss")
        if roles.get("bass") and bass_need >= _env_float("BUSY_BAMIX_V63310_BASS_MEDIUM_ENABLE", 0.18, minimum=0.0, maximum=1.0) and drum_need >= _env_float("BUSY_BAMIX_V63310_BASS_DENSITY_CONTEXT_ENABLE", 0.65, minimum=0.0, maximum=1.0):
            _raise_aug_floor("bass_harmonic_translation", "medium", "bass_harmonic_effectiveness_floor_for_small_speaker_translation")

    # v63.7.0: complete Body / Center / Vocal Support routing.  This still does
    # not create a song-specific exception: it raises only role-evidence-backed
    # body/center/vocal modules and leaves artifact/role clamps below in force.
    if _env_on("BUSY_BAMIX_V6370_BODY_CENTER_VOCAL_SUPPORT", "1"):
        def _raise_v6370_floor(name: str, floor_label: str, reason: str, need: float) -> None:
            rec = modules.get(name, {})
            cur_dec = str(rec.get("decision") or "").lower()
            if cur_dec in {"reject", "off", "disable", "bypass"}:
                return
            if _intensity_scalar(rec.get("intensity"), "off") < _intensity_scalar(floor_label, "off"):
                modules[name] = {**rec, "decision": rec.get("decision") or "enable_with_ducking", "intensity": floor_label, "reason": reason, "source": "deterministic_body_center_vocal_floor_v6370", "need_score": round(float(need), 4), "center_body_need_score": round(float(center_body_need), 4), "vocal_support_need_score": round(float(vocal_support_need), 4)}
                clamps.append(f"{name}_raised_to_{floor_label}_by_v6370_body_center_vocal_support")
        if roles.get("music_bed") and max(body_need, center_body_need) >= _env_float("BUSY_BAMIX_V6370_BODY_MEDIUM_ENABLE", 0.18, minimum=0.0, maximum=1.0):
            _raise_v6370_floor("low_mid_body_fill", "medium", "v6370_low_mid_body_deficit_center_owned_fill", max(body_need, center_body_need))
        if roles.get("vocal") and vocal_support_need >= _env_float("BUSY_BAMIX_V6370_VOCAL_MEDIUM_ENABLE", 0.22, minimum=0.0, maximum=1.0):
            _raise_v6370_floor("vocal_support_body_layer", "medium", "v6370_vocal_fundamental_body_support", vocal_support_need)
        if max(center_need, center_body_need) >= _env_float("BUSY_BAMIX_V6370_CENTER_ANCHOR_ENABLE", 0.12, minimum=0.0, maximum=1.0):
            _raise_v6370_floor("center_anchor", "medium" if max(center_need, center_body_need) >= 0.36 else "light", "v6370_center_hollow_low_mid_anchor", max(center_need, center_body_need))

    # v63.8.0: Bass Harmonic Translation final-form routing.  This is a
    # capability floor, not a limiter or DML loudness rescue.  It keeps the bass
    # layer bounded at medium, because the renderer creates 2nd/3rd harmonics,
    # mono anchors the low band, and can downscale/notch against vocal low-mid.
    if _env_on("BUSY_BAMIX_V6380_BASS_HARMONIC_TRANSLATION", "1") and roles.get("bass"):
        cur = modules.get("bass_harmonic_translation", {})
        cur_dec = str(cur.get("decision") or "").lower()
        if cur_dec not in {"reject", "off", "disable", "bypass"} and bass_need >= _env_float("BUSY_BAMIX_V6380_BASS_TRANSLATION_ENABLE", 0.16, minimum=0.0, maximum=1.0):
            floor_label = _intensity_from_need(bass_need, medium_at=0.36, strong_at=0.68, max_label="strong")
            if _intensity_scalar(cur.get("intensity"), "off") < _intensity_scalar(floor_label, "off"):
                modules["bass_harmonic_translation"] = {**cur, "decision": "enable_with_guarded_notch", "intensity": floor_label, "reason": "v6380_input_relative_bass_translation_final_form", "source": "deterministic_bass_harmonic_translation_floor_v6380", "need_score": round(float(bass_need), 4), "vocal_conflict_need_score": round(float(summary.get("v6380_vocal_bass_conflict_need_proxy") or 0.0), 4), "low_side_need_score": round(float(summary.get("bass_low_side_leakage_need_proxy") or 0.0), 4)}
                clamps.append(f"bass_harmonic_translation_raised_to_{floor_label}_by_v6380_final_form")

    # v63.3.2: GPT may reject center_anchor when correlation is high, but a
    # large stereo-minus-mono loudness delta is a separate translation failure.
    # Deterministic mono evidence is allowed to override a GPT reject because the
    # DSP layer is still bounded and peak-guarded downstream.
    mono_need = float(summary.get("center_anchor_need_from_mono_delta") or 0.0)
    mono_delta = summary.get("reference_stereo_minus_mono_lufs_db")
    if mono_need >= _env_float("BUSY_BAMIX_V6332_CENTER_MONO_NEED_ENABLE", 0.20, minimum=0.0, maximum=1.0):
        cur = modules.get("center_anchor", {})
        cur_dec = str(cur.get("decision") or "").lower()
        if cur_dec in {"reject", "defer", "off", "disable", "bypass", ""}:
            modules["center_anchor"] = {
                "decision": "enable",
                "intensity": _intensity_label(cur.get("intensity"), "light") if _intensity_scalar(cur.get("intensity"), "off") > 0 else "light",
                "reason": f"mono_loudness_delta_requires_center_anchor_before_mastering(delta_db={mono_delta})",
                "source": "deterministic_mono_delta_override",
            }
            clamps.append("center_anchor_overrode_gpt_reject_for_mono_loudness_delta")

    # v63.3.3: the previous mono proxy can be clean before mastering while the
    # final loud master still loses mono translation after density completion.
    # When body/bass/drum assist evidence is present, add a very light,
    # band-limited center support anchor even if the reference correlation is
    # already high.  This is not a width correction; it is a survival layer for
    # the downstream limiter/finish chain and is still TP-guarded at render time.
    if _env_on("BUSY_BAMIX_V6333_CENTER_SUPPORT_ANCHOR", "1"):
        cur = modules.get("center_anchor", {})
        cur_dec = str(cur.get("decision") or "").lower()
        if cur_dec in {"reject", "defer", "off", "disable", "bypass", ""}:
            body_need = float(summary.get("low_mid_body_need_proxy") or 0.0)
            bass_need = float(summary.get("bass_harmonic_translation_need_proxy") or 0.0)
            drum_need = float(summary.get("drum_density_need_proxy") or 0.0)
            support_need = max(
                body_need / max(_env_float("BUSY_BAMIX_V6333_CENTER_BODY_NEED_AT", 0.20, minimum=0.01, maximum=1.0), 1e-6),
                bass_need / max(_env_float("BUSY_BAMIX_V6333_CENTER_BASS_NEED_AT", 0.18, minimum=0.01, maximum=1.0), 1e-6),
                drum_need / max(_env_float("BUSY_BAMIX_V6333_CENTER_DRUM_NEED_AT", 0.70, minimum=0.05, maximum=1.0), 1e-6),
            )
            ref_corr_for_support = summary.get("reference_phase_correlation")
            corr_ok = True
            try:
                if ref_corr_for_support is not None:
                    corr_ok = float(ref_corr_for_support) >= _env_float("BUSY_BAMIX_V6333_CENTER_SUPPORT_MIN_CORR", 0.35, minimum=-0.5, maximum=0.95)
            except Exception:
                corr_ok = True
            if support_need >= 1.0 and corr_ok:
                support_intensity = "medium" if support_need >= _env_float("BUSY_BAMIX_V6335_CENTER_SUPPORT_MEDIUM_AT", 1.25, minimum=1.0, maximum=3.0) else "light"
                modules["center_anchor"] = {
                    "decision": "enable",
                    "intensity": support_intensity,
                    "reason": "density_completion_center_support_anchor_for_body_bass_drum_assist",
                    "source": "deterministic_density_support_center_anchor",
                    "support_need": round(float(support_need), 4),
                }
                clamps.append("center_anchor_enabled_for_density_completion_center_support")

    # v63.7.0 executes vocal_support_body_layer as a bounded, peak-guarded,
    # vocal-derived center/fundamental support layer.  The remaining high-risk
    # room/depth module stays deferred until dedicated smear/phase guards exist.
    executable = {"bass_harmonic_translation", "low_mid_body_fill", "center_anchor", "drum_parallel_density", "side_texture_control", "transient_ghost", "vocal_support_body_layer"}
    for name in _augmentation_module_names():
        rec = modules[name]
        if name not in executable:
            if rec.get("decision") not in {"reject", "off"}:
                rec["decision"] = "defer"
                rec["intensity"] = "off"
                rec["reason"] = str(rec.get("reason") or "")[:180] + "; deferred_until_dedicated_v633x_dsp"
                clamps.append(f"{name}_deferred_not_executable_in_v6336")
            continue
        if not _env_on(f"BUSY_BAMIX_V633_AUG_{name.upper()}", "1"):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "disabled_by_env"; clamps.append(f"{name}_disabled_by_env")
        def _module_roles(module_name: str) -> set[str]:
            return {
                "bass_harmonic_translation": {"bass"},
                "low_mid_body_fill": {"music_bed", "music", "unknown"},
                "drum_parallel_density": {"drums", "kick", "snare", "hats"},
                "transient_ghost": {"drums", "kick", "snare", "hats"},
                "vocal_support_body_layer": {"vocal"},
                "center_anchor": {"vocal", "music_bed", "music", "drums", "bass", "unknown"},
                "side_texture_control": {"music_bed", "music", "fx_ambience", "hats", "unknown"},
            }.get(module_name, set())
        rel_roles = _module_roles(name)
        rel_items = [x for x in stem_metrics if str(x.get("role") or "") in rel_roles] if rel_roles else stem_metrics
        module_art = max([float(x.get("artifact_risk") or 0.0) for x in rel_items] or [max_art])
        module_conf = float(np.mean([float(x.get("role_confidence") or 0.0) for x in rel_items])) if rel_items else mean_conf
        if module_art >= _env_float("BUSY_BAMIX_V633_AUG_HIGH_ARTIFACT_AT", 0.88, minimum=0.3, maximum=0.99) and name in {"low_mid_body_fill", "drum_parallel_density", "transient_ghost", "vocal_support_body_layer"}:
            if _intensity_scalar(rec.get("intensity"), "off") > _intensity_scalar("medium"):
                rec["intensity"] = "medium"; clamps.append(f"{name}_reduced_to_medium_for_relevant_artifact_risk")
        if module_art >= _env_float("BUSY_BAMIX_V6336_AUG_SEVERE_RELEVANT_ARTIFACT_AT", 0.97, minimum=0.75, maximum=0.995) and name not in {"center_anchor", "side_texture_control"}:
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "severe_relevant_artifact_risk_blocks_derived_layer"; clamps.append(f"{name}_rejected_severe_relevant_artifact")
        if module_conf < _env_float("BUSY_BAMIX_V633_AUG_LOW_CONF_AT", 0.34, minimum=0.1, maximum=0.9) and name not in {"center_anchor", "side_texture_control"}:
            rec["decision"] = "defer"; rec["intensity"] = "off"; rec["reason"] = "low_relevant_role_confidence_defers_stem_derived_layer"; clamps.append(f"{name}_deferred_low_relevant_role_confidence")
        if name == "bass_harmonic_translation" and not roles.get("bass"):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "missing_bass_stem"; clamps.append("bass_harmonic_translation_rejected_missing_bass")
        if name == "low_mid_body_fill" and not (roles.get("music_bed") or roles.get("unknown")):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "missing_music_bed_stem"; clamps.append("low_mid_body_fill_rejected_missing_music_bed")
        if name == "drum_parallel_density" and not (roles.get("drums") or roles.get("kick") or roles.get("snare")):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "missing_drum_stem"; clamps.append("drum_parallel_density_rejected_missing_drums")
        if name == "transient_ghost" and not (roles.get("drums") or roles.get("kick") or roles.get("snare")):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "missing_drum_stem"; clamps.append("transient_ghost_rejected_missing_drums")
        if name == "vocal_support_body_layer" and not roles.get("vocal"):
            rec["decision"] = "reject"; rec["intensity"] = "off"; rec["reason"] = "missing_vocal_stem"; clamps.append("vocal_support_body_layer_rejected_missing_vocal")
    enabled = {
        k: (k in executable and str(v.get("decision") or "").lower() in {"enable", "enable_with_ducking", "on"} and _intensity_scalar(v.get("intensity"), "off") > 0.0)
        for k, v in modules.items()
    }
    if not _env_on("BUSY_BAMIX_V633_STEM_AUGMENTATION", "1"):
        enabled = {k: False for k in enabled}
        clamps.append("stem_augmentation_disabled_by_env")
    strategy = "fill_center_body_vocal_and_translation" if any(enabled.get(k) for k in ["bass_harmonic_translation", "low_mid_body_fill", "center_anchor", "side_texture_control", "vocal_support_body_layer"]) else "punch_and_density_support" if (enabled.get("drum_parallel_density") or enabled.get("transient_ghost")) else "none"
    return _jsonable({
        "schema_version": _SCHEMA + ".stem_augmentation_plan",
        "active": bool(_env_on("BUSY_BAMIX_V633_STEM_AUGMENTATION", "1")),
        "strategy": strategy,
        "gpt_5_5_embedded_in_existing_mixing_call": bool(isinstance(ai_payload, dict) and ai_payload.get("available")),
        "no_extra_gpt_call": True,
        "problem_summary": summary,
        "modules": modules,
        "modules_enabled": enabled,
        "executable_modules_v6380": sorted(executable),
        "executable_modules_v6370": sorted(executable),
        "executable_modules_v6336": sorted(executable),
        "governance": {
            "deterministic_validator_clamps": clamps,
            "policy": "GPT-5.5 recommends stem-derived augmentation strategy only; deterministic clamp chooses bounded DSP and may reject all layers. No external/generated musical content is allowed.",
        },
    })

def _build_v631_mix_strategy(recipe: str, stem_metrics: list[dict[str, Any]], ai_payload: dict[str, Any] | None = None, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = _recipe_module_defaults(recipe)
    ai_mods = _extract_ai_module_recommendations(ai_payload)
    modules = {**defaults, **ai_mods}
    mean_conf = float(np.mean([float(x.get("role_confidence") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    mean_art = float(np.mean([float(x.get("artifact_risk") or 0.0) for x in stem_metrics])) if stem_metrics else 0.0
    max_art = max([float(x.get("artifact_risk") or 0.0) for x in stem_metrics] or [0.0])
    role_summary = _build_role_summary(stem_metrics)
    v645_residue_map = _v645_stem_neural_codec_residue_map(stem_metrics)
    v645_pressure = _v645_residue_pressure_from_map(v645_residue_map)
    clamps: list[str] = []
    # First clamp GPT-5.5 recommendations to recipe-specific maxima.  The model
    # can choose emphasis, but it cannot turn an artifact-safe/acoustic recipe into
    # dense-pop processing by requesting "strong" for every module.
    for _mod, _max_label in _recipe_module_maxima(recipe).items():
        _before = _intensity_label(modules.get(_mod), defaults.get(_mod, "medium"))
        _after = _clamp_intensity_to_max(_before, _max_label)
        if _after != _before:
            modules[_mod] = _after
            clamps.append(f"{_mod}_clamped_to_recipe_max_{_after}")
    if max_art >= _env_float("BUSY_BAMIX_V631_HIGH_ARTIFACT_AT", 0.72, minimum=0.3, maximum=0.98):
        for k in ["harmonic_density", "drum_punch"]:
            if _intensity_scalar(modules.get(k), "off") > 0.45:
                modules[k] = "light"
                clamps.append(f"{k}_reduced_for_artifact_risk")
        if max_art >= 0.86:
            modules["harmonic_density"] = "off"
            clamps.append("harmonic_density_disabled_for_severe_artifact_risk")
    if mean_conf < _env_float("BUSY_BAMIX_V631_LOW_CONFIDENCE_AT", 0.55, minimum=0.1, maximum=0.9):
        for k in ["vocal_pocket", "kick_bass", "drum_punch", "stereo_safety"]:
            if _intensity_scalar(modules.get(k), "off") > 0.68:
                modules[k] = "medium"
                clamps.append(f"{k}_reduced_for_low_role_confidence")
    if not role_summary.get("vocal"):
        modules["vocal_pocket"] = "off"
        clamps.append("vocal_pocket_disabled_no_vocal_bus")
    if not (role_summary.get("bass") and (role_summary.get("kick") or role_summary.get("drums"))):
        if _env_on("BUSY_BAMIX_V631_REQUIRE_KICK_BASS_ROLES", "0"):
            modules["kick_bass"] = "off"
            clamps.append("kick_bass_disabled_missing_role_pair")
    if not role_summary.get("drums") and not role_summary.get("kick"):
        modules["drum_punch"] = "off"
        clamps.append("drum_punch_disabled_no_drum_bus")
    if _env_on("BUSY_BAMIX_V645_STEM_RESIDUE_MAP", "1") and bool(v645_pressure.get("active")):
        side_pressure = float(v645_pressure.get("effective_side_hf_hash_pressure", v645_pressure.get("side_hf_hash_pressure")) or 0.0)
        residue_pressure = float(v645_pressure.get("residue_pressure") or 0.0)
        current_side = _intensity_scalar(modules.get("side_texture_control"), "off")
        if side_pressure >= _env_float("BUSY_BAMIX_V645_FORCE_SIDE_TEXTURE_AT", 0.22, minimum=0.0, maximum=1.0) and current_side < _intensity_scalar("light"):
            modules["side_texture_control"] = "light"
            clamps.append("v645_stem_residue_witness_enabled_side_texture_control")
        if residue_pressure >= _env_float("BUSY_BAMIX_V645_MEDIUM_SIDE_TEXTURE_AT", 0.42, minimum=0.0, maximum=1.0) and current_side < _intensity_scalar("medium"):
            modules["side_texture_control"] = "medium"
            clamps.append("v645_stem_residue_witness_lifted_side_texture_control_to_medium")
    enabled = {k: (_intensity_scalar(v, "off") > 0.0 and _env_on(f"BUSY_BAMIX_V631_{k.upper()}", "1")) for k, v in modules.items()}
    stem_augmentation_plan = _build_stem_augmentation_plan(recipe, stem_metrics, ai_payload=ai_payload, reference=reference)
    # Master feature flag can switch the entire v63.1 layer to shadow/legacy behavior.
    if not _env_on("BUSY_BAMIX_V631_MODULES", "1"):
        enabled = {k: False for k in enabled}
        clamps.append("v631_modules_disabled_by_env")
    return _jsonable({
        "schema_version": _SCHEMA + ".mix_strategy_consultant_v63_1",
        "active": bool(_env_on("BUSY_BAMIX_V631_MODULES", "1")),
        "recipe": recipe,
        "gpt_5_5_strategy_consultant": bool(isinstance(ai_payload, dict) and ai_payload.get("available")),
        "module_intensity": modules,
        "modules_enabled": enabled,
        "stem_augmentation": stem_augmentation_plan,
        "role_summary": role_summary,
        "governance": {
            "mean_role_confidence": round(mean_conf, 4),
            "mean_artifact_risk": round(mean_art, 4),
            "max_artifact_risk": round(max_art, 4),
            "v645_stem_neural_codec_residue_map": v645_residue_map,
            "v645_residue_pressure": v645_pressure,
            "deterministic_validator_clamps": clamps,
            "policy": "GPT-5.5 may recommend module emphasis and strategy; deterministic validator owns numeric parameters, clamps unsafe intensity, and renderer uses only validated module states.",
        },
        "runtime_contract": {
            "single_recipe": True,
            "single_full_premaster_render": True,
            "correction_rerender_default_disabled": True,
            "virtual_premaster_correction_plan_only": True,
            "no_multi_candidate_buffers": True,
            "block_wise_stateful_dsp": True,
        },
    })


def _module_gain(strategy: dict[str, Any] | None, name: str, default: str = "medium") -> float:
    if not isinstance(strategy, dict):
        return _intensity_scalar(default)
    enabled = strategy.get("modules_enabled") if isinstance(strategy.get("modules_enabled"), dict) else {}
    if enabled and not bool(enabled.get(name, True)):
        return 0.0
    intens = strategy.get("module_intensity") if isinstance(strategy.get("module_intensity"), dict) else {}
    return _intensity_scalar(intens.get(name), default)



def _filter_norm_cutoff(cutoff: Any, sr: int) -> Any:
    nyq = max(float(sr) * 0.5, 1.0)
    if isinstance(cutoff, (list, tuple)):
        vals = [float(x) / nyq for x in cutoff]
        vals = [float(np.clip(x, 1e-5, 0.999)) for x in vals]
        if len(vals) == 2 and vals[0] >= vals[1]:
            vals[0] = max(1e-5, vals[1] * 0.5)
        return vals
    return float(np.clip(float(cutoff) / nyq, 1e-5, 0.999))


def _stateful_sos_filter_1d(x: np.ndarray, state: dict[str, Any], key: str, *, sr: int, btype: str, cutoff: Any, order: int = 2) -> tuple[np.ndarray, dict[str, Any]]:
    """Small stateful IIR helper for block-wise BAMix modules.

    Coefficients and delay state are intentionally kept inside `state` and later
    removed from public telemetry by `_public_state`.  This lets v63.1.2 use real
    band/sidechain processing without allocating full-length sidechain arrays.
    """
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    if arr.size == 0 or butter is None or sosfilt is None or sosfilt_zi is None:
        return arr.astype(np.float32, copy=False), state
    try:
        norm = _filter_norm_cutoff(cutoff, sr)
        cfg = (str(btype), tuple(norm) if isinstance(norm, list) else float(norm), int(order), int(sr))
        cfg_key = f"{key}_cfg"
        sos_key = f"{key}_sos"
        zi_key = f"{key}_zi"
        if state.get(cfg_key) != cfg or sos_key not in state or zi_key not in state:
            sos = butter(int(order), norm, btype=str(btype), output="sos")
            zi = sosfilt_zi(sos) * float(arr[0] if arr.size else 0.0)
            state[cfg_key] = cfg
            state[sos_key] = sos
            state[zi_key] = zi
        y, zi = sosfilt(state[sos_key], arr, zi=state[zi_key])
        state[zi_key] = zi
        return y.astype(np.float32, copy=False), state
    except Exception:
        return arr.astype(np.float32, copy=False), state


def _stateful_sos_filter_stereo(x: np.ndarray, state: dict[str, Any], key: str, *, sr: int, btype: str, cutoff: Any, order: int = 2) -> tuple[np.ndarray, dict[str, Any]]:
    y = _ensure_stereo(x).astype(np.float32, copy=False)
    if y.size == 0:
        return y, state
    left, state = _stateful_sos_filter_1d(y[:, 0], state, f"{key}_l", sr=sr, btype=btype, cutoff=cutoff, order=order)
    right, state = _stateful_sos_filter_1d(y[:, 1], state, f"{key}_r", sr=sr, btype=btype, cutoff=cutoff, order=order)
    return np.stack([left, right], axis=1).astype(np.float32, copy=False), state


def _smooth_need(state: dict[str, Any], key: str, value: float, *, attack: float = 0.35, release: float = 0.10) -> float:
    value = float(np.clip(value, 0.0, 1.0))
    prev = float(state.get(key, 0.0) or 0.0)
    a = float(attack if value > prev else release)
    out = prev * (1.0 - a) + value * a
    state[key] = float(out)
    return float(out)

def _amount_stats(state: dict[str, Any], prefix: str, amount_db: float) -> None:
    try:
        amount = max(0.0, float(amount_db))
    except Exception:
        amount = 0.0
    n_key = f"{prefix}_block_count"
    sum_key = f"{prefix}_sum_db"
    max_key = f"max_{prefix}_db"
    avg_key = f"avg_{prefix}_db"
    active_key = f"{prefix}_active_block_count"
    frac_key = f"{prefix}_active_fraction"
    count = int(state.get(n_key, 0) or 0) + 1
    total = float(state.get(sum_key, 0.0) or 0.0) + amount
    active_count = int(state.get(active_key, 0) or 0) + (1 if amount > 0.05 else 0)
    state[n_key] = count
    state[sum_key] = total
    state[active_key] = active_count
    state[frac_key] = round(active_count / max(count, 1), 4)
    state[max_key] = round(max(float(state.get(max_key, 0.0) or 0.0), amount), 3)
    state[avg_key] = round(total / max(count, 1), 3)



def _need_actual_stats(state: dict[str, Any], prefix: str, need_score: float, actual_db: float, *, applied_threshold_db: float = 0.05) -> None:
    """v63.1.3.2: aggregate need/actual authority telemetry.

    Per-block module decisions are still useful for debugging, but the final block
    can be silence or a fade-out.  These aggregate counters keep need_score and
    actual_amount aligned with the amount summaries shown in debug briefs.
    """
    try:
        need = float(np.clip(float(need_score), 0.0, 1.0))
    except Exception:
        need = 0.0
    try:
        actual = max(0.0, float(actual_db))
    except Exception:
        actual = 0.0
    n_key = f"{prefix}_need_block_count"
    need_sum_key = f"{prefix}_need_sum"
    actual_sum_key = f"{prefix}_actual_sum_db"
    applied_key = f"{prefix}_actual_applied_block_count"
    count = int(state.get(n_key, 0) or 0) + 1
    need_sum = float(state.get(need_sum_key, 0.0) or 0.0) + need
    actual_sum = float(state.get(actual_sum_key, 0.0) or 0.0) + actual
    applied_count = int(state.get(applied_key, 0) or 0) + (1 if actual > float(applied_threshold_db) else 0)
    state[n_key] = count
    state[need_sum_key] = need_sum
    state[actual_sum_key] = actual_sum
    state[applied_key] = applied_count
    state["need_score_avg"] = round(need_sum / max(count, 1), 4)
    state["need_score_max"] = round(max(float(state.get("need_score_max", 0.0) or 0.0), need), 4)
    state["actual_amount_avg_db"] = round(actual_sum / max(count, 1), 3)
    state["actual_amount_max_db"] = round(max(float(state.get("actual_amount_max_db", 0.0) or 0.0), actual), 3)
    state["applied_actual"] = bool(applied_count > 0)
    state["actual_applied_fraction"] = round(applied_count / max(count, 1), 4)
    if need <= 0.05 and actual <= float(applied_threshold_db):
        state["aggregate_decision"] = "aggregate_bypass_need_low"
    elif need > 0.20 and actual <= float(applied_threshold_db):
        state["aggregate_decision"] = "aggregate_need_present_but_actual_minimal"
    else:
        state["aggregate_decision"] = "aggregate_need_actual_aligned"


def _soft_tanh_block(x: np.ndarray, *, drive_db: float, wet: float, oversample: int, state: dict[str, Any], label: str) -> tuple[np.ndarray, dict[str, Any]]:
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0 or drive_db <= 0.0 or wet <= 0.0:
        return arr, state
    drive = _amp(float(drive_db))
    denom = max(float(np.tanh(drive)), 1e-6)
    os_factor = int(np.clip(int(oversample or 1), 1, 4))
    used_os = 1
    proc = arr
    try:
        if os_factor > 1 and resample_poly is not None and _env_on(f"BUSY_BAMIX_V6312_{label.upper()}_OVERSAMPLE", "1"):
            up = resample_poly(arr, os_factor, 1, axis=0).astype(np.float32, copy=False)
            soft = np.tanh(up * drive) / denom
            down = resample_poly(soft, 1, os_factor, axis=0).astype(np.float32, copy=False)
            if down.shape[0] < arr.shape[0]:
                pad = np.zeros_like(arr)
                pad[:down.shape[0]] = down
                down = pad
            proc = down[:arr.shape[0]]
            used_os = os_factor
        else:
            proc = (np.tanh(arr * drive) / denom).astype(np.float32, copy=False)
    except Exception:
        proc = (np.tanh(arr * drive) / denom).astype(np.float32, copy=False)
        used_os = 1
    out = (arr * (1.0 - wet) + proc * wet).astype(np.float32, copy=False)
    state.update({"oversample_factor": int(used_os), "curve": "normalized_tanh", "oversampling_policy": "2x/4x when scipy.resample_poly is available; base-rate fallback otherwise"})
    return out, state

def _low_band_scalar_duck(bass: np.ndarray, trigger: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.1.2: true low-band kick/bass ducking, not a broadband scalar proxy.

    The amount is still need-aware: GPT/validator intensity defines capacity,
    while block evidence (kick activity + bass low-band activity) decides how
    much is actually applied.  If there is no measurable collision, the duck can
    legitimately remain near zero.
    """
    if intensity <= 0.0 or bass.size == 0 or trigger.size == 0:
        return bass, state
    bass = _ensure_stereo(bass).astype(np.float32, copy=False)
    trigger = _ensure_stereo(trigger).astype(np.float32, copy=False)
    r = str(state.get("recipe", "") or "")
    crossover = _env_float("BUSY_BAMIX_V6312_KICK_BASS_CROSSOVER_HZ", 105.0, minimum=55.0, maximum=180.0)
    bass_low, state = _stateful_sos_filter_stereo(bass, state, "kick_bass_bass_low", sr=sr, btype="lowpass", cutoff=crossover, order=2)
    bass_high = (bass - bass_low).astype(np.float32, copy=False)
    trigger_low, state = _stateful_sos_filter_stereo(trigger, state, "kick_bass_trigger_low", sr=sr, btype="lowpass", cutoff=max(crossover, 90.0), order=2)
    trig_rms = _rms(trigger_low)
    bass_low_rms = _rms(bass_low)
    bass_full_rms = _rms(bass)
    if trig_rms < 1e-7 or bass_low_rms < 1e-8:
        _amount_stats(state, "duck", 0.0)
        _need_actual_stats(state, "kick_bass", 0.0, 0.0)
        state.update({"active": True, "last_duck_db": 0.0, "need_score": 0.0, "decision": "minimal_action_because_trigger_or_bass_low_absent", "method": "true_low_band_multiband_sidechain_duck_v6312"})
        return bass, state
    trig_ref = max(float(state.get("trigger_ref", 0.0) or 0.0) * 0.985, trig_rms)
    bass_ref = max(float(state.get("bass_low_ref", 0.0) or 0.0) * 0.985, bass_low_rms)
    state["trigger_ref"] = trig_ref
    state["bass_low_ref"] = bass_ref
    trigger_activity = float(np.clip(trig_rms / max(trig_ref, 1e-9), 0.0, 1.0))
    bass_activity = float(np.clip(bass_low_rms / max(bass_ref, 1e-9), 0.0, 1.0))
    low_share = float(np.clip(bass_low_rms / max(bass_full_rms, 1e-9), 0.0, 1.0))
    # Product keeps it quiet when either side is not materially active.
    raw_need = float(np.clip((trigger_activity * bass_activity) * (0.45 + 0.75 * low_share), 0.0, 1.0))
    need = _smooth_need(state, "duck_need_env", raw_need, attack=0.50, release=0.18)
    cap_db = _env_float("BUSY_BAMIX_V631_KICK_BASS_MAX_DUCK_DB", 4.0, minimum=0.0, maximum=6.0) * float(intensity)
    duck_db = float(np.clip(cap_db * need, 0.0, cap_db))
    gain = _amp(-duck_db)
    out = (bass_high + bass_low * gain).astype(np.float32, copy=False)
    _amount_stats(state, "duck", duck_db)
    _need_actual_stats(state, "kick_bass", need, duck_db)
    state.update({
        "active": True,
        "last_duck_db": round(duck_db, 3),
        "capacity_db": round(float(cap_db), 3),
        "need_score": round(float(need), 4),
        "trigger_activity": round(trigger_activity, 4),
        "bass_low_activity": round(bass_activity, 4),
        "bass_low_share": round(low_share, 4),
        "crossover_hz": round(float(crossover), 2),
        "decision": "need_aware_low_band_duck" if duck_db > 0.05 else "minimal_action_because_low_overlap",
        "method": "true_low_band_multiband_sidechain_duck_v6312",
    })
    return out, state


def _vocal_pocket_bed(bed: np.ndarray, vocal: np.ndarray, *, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.1.2: two-band vocal-triggered dynamic EQ on the bed.

    This replaces the v63.1 broadband placeholder.  GPT/validator intensity is
    capacity only; measured vocal activity and bed/vocal band masking decide the
    actual cut.  Therefore near-zero cuts are valid when masking evidence is low.
    """
    if intensity <= 0.0 or bed.size == 0 or vocal.size == 0:
        return bed, state
    bed = _ensure_stereo(bed).astype(np.float32, copy=False)
    vocal = _ensure_stereo(vocal).astype(np.float32, copy=False)
    vocal_r = _rms(vocal)
    bed_r = _rms(bed)
    if vocal_r < 1e-7 or bed_r < 1e-8:
        _amount_stats(state, "pocket_cut", 0.0)
        _need_actual_stats(state, "vocal_pocket", 0.0, 0.0)
        state.update({"active": True, "last_cut_db": 0.0, "need_score": 0.0, "decision": "minimal_action_because_vocal_or_bed_absent", "method": "two_band_sidechain_dynamic_eq_v6312"})
        return bed, state
    # Band-limited analysis and processing: mud and presence are enough for v63.1.2.
    bed_mud, state = _stateful_sos_filter_stereo(bed, state, "vp_bed_mud", sr=TARGET_SR, btype="bandpass", cutoff=(250.0, 500.0), order=2)
    bed_pres, state = _stateful_sos_filter_stereo(bed, state, "vp_bed_presence", sr=TARGET_SR, btype="bandpass", cutoff=(1200.0, 4200.0), order=2)
    vocal_mono = np.mean(vocal, axis=1).astype(np.float32, copy=False)
    vocal_mud, state = _stateful_sos_filter_1d(vocal_mono, state, "vp_vocal_mud_detector", sr=TARGET_SR, btype="bandpass", cutoff=(250.0, 500.0), order=2)
    vocal_pres, state = _stateful_sos_filter_1d(vocal_mono, state, "vp_vocal_presence_detector", sr=TARGET_SR, btype="bandpass", cutoff=(1200.0, 4200.0), order=2)
    v_ref = max(float(state.get("vocal_ref", 0.0) or 0.0) * 0.985, vocal_r)
    state["vocal_ref"] = v_ref
    activity = float(np.clip(vocal_r / max(v_ref, 1e-9), 0.0, 1.0))
    mud_mask = float(np.clip(_rms(bed_mud) / max(_rms(vocal_mud) * 1.20 + 1e-9, 1e-9) * 0.55, 0.0, 1.0))
    pres_mask = float(np.clip(_rms(bed_pres) / max(_rms(vocal_pres) * 1.10 + 1e-9, 1e-9) * 0.65, 0.0, 1.0))
    mud_need = _smooth_need(state, "mud_need_env", activity * mud_mask, attack=0.45, release=0.16)
    pres_need = _smooth_need(state, "presence_need_env", activity * pres_mask, attack=0.45, release=0.16)
    mud_cap = _env_float("BUSY_BAMIX_V6312_VOCAL_POCKET_MUD_MAX_CUT_DB", 2.4, minimum=0.0, maximum=5.0) * float(intensity)
    pres_cap = _env_float("BUSY_BAMIX_V6312_VOCAL_POCKET_PRESENCE_MAX_CUT_DB", 3.0, minimum=0.0, maximum=5.0) * float(intensity)
    mud_cut = float(np.clip(mud_cap * mud_need, 0.0, mud_cap))
    pres_cut = float(np.clip(pres_cap * pres_need, 0.0, pres_cap))
    out = bed.copy()
    if mud_cut > 1e-5:
        out += bed_mud * (_amp(-mud_cut) - 1.0)
    if pres_cut > 1e-5:
        out += bed_pres * (_amp(-pres_cut) - 1.0)
    out = out.astype(np.float32, copy=False)
    mean_cut = max(mud_cut, pres_cut)
    _amount_stats(state, "pocket_cut", mean_cut)
    _need_actual_stats(state, "vocal_pocket", max(mud_need, pres_need), mean_cut)
    state.update({
        "active": True,
        "last_cut_db": round(float(mean_cut), 3),
        "mud_cut_db": round(float(mud_cut), 3),
        "presence_cut_db": round(float(pres_cut), 3),
        "need_score": round(float(max(mud_need, pres_need)), 4),
        "vocal_activity": round(activity, 4),
        "mud_masking_score": round(float(mud_mask), 4),
        "presence_masking_score": round(float(pres_mask), 4),
        "decision": "need_aware_two_band_pocket" if mean_cut > 0.05 else "minimal_action_because_masking_low",
        "method": "two_band_sidechain_dynamic_eq_v6312",
    })
    return out, state


def _drum_soft_peak_rounding(drums: np.ndarray, *, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or drums.size == 0:
        return drums, state
    drum_rms = _rms(drums)
    if drum_rms < 1e-8:
        _amount_stats(state, "rounding_drive", 0.0)
        _need_actual_stats(state, "drum_punch", 0.0, 0.0)
        state.update({"active": True, "applied": False, "need_score": 0.0, "decision": "minimal_action_because_drum_bus_silent", "method": "oversampled_soft_peak_rounding_v6312"})
        return drums, state
    peak_db = _db(_peak(drums))
    crest = _db(_peak(drums) / max(drum_rms, 1e-9))
    # Need-aware: only round when there is transient-heavy material or true-peak pressure.
    transient_need = float(np.clip((crest - 11.0) / 7.0, 0.0, 1.0))
    peak_need = float(np.clip((peak_db + 6.0) / 6.0, 0.0, 1.0))
    need = _smooth_need(state, "rounding_need_env", max(transient_need, peak_need * 0.6), attack=0.45, release=0.18)
    if need <= 0.02:
        _amount_stats(state, "rounding_drive", 0.0)
        _need_actual_stats(state, "drum_punch", need, 0.0)
        state.update({"active": True, "applied": False, "need_score": round(need, 4), "potential_drive_db": 0.0, "decision": "minimal_action_because_drum_transient_pressure_low", "method": "oversampled_soft_peak_rounding_v6312"})
        return drums, state
    drive_db = _env_float("BUSY_BAMIX_V631_DRUM_ROUNDING_DRIVE_DB", 1.35, minimum=0.0, maximum=4.0) * float(intensity) * (0.55 + 0.45 * need)
    wet = _env_float("BUSY_BAMIX_V631_DRUM_ROUNDING_WET", 0.24, minimum=0.0, maximum=0.8) * (0.45 + 0.55 * need)
    os_factor = _env_int("BUSY_BAMIX_V631_DRUM_ROUNDING_OS", 2, minimum=1, maximum=4)
    out, state = _soft_tanh_block(drums, drive_db=drive_db, wet=wet, oversample=os_factor, state=state, label="drum_rounding")
    _amount_stats(state, "rounding_drive", drive_db)
    _need_actual_stats(state, "drum_punch", need, drive_db)
    state.update({"active": True, "applied": True, "drive_db": round(drive_db, 3), "wet": round(wet, 3), "need_score": round(need, 4), "crest_db": round(crest, 3), "peak_dbfs": round(peak_db, 3), "decision": "need_aware_drum_soft_peak_rounding", "method": "oversampled_soft_peak_rounding_v6312"})
    return out.astype(np.float32, copy=False), state


def _harmonic_density(mix: np.ndarray, *, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or mix.size == 0:
        return mix, state
    mix_rms = _rms(mix)
    if mix_rms < 1e-8:
        _amount_stats(state, "density_drive", 0.0)
        _need_actual_stats(state, "harmonic_density", 0.0, 0.0)
        state.update({"active": True, "applied": False, "need_score": 0.0, "decision": "minimal_action_because_mixbus_silent", "method": "oversampled_mixbus_harmonic_density_v6312"})
        return mix, state
    crest = _db(_peak(mix) / max(mix_rms, 1e-9))
    density_need = float(np.clip((crest - 12.0) / 6.0, 0.0, 1.0))
    need = _smooth_need(state, "density_need_env", density_need, attack=0.35, release=0.12)
    if need <= 0.025:
        _amount_stats(state, "density_drive", 0.0)
        _need_actual_stats(state, "harmonic_density", need, 0.0)
        state.update({"active": True, "applied": False, "need_score": round(need, 4), "decision": "minimal_action_because_density_already_sufficient", "method": "oversampled_mixbus_harmonic_density_v6312"})
        return mix, state
    drive_db = _env_float("BUSY_BAMIX_V631_HARMONIC_DENSITY_DRIVE_DB", 0.95, minimum=0.0, maximum=3.0) * float(intensity) * (0.50 + 0.50 * need)
    wet = _env_float("BUSY_BAMIX_V631_HARMONIC_DENSITY_WET", 0.18, minimum=0.0, maximum=0.7) * (0.50 + 0.50 * need)
    os_factor = _env_int("BUSY_BAMIX_V631_HARMONIC_DENSITY_OS", 2, minimum=1, maximum=4)
    out, state = _soft_tanh_block(mix, drive_db=drive_db, wet=wet, oversample=os_factor, state=state, label="harmonic_density")
    _amount_stats(state, "density_drive", drive_db)
    _need_actual_stats(state, "harmonic_density", need, drive_db)
    state.update({"active": True, "applied": True, "drive_db": round(drive_db, 3), "wet": round(wet, 3), "need_score": round(need, 4), "crest_db": round(crest, 3), "decision": "need_aware_mixbus_harmonic_density", "method": "oversampled_mixbus_harmonic_density_v6312"})
    return out.astype(np.float32, copy=False), state


def _elliptical_stereo_safety(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any], recipe: str) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or mix.size == 0:
        return mix, state
    y = _ensure_stereo(mix)
    mid = (y[:, 0] + y[:, 1]) * 0.5
    side = (y[:, 0] - y[:, 1]) * 0.5
    r = str(recipe or "").lower()
    default_cross = 80.0 if "acoustic" in r else 120.0 if ("dense" in r or "club" in r or "bass" in r) else 100.0
    crossover = _env_float("BUSY_BAMIX_V631_ELLIPTICAL_CROSSOVER_HZ", default_cross, minimum=50.0, maximum=180.0)
    reduction_db = _env_float("BUSY_BAMIX_V631_LOW_SIDE_REDUCTION_DB", 9.0, minimum=0.0, maximum=24.0) * float(intensity)
    if butter is not None and sosfilt is not None and sosfilt_zi is not None:
        key = "elliptical_side_hpf_sos"
        if key not in state:
            sos = butter(2, crossover / (float(sr) * 0.5), btype="highpass", output="sos")
            zi = sosfilt_zi(sos)
            state[key] = sos
            state["elliptical_side_hpf_zi_l"] = zi * float(side[0] if side.size else 0.0)
        sos = state[key]
        zi = state.get("elliptical_side_hpf_zi_l")
        side_high, zi = sosfilt(sos, side.astype(np.float32, copy=False), zi=zi)
        state["elliptical_side_hpf_zi_l"] = zi
        side_low = side - side_high
        side = side_high + side_low * _amp(-reduction_db)
        method = "stateful_side_highpass_low_side_attenuation"
    else:
        side *= _amp(-min(reduction_db, 3.0))
        method = "fallback_global_side_attenuation"
    out = np.stack([mid + side, mid - side], axis=1).astype(np.float32, copy=False)
    state.update({"active": True, "crossover_hz": round(float(crossover), 2), "side_low_reduction_db": round(float(reduction_db), 3), "method": method})
    return out, state



def _glue_compressor_block(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any], recipe: str) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.1.3: need/GR-authority aligned feedforward glue compressor.

    v63.1.2.1 proved that glue telemetry was no longer a fixed-GR proxy, but the
    need score and actual gain reduction could still look contradictory.  This
    version makes the authority explicit: if need is low, bypass is correct; if
    need is high but the detector sits below threshold, the threshold is lowered
    once analytically inside the same block.  This is not a new render/candidate.
    """
    if intensity <= 0.0 or mix.size == 0:
        return mix, state
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    peak = _peak(y)
    rms = _rms(y)
    if peak <= 1e-8 or rms <= 1e-10:
        _amount_stats(state, "glue_gr", 0.0)
        _need_actual_stats(state, "glue", 0.0, 0.0)
        state.update({"active": True, "applied": False, "need_score": 0.0, "decision": "minimal_action_because_mixbus_silent", "method": "need_gr_authority_aligned_feedforward_soft_knee_glue_v6313"})
        return y, state

    r = str(recipe or "").lower()
    ratio = _env_float("BUSY_BAMIX_V631_GLUE_RATIO", 1.65 if "punch" in r else 1.85 if ("dense" in r or "club" in r) else 1.55, minimum=1.1, maximum=2.8)
    attack_ms = _env_float("BUSY_BAMIX_V631_GLUE_ATTACK_MS", 45.0 if "punch" in r else 22.0 if ("dense" in r or "club" in r) else 32.0, minimum=8.0, maximum=80.0)
    release_ms = _env_float("BUSY_BAMIX_V631_GLUE_RELEASE_MS", 165.0 if "punch" in r else 210.0, minimum=45.0, maximum=500.0)
    knee = _env_float("BUSY_BAMIX_V6312_1_GLUE_KNEE_DB", 6.0, minimum=2.0, maximum=12.0)
    max_gr_cap = _env_float("BUSY_BAMIX_V631_GLUE_MAX_GR_DB", 2.5, minimum=0.0, maximum=4.0) * float(intensity)

    peak_db = _db(peak)
    rms_db = _db(rms)
    crest = _db(peak / max(rms, 1e-9)) if peak > 0 and rms > 0 else 0.0
    crest_target = 13.8 if "punch" in r or "acoustic" in r or "cinematic" in r else 12.2 if ("dense" in r or "club" in r) else 12.8
    crest_need = float(np.clip((crest - crest_target) / 5.0, 0.0, 1.0))
    peak_need = float(np.clip((peak_db + 4.0) / 5.0, 0.0, 1.0))
    peak_crest_gate = float(np.clip((crest - max(crest_target - 1.0, 10.0)) / 4.0, 0.0, 1.0))
    density_need = float(np.clip((rms_db - (-16.0)) / 8.0, 0.0, 1.0)) if ("dense" in r or "club" in r) else 0.0
    raw_need = max(crest_need, peak_need * peak_crest_gate * 0.55, density_need * 0.35)
    need = _smooth_need(state, "glue_need_env", raw_need, attack=0.40, release=0.16)
    max_gr = float(max_gr_cap * (0.35 + 0.65 * need))
    min_need = _env_float("BUSY_BAMIX_V6313_GLUE_MIN_NEED", _env_float("BUSY_BAMIX_V6312_1_GLUE_MIN_NEED", 0.035, minimum=0.0, maximum=0.5), minimum=0.0, maximum=0.5)
    if need <= min_need or max_gr <= 0.01:
        _amount_stats(state, "glue_gr", 0.0)
        state.update({
            "active": True, "applied": False,
            "ratio": round(float(ratio), 3), "attack_ms": round(float(attack_ms), 2), "release_ms": round(float(release_ms), 2),
            "need_score": round(float(need), 4), "raw_need_score": round(float(raw_need), 4),
            "crest_need": round(float(crest_need), 4), "peak_need": round(float(peak_need), 4), "density_need": round(float(density_need), 4),
            "crest_db": round(float(crest), 3), "peak_dbfs": round(float(peak_db), 3), "rms_dbfs": round(float(rms_db), 3),
            "peak_crest_gate": round(float(peak_crest_gate), 4),
            "decision": "minimal_action_because_glue_need_low",
            "method": "need_gr_authority_aligned_feedforward_soft_knee_glue_v6313",
        })
        return y, state

    mono = np.mean(y, axis=1).astype(np.float32, copy=False)
    sc_hz = 100.0 if "punch" in r else 85.0
    detector, state = _stateful_sos_filter_1d(mono, state, "glue_sidechain_hpf", sr=sr, btype="highpass", cutoff=_env_float("BUSY_BAMIX_V6312_GLUE_SC_HPF_HZ", sc_hz, minimum=40.0, maximum=180.0), order=2)
    xdb = 20.0 * np.log10(np.maximum(np.abs(detector), 1e-8))
    if xdb.size == 0:
        return y, state
    p85 = float(np.percentile(xdb, 85.0)); p90 = float(np.percentile(xdb, 90.0)); p95 = float(np.percentile(xdb, 95.0))
    base_pct = p95 if "punch" in r else p90
    need_offset = (1.25 + 2.75 * need) if "punch" in r else (1.75 + 3.00 * need)
    threshold = float(np.clip(base_pct - need_offset, -72.0, -1.0))
    env_start = float(state.get("glue_env_db", base_pct) if math.isfinite(float(state.get("glue_env_db", base_pct))) else base_pct)
    env_start = max(env_start, p85 - 12.0)
    alpha_a = math.exp(-math.log(9.0) / (float(sr) * max(attack_ms / 1000.0, 1e-5)))
    alpha_r = math.exp(-math.log(9.0) / (float(sr) * max(release_ms / 1000.0, 1e-5)))

    def _compute(thr: float, env0: float) -> tuple[np.ndarray, float]:
        env = float(env0)
        gr = np.zeros_like(xdb, dtype=np.float32)
        for i, x in enumerate(xdb):
            if x > env:
                env = alpha_a * env + (1.0 - alpha_a) * float(x)
            else:
                env = alpha_r * env + (1.0 - alpha_r) * float(x)
            over = env - float(thr)
            if over <= -knee * 0.5:
                cur = 0.0
            elif abs(over) <= knee * 0.5:
                cur = (1.0 / ratio - 1.0) * ((over + knee * 0.5) ** 2) / (2.0 * knee)
            else:
                cur = (1.0 / ratio - 1.0) * over
            gr[i] = float(np.clip(cur, -max_gr, 0.0))
        return gr, env

    gr, env = _compute(threshold, env_start)
    gr_abs_avg = abs(float(np.mean(gr)) if gr.size else 0.0)
    gr_abs_max = abs(float(np.min(gr)) if gr.size else 0.0)
    desired_max_gr = min(float(max_gr), _env_float("BUSY_BAMIX_V6313_GLUE_DESIRED_MAX_GR_DB", 0.20 + 0.65 * float(need), minimum=0.0, maximum=2.5))
    authority_shift = 0.0
    if need >= _env_float("BUSY_BAMIX_V6313_GLUE_AUTHORITY_NEED_AT", 0.45, minimum=0.0, maximum=1.0) and gr_abs_max < desired_max_gr and max_gr > 0.05:
        authority_shift = float(np.clip((desired_max_gr - gr_abs_max) * 2.75 + (need - 0.45) * 2.0, 0.0, _env_float("BUSY_BAMIX_V6313_GLUE_MAX_AUTHORITY_SHIFT_DB", 5.5, minimum=0.0, maximum=12.0)))
        if authority_shift > 0.05:
            gr2, env2 = _compute(threshold - authority_shift, env_start)
            gr2_abs_max = abs(float(np.min(gr2)) if gr2.size else 0.0)
            # Accept the authority assist only when it materially improves need/actual alignment.
            if gr2_abs_max > gr_abs_max + 0.03:
                gr, env = gr2, env2
                threshold = threshold - authority_shift
                gr_abs_avg = abs(float(np.mean(gr)) if gr.size else 0.0)
                gr_abs_max = gr2_abs_max
            else:
                authority_shift = 0.0

    state["glue_env_db"] = float(env)
    gain = np.power(10.0, gr / 20.0).astype(np.float32)
    out = (y * gain[:, None]).astype(np.float32, copy=False)

    n = int(gr.size)
    _amount_stats(state, "glue_gr", gr_abs_avg)
    _need_actual_stats(state, "glue", need, gr_abs_avg)
    prev_n = int(state.get("sample_count", 0) or 0)
    prev_sum = float(state.get("gr_sum_db", 0.0) or 0.0)
    prev_active = float(state.get("gr_active_samples", 0.0) or 0.0)
    state["sample_count"] = prev_n + n
    state["gr_sum_db"] = prev_sum + float(np.sum(gr))
    state["gr_active_samples"] = prev_active + float(np.sum(gr < -0.05))
    state["gr_min_db"] = min(float(state.get("gr_min_db", 0.0) or 0.0), float(np.min(gr)) if n else 0.0)
    running_n = max(int(state.get("sample_count", 0) or 0), 1)
    authority_need_at = _env_float("BUSY_BAMIX_V6313_GLUE_AUTHORITY_NEED_AT", 0.45, minimum=0.0, maximum=1.0)
    gr_actual_alignment = (
        "bypass_need_low" if need <= min_need
        else "low_to_moderate_need_minimal_gr_ok" if need < authority_need_at and gr_abs_max < max(0.05, desired_max_gr * 0.45)
        else "aligned" if gr_abs_max >= max(0.05, desired_max_gr * 0.45)
        else "need_high_but_detector_below_threshold"
    )
    state.update({
        "active": True, "applied": bool(gr_abs_max > 0.01),
        "ratio": round(float(ratio), 3), "attack_ms": round(float(attack_ms), 2), "release_ms": round(float(release_ms), 2),
        "threshold_db": round(float(threshold), 3),
        "detector_p85_db": round(float(p85), 3), "detector_p90_db": round(float(p90), 3), "detector_p95_db": round(float(p95), 3),
        "need_score": round(float(need), 4), "raw_need_score": round(float(raw_need), 4),
        "crest_need": round(float(crest_need), 4), "peak_need": round(float(peak_need), 4), "density_need": round(float(density_need), 4),
        "desired_max_gr_db": round(float(desired_max_gr), 3),
        "authority_shift_db": round(float(authority_shift), 3),
        "need_actual_alignment": gr_actual_alignment,
        "crest_db": round(float(crest), 3), "peak_dbfs": round(float(peak_db), 3), "rms_dbfs": round(float(rms_db), 3),
        "peak_crest_gate": round(float(peak_crest_gate), 4),
        "block_avg_gr_db": round(float(np.mean(gr)) if n else 0.0, 3),
        "block_max_gr_db": round(float(np.min(gr)) if n else 0.0, 3),
        "avg_gr_db": round(float(state.get("gr_sum_db", 0.0)) / float(running_n), 3),
        "max_gr_db": round(float(state.get("gr_min_db", 0.0)), 3),
        "avg_gr_abs_db": round(abs(float(state.get("gr_sum_db", 0.0)) / float(running_n)), 3),
        "max_gr_abs_db": round(abs(float(state.get("gr_min_db", 0.0))), 3),
        "active_fraction": round(float(state.get("gr_active_samples", 0.0)) / float(running_n), 4),
        "decision": "need_aware_variable_glue_authority_aligned" if gr_abs_max > 0.05 else "minimal_action_because_detector_below_threshold",
        "method": "need_gr_authority_aligned_feedforward_soft_knee_glue_v6313",
    })
    return out, state

def _stereo_depth_safety(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any], recipe: str) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or mix.size == 0:
        return mix, state
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    corr_pre = _corr(y)
    mid = (y[:, 0] + y[:, 1]) * 0.5
    side = (y[:, 0] - y[:, 1]) * 0.5
    r = str(recipe or "").lower()
    wide_recipe = any(k in r for k in ["wide", "cinematic", "dense"])
    high_side, state = _stateful_sos_filter_1d(side, state, "stereo_side_high", sr=sr, btype="highpass", cutoff=160.0, order=2)
    low_side = side - high_side
    side_gain_db = 0.0
    center_lift_db = 0.0
    full_side_anchor_trim_db = 0.0
    decision = "neutral_center_lock"
    mid_rms = _rms(mid)
    side_rms = _rms(side)
    side_mid_db = _db(side_rms / max(mid_rms, 1e-9)) if mid_rms > 0.0 and side_rms > 0.0 else -120.0
    if corr_pre < 0.20:
        side_gain_db = -min(3.0 * float(intensity), 3.0)
        decision = "side_reduced_for_phase_safety"
    elif wide_recipe and corr_pre > 0.45:
        side_gain_db = _env_float("BUSY_BAMIX_V6312_WIDTH_SIDE_GAIN_DB", 1.15, minimum=0.0, maximum=2.5) * float(intensity)
        decision = "safe_high_side_width"
    # v63.2: commercial premaster translation anchor.  The later mastering mono
    # guard often has no TP/floor room after the final limiter; keep a modest
    # center anchor here while there is still bus-level headroom.
    anchor_active = _env_on("BUSY_BAMIX_V632_TRANSLATION_CENTER_ANCHOR", "1") and not any(k in r for k in ["cinematic", "ambient"])
    anchor_threshold = _env_float("BUSY_BAMIX_V632_SIDE_MID_ANCHOR_THRESHOLD_DB", -13.5 if wide_recipe else -15.0, minimum=-30.0, maximum=-3.0)
    anchor_need = float(np.clip((side_mid_db - anchor_threshold) / 8.0, 0.0, 1.0))
    if anchor_active and anchor_need > 0.02:
        side_anchor_trim = _env_float("BUSY_BAMIX_V632_CENTER_ANCHOR_SIDE_TRIM_DB", 1.10, minimum=0.0, maximum=3.5) * float(intensity) * (0.35 + 0.65 * anchor_need)
        center_lift_db = _env_float("BUSY_BAMIX_V632_CENTER_ANCHOR_MID_LIFT_DB", 0.38, minimum=0.0, maximum=1.5) * float(intensity) * anchor_need
        side_gain_db -= float(side_anchor_trim)
        extreme_side = side_mid_db > _env_float("BUSY_BAMIX_V6322_FULL_SIDE_ANCHOR_THRESHOLD_DB", -3.0, minimum=-18.0, maximum=9.0) or corr_pre < _env_float("BUSY_BAMIX_V6322_FULL_SIDE_ANCHOR_CORR_THRESHOLD", 0.18, minimum=-0.8, maximum=0.9)
        if extreme_side and not wide_recipe:
            full_side_anchor_trim_db = -_env_float("BUSY_BAMIX_V6322_FULL_SIDE_ANCHOR_TRIM_DB", 0.75, minimum=0.0, maximum=2.5) * float(intensity) * anchor_need
        decision = "translation_center_anchor_with_width_guard" if decision == "neutral_center_lock" else decision + "+translation_center_anchor"
    high_side = high_side * _amp(side_gain_db)
    mid_out = mid * _amp(center_lift_db)
    out_side = (low_side + high_side) * _amp(full_side_anchor_trim_db)
    out = np.stack([mid_out + out_side, mid_out - out_side], axis=1).astype(np.float32, copy=False)
    # Preserve block peak roughly; the anchor changes balance, not loudness target.
    pk_in = _peak(y); pk_out = _peak(out)
    if pk_in > 1e-8 and pk_out > pk_in * 1.015:
        out *= np.float32(pk_in / max(pk_out, 1e-8))
    corr_post = _corr(out)
    state.update({"active": True, "side_gain_db": round(float(side_gain_db), 3), "full_side_anchor_trim_db": round(float(full_side_anchor_trim_db), 3), "center_lift_db": round(float(center_lift_db), 3), "side_mid_db": round(float(side_mid_db), 3), "anchor_need": round(float(anchor_need), 4), "correlation_pre": round(float(corr_pre), 4), "correlation_post": round(float(corr_post), 4), "decision": decision, "method": "high_side_width_with_phase_guard_plus_v6322_translation_center_anchor_full_side_trim"})
    return out, state

def _translation_qc_update(mix: np.ndarray, state: dict[str, Any]) -> dict[str, Any]:
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    if y.size == 0:
        state["active"] = False
        return state
    l = y[:, 0].astype(np.float64, copy=False)
    r = y[:, 1].astype(np.float64, copy=False)
    mono = (l + r) * 0.5
    state["active"] = True
    state["sum_l2"] = float(state.get("sum_l2", 0.0) or 0.0) + float(np.sum(l * l))
    state["sum_r2"] = float(state.get("sum_r2", 0.0) or 0.0) + float(np.sum(r * r))
    state["sum_lr"] = float(state.get("sum_lr", 0.0) or 0.0) + float(np.sum(l * r))
    state["sample_count"] = int(state.get("sample_count", 0) or 0) + int(y.shape[0])
    state["mono_peak"] = max(float(state.get("mono_peak", 0.0) or 0.0), float(np.max(np.abs(mono))) if mono.size else 0.0)
    state["stereo_peak"] = max(float(state.get("stereo_peak", 0.0) or 0.0), _peak(y))
    return state


def _translation_qc_finalize(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict) or not state.get("active"):
        return {"active": False}
    den = math.sqrt(max(float(state.get("sum_l2", 0.0) or 0.0), 1e-18) * max(float(state.get("sum_r2", 0.0) or 0.0), 1e-18))
    corr = float(np.clip(float(state.get("sum_lr", 0.0) or 0.0) / den, -1.0, 1.0)) if den > 0 else 0.0
    mono_peak = float(state.get("mono_peak", 0.0) or 0.0)
    stereo_peak = float(state.get("stereo_peak", 0.0) or 0.0)
    mono_delta = _db(mono_peak + 1e-12) - _db(stereo_peak + 1e-12)
    return {
        "active": True,
        "phase_correlation": round(corr, 4),
        "mono_folddown_peak_delta_db": round(float(mono_delta), 3),
        "sample_count": int(state.get("sample_count", 0) or 0),
        "method": "running_full_render_translation_qc_aggregate_v6312",
    }






def _augmentation_mark_block(state: dict[str, Any], *, applied: bool, reason: str | None = None) -> dict[str, Any]:
    """Track v63.3 assist-layer telemetry across blocks without stale flags.

    The last render block can be silence/fade-out.  Public telemetry must show
    whether the module was planned/active and whether it applied on any block,
    not only the status of the final block.  Keep DSP filter state intact.
    """
    if not isinstance(state, dict):
        state = {}
    n = int(state.get("block_count", 0) or 0) + 1
    state["block_count"] = n
    state["active"] = True
    state["last_block_applied"] = bool(applied)
    if applied:
        c = int(state.get("applied_block_count", 0) or 0) + 1
        state["applied_block_count"] = c
        state["applied"] = True
        # v63.3.4.1: a module may reject one block and apply on a later block.
        # Do not let the earlier block-level rejection reason masquerade as the
        # aggregate/run-level reason for an applied module.  Keep the public
        # bypass evidence under last_bypass_reason only when the last block was
        # actually bypassed.
        state.pop("last_bypass_reason", None)
        stale_reason = str(state.get("reason") or "")
        if stale_reason and any(token in stale_reason for token in ("rejected", "disabled", "silent", "below_threshold", "too_quiet")):
            state.pop("reason", None)
    else:
        c = int(state.get("applied_block_count", 0) or 0)
        state["applied"] = bool(state.get("applied", False))
        if reason:
            state["last_bypass_reason"] = str(reason)[:120]
    state["applied_fraction"] = round(float(c) / float(max(n, 1)), 4)
    return state

def _augmentation_gain(strategy: dict[str, Any] | None, name: str, default: str = "off") -> float:
    if not isinstance(strategy, dict):
        return 0.0
    aug = strategy.get("stem_augmentation") if isinstance(strategy.get("stem_augmentation"), dict) else {}
    enabled = aug.get("modules_enabled") if isinstance(aug.get("modules_enabled"), dict) else {}
    if enabled and not bool(enabled.get(name, False)):
        return 0.0
    mods = aug.get("modules") if isinstance(aug.get("modules"), dict) else {}
    rec = mods.get(name) if isinstance(mods.get(name), dict) else {}
    return _intensity_scalar(rec.get("intensity"), default)


def _limit_assist_layer(layer: np.ndarray, dry: np.ndarray, *, blend_db: float, max_peak_ratio: float = 0.55) -> np.ndarray:
    wet = _ensure_stereo(layer).astype(np.float32, copy=False)
    dry = _ensure_stereo(dry).astype(np.float32, copy=False)
    wet *= _amp(float(blend_db))
    dp = _peak(dry)
    wp = _peak(wet)
    if dp > 1e-8 and wp > dp * float(max_peak_ratio):
        wet *= np.float32((dp * float(max_peak_ratio)) / max(wp, 1e-8))
    return np.nan_to_num(wet, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)


def _aug_metric_max_db(state: dict[str, Any], key: str, value_db: float) -> None:
    """Store a run-level max dB metric while preserving last-block telemetry.

    Several augmentation reports used to look ineffective when the final fade-out
    block overwrote peak/RMS fields with near-silence.  Keep `<key>_last` for the
    current block and make `<key>` the maximum useful value observed across the
    render.  Since these are dB values, the maximum is the least-negative value.
    """
    try:
        v = float(value_db)
    except Exception:
        return
    if not math.isfinite(v):
        return
    state[str(key) + "_last"] = round(float(v), 3)
    try:
        old = float(state.get(key))
    except Exception:
        old = -120.0
    state[str(key)] = round(float(max(old, v)), 3)


def _aug_metric_max(state: dict[str, Any], key: str, value: float) -> None:
    try:
        v = float(value)
    except Exception:
        return
    if not math.isfinite(v):
        return
    state[str(key) + "_last"] = round(float(v), 6)
    try:
        old = float(state.get(key))
    except Exception:
        old = 0.0
    state[str(key)] = round(float(max(old, v)), 6)


def _augment_bass_harmonic_translation(
    bass_bus: np.ndarray,
    vocal_bus: np.ndarray | None = None,
    *,
    sr: int,
    intensity: float,
    state: dict[str, Any],
    recipe: str | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.8.0 guarded bass harmonic translation final form.

    The module improves small-speaker bass audibility by deriving bounded 2nd/3rd
    harmonics from the existing bass fundamental.  It does not boost sub, does
    not ask the limiter for more loudness, and does not alter the vocal bus.  Low
    bass is first mono-anchored; generated harmonics are DC-blocked, band-limited,
    peak-guarded, and downscaled when vocal low-mid/fundamental conflict is high.
    """
    if intensity <= 0.0 or not _env_on("BUSY_BAMIX_V6380_BASS_HARMONIC_TRANSLATION", "1"):
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return bass_bus, state
    if bass_bus.size == 0 or _rms(bass_bus) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        state.update({"active": True, "method": "v6380_bass_harmonic_translation_bypass_silent"})
        return bass_bus, state

    y = _ensure_stereo(bass_bus).astype(np.float32, copy=False)
    corr_pre = _corr(y)
    mid = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    side = ((y[:, 0] - y[:, 1]) * 0.5).astype(np.float32, copy=False)

    # Strict mono anchor: attenuate only side-band low energy so sub/fundamental
    # remains phase-coherent without collapsing the upper stereo character.
    mono_xover = _env_float("BUSY_BAMIX_V6380_MONO_ANCHOR_HZ", 128.0, minimum=70.0, maximum=190.0)
    mid_low, state = _stateful_sos_filter_1d(mid, state, "v6380_bass_mid_low_anchor", sr=sr, btype="lowpass", cutoff=mono_xover, order=4)
    side_low, state = _stateful_sos_filter_1d(side, state, "v6380_bass_side_low_anchor", sr=sr, btype="lowpass", cutoff=mono_xover, order=4)
    side_low_over_mid_db = _db(_rms(side_low) + 1e-12) - _db(_rms(mid_low) + 1e-12)
    side_start = _env_float("BUSY_BAMIX_V6380_SIDE_LOW_START_DB", -16.0, minimum=-36.0, maximum=0.0)
    side_need = float(np.clip((side_low_over_mid_db - side_start) / _env_float("BUSY_BAMIX_V6380_SIDE_LOW_RANGE_DB", 12.0, minimum=2.0, maximum=36.0), 0.0, 1.0))
    side_trim_db = 0.0
    if side_need > _env_float("BUSY_BAMIX_V6380_SIDE_LOW_MIN_NEED", 0.04, minimum=0.0, maximum=0.5):
        side_trim_db = -_env_float("BUSY_BAMIX_V6380_SIDE_LOW_MAX_TRIM_DB", 4.5, minimum=0.0, maximum=12.0) * side_need * float(np.clip(intensity, 0.0, 1.0))
        side = (side + side_low * np.float32(_amp(side_trim_db) - 1.0)).astype(np.float32, copy=False)
        y = np.stack([mid + side, mid - side], axis=1).astype(np.float32, copy=False)
    mono = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)

    # Fundamental isolation.  A tiny high-pass before the low-pass is the DC guard
    # input stage, not a sub boost.
    fund_hp = _env_float("BUSY_BAMIX_V6380_FUND_HP_HZ", 28.0, minimum=12.0, maximum=55.0)
    fund_lp = _env_float("BUSY_BAMIX_V6380_FUND_LP_HZ", 118.0, minimum=70.0, maximum=165.0)
    fundamental, state = _stateful_sos_filter_1d(mono, state, "v6380_bass_fund_hp", sr=sr, btype="highpass", cutoff=fund_hp, order=2)
    fundamental, state = _stateful_sos_filter_1d(fundamental, state, "v6380_bass_fund_lp", sr=sr, btype="lowpass", cutoff=fund_lp, order=4)
    fund_rms = _rms(fundamental)
    pk = float(np.percentile(np.abs(fundamental), 95.0)) if fundamental.size else 0.0
    if pk < 1e-7 or fund_rms < _amp(_env_float("BUSY_BAMIX_V6380_MIN_FUND_RMS_DB", -70.0, minimum=-110.0, maximum=-36.0)):
        state = _augmentation_mark_block(state, applied=False, reason="low_fundamental_too_quiet")
        state.update({
            "active": True,
            "reason": "low_fundamental_too_quiet",
            "fundamental_rms_db": round(_db(fund_rms), 3),
            "method": "v6380_bass_harmonic_translation_bypass_quiet_fundamental",
        })
        return y, state

    # Input-relative harmonic need: if the existing 2nd/3rd region is already
    # strong, keep the layer tiny even when intensity requested by planner is high.
    existing_harm, state = _stateful_sos_filter_1d(mono, state, "v6380_bass_existing_harm_band", sr=sr, btype="bandpass", cutoff=[fund_lp, _env_float("BUSY_BAMIX_V6380_HARM_LP_HZ", 360.0, minimum=220.0, maximum=520.0)], order=2)
    existing_harm_rms = _rms(existing_harm)
    harmonic_deficit_db = _db(fund_rms + 1e-12) - _db(existing_harm_rms + 1e-12)
    need = float(np.clip((harmonic_deficit_db - _env_float("BUSY_BAMIX_V6380_HARM_DEFICIT_START_DB", 5.0, minimum=-6.0, maximum=18.0)) / _env_float("BUSY_BAMIX_V6380_HARM_DEFICIT_RANGE_DB", 14.0, minimum=3.0, maximum=36.0), 0.0, 1.0))
    need = max(need, side_need * _env_float("BUSY_BAMIX_V6380_SIDE_NEED_TO_HARMONIC", 0.35, minimum=0.0, maximum=1.0))

    recipe_l = str(recipe or "").lower()
    if any(tok in recipe_l for tok in ("club", "dense", "edm", "low_end", "bass")):
        genre_scalar = _env_float("BUSY_BAMIX_V6380_GENRE_SCALAR_DENSE", 1.08, minimum=0.4, maximum=1.5)
    elif any(tok in recipe_l for tok in ("acoustic", "cinematic", "natural", "fragile")):
        genre_scalar = _env_float("BUSY_BAMIX_V6380_GENRE_SCALAR_NATURAL", 0.62, minimum=0.2, maximum=1.0)
    else:
        genre_scalar = 1.0

    x = np.clip(fundamental / max(pk, 1e-7), -1.0, 1.0).astype(np.float32, copy=False)
    t2 = (2.0 * x * x - 1.0).astype(np.float32, copy=False)
    t3 = (4.0 * x * x * x - 3.0 * x).astype(np.float32, copy=False)
    t2 -= np.float32(float(np.mean(t2)) if t2.size else 0.0)
    t3 -= np.float32(float(np.mean(t3)) if t3.size else 0.0)

    intensity_s = float(np.clip(float(intensity), 0.0, 1.0))
    amount = float(np.clip(intensity_s * (0.45 + 0.55 * need) * genre_scalar, 0.0, _env_float("BUSY_BAMIX_V6380_MAX_AMOUNT", 0.94, minimum=0.1, maximum=1.0)))
    t2_mix = _env_float("BUSY_BAMIX_V6380_T2", 0.34, minimum=0.0, maximum=0.85) * amount
    t3_mix = _env_float("BUSY_BAMIX_V6380_T3", 0.18, minimum=0.0, maximum=0.65) * amount
    shaped = (t2_mix * t2 + t3_mix * t3).astype(np.float32, copy=False)
    drive = _env_float("BUSY_BAMIX_V6380_DRIVE", 1.12, minimum=0.35, maximum=2.5) * (0.80 + 0.35 * amount)
    shaped = np.tanh(shaped * np.float32(drive)).astype(np.float32, copy=False)
    shaped -= np.float32(float(np.mean(shaped)) if shaped.size else 0.0)

    # DC blocker and translation band limit after nonlinear generation.
    dc_hp = _env_float("BUSY_BAMIX_V6380_DC_BLOCK_HP_HZ", 24.0, minimum=8.0, maximum=60.0)
    harm_hp = _env_float("BUSY_BAMIX_V6380_HARM_HP_HZ", 92.0, minimum=60.0, maximum=150.0)
    harm_lp = _env_float("BUSY_BAMIX_V6380_HARM_LP_HZ", 360.0, minimum=220.0, maximum=520.0)
    dc_before = float(np.mean(shaped)) if shaped.size else 0.0
    shaped, state = _stateful_sos_filter_1d(shaped, state, "v6380_bass_harm_dc_block", sr=sr, btype="highpass", cutoff=dc_hp, order=2)
    shaped, state = _stateful_sos_filter_1d(shaped, state, "v6380_bass_harm_band_limit", sr=sr, btype="bandpass", cutoff=[harm_hp, harm_lp], order=2)
    dc_after = float(np.mean(shaped)) if shaped.size else 0.0

    # Vocal low-mid conflict notch: never cut the vocal bus; reduce only the
    # generated bass harmonic layer in the vocal fundamental/body lane.
    vocal_conflict_need = 0.0
    vocal_duck_db = 0.0
    vocal_lm_db = None
    harm_lm_db = None
    if vocal_bus is not None and np.asarray(vocal_bus).size:
        try:
            vmono = np.mean(_ensure_stereo(vocal_bus).astype(np.float32, copy=False), axis=1).astype(np.float32, copy=False)
            v_lm, state = _stateful_sos_filter_1d(vmono, state, "v6380_vocal_conflict_lm", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6380_VOCAL_CONFLICT_HP_HZ", 105.0, minimum=70.0, maximum=170.0), _env_float("BUSY_BAMIX_V6380_VOCAL_CONFLICT_LP_HZ", 330.0, minimum=220.0, maximum=520.0)], order=2)
            h_lm, state = _stateful_sos_filter_1d(shaped, state, "v6380_bass_harm_conflict_lm", sr=sr, btype="bandpass", cutoff=[105.0, 330.0], order=2)
            v_lm_rms = _rms(v_lm)
            h_lm_rms = _rms(h_lm)
            vocal_lm_db = _db(v_lm_rms)
            harm_lm_db = _db(h_lm_rms)
            if v_lm_rms > _amp(_env_float("BUSY_BAMIX_V6380_VOCAL_ACTIVE_RMS_DB", -54.0, minimum=-90.0, maximum=-24.0)) and h_lm_rms > 1e-9:
                # Conflict rises when generated harmonics approach the vocal low-mid
                # lane.  Downscale is bounded and can reach a notch-like reduction.
                proximity_db = harm_lm_db - vocal_lm_db
                vocal_conflict_need = float(np.clip((proximity_db - _env_float("BUSY_BAMIX_V6380_VOCAL_CONFLICT_START_DB", -20.0, minimum=-36.0, maximum=0.0)) / _env_float("BUSY_BAMIX_V6380_VOCAL_CONFLICT_RANGE_DB", 18.0, minimum=4.0, maximum=36.0), 0.0, 1.0))
                if vocal_conflict_need > 0.01:
                    vocal_duck_db = -_env_float("BUSY_BAMIX_V6380_VOCAL_CONFLICT_MAX_DUCK_DB", 3.8, minimum=0.0, maximum=9.0) * vocal_conflict_need * intensity_s
                    shaped *= np.float32(_amp(vocal_duck_db))
        except Exception:
            vocal_conflict_need = 0.0
            vocal_duck_db = 0.0

    layer = np.stack([shaped, shaped], axis=1).astype(np.float32, copy=False)
    blend = _env_float("BUSY_BAMIX_V6380_BLEND_DB", -16.8, minimum=-28.0, maximum=-8.0) + _env_float("BUSY_BAMIX_V6380_BLEND_LIFT_DB", 2.8, minimum=0.0, maximum=6.0) * amount
    wet = _limit_assist_layer(layer, y, blend_db=blend, max_peak_ratio=_env_float("BUSY_BAMIX_V6380_MAX_PEAK_RATIO", 0.42, minimum=0.04, maximum=0.75))
    out = (y + wet).astype(np.float32, copy=False)

    # Guard against stealing headroom before the shared bus_peak_guard.  Scale only
    # the generated wet contribution when possible; do not globally boost bass.
    pk_in = _peak(y)
    pk_out = _peak(out)
    peak_guard_db = 0.0
    if pk_in > 1e-8 and pk_out > pk_in * _amp(_env_float("BUSY_BAMIX_V6380_MAX_PEAK_BUMP_DB", 0.22, minimum=0.0, maximum=0.8)):
        max_pk = pk_in * _amp(_env_float("BUSY_BAMIX_V6380_MAX_PEAK_BUMP_DB", 0.22, minimum=0.0, maximum=0.8))
        lo, hi = 0.0, 1.0
        for _ in range(10):
            mid_s = (lo + hi) * 0.5
            cand = (y + wet * np.float32(mid_s)).astype(np.float32, copy=False)
            if _peak(cand) <= max_pk:
                lo = mid_s
            else:
                hi = mid_s
        wet *= np.float32(lo)
        out = (y + wet).astype(np.float32, copy=False)
        peak_guard_db = _db(float(lo))

    corr_post = _corr(out)
    # Mono compatibility guard should never be worsened by this module.
    mono_guard_scale = 1.0
    if corr_post + 1e-4 < corr_pre - _env_float("BUSY_BAMIX_V6380_MAX_CORR_DROP", 0.015, minimum=0.0, maximum=0.20):
        mono_guard_scale = _env_float("BUSY_BAMIX_V6380_MONO_GUARD_SCALE", 0.65, minimum=0.0, maximum=1.0)
        wet *= np.float32(mono_guard_scale)
        out = (y + wet).astype(np.float32, copy=False)
        corr_post = _corr(out)

    assist_rms = _rms(wet)
    # Report delivered harmonic ratios after blend/peak/mono guards, not the
    # pre-blend Chebyshev polynomial magnitude.  This keeps telemetry aligned
    # with the actual small-speaker assist injected into the bus.
    mix_sum = max(abs(float(t2_mix)) + abs(float(t3_mix)), 1e-12)
    delivered_harm_rms = _rms(wet[:, 0] if wet.ndim == 2 else wet)
    second_ratio = float((delivered_harm_rms * abs(float(t2_mix)) / mix_sum) / max(fund_rms, 1e-12))
    third_ratio = float((delivered_harm_rms * abs(float(t3_mix)) / mix_sum) / max(fund_rms, 1e-12))
    state = _augmentation_mark_block(state, applied=bool(assist_rms > _amp(_env_float("BUSY_BAMIX_V6380_MIN_ASSIST_RMS_DB", -86.0, minimum=-120.0, maximum=-48.0))))
    state.update({
        "active": True,
        "applied": bool(state.get("applied", False)),
        "intensity": round(float(intensity_s), 4),
        "amount": round(float(amount), 4),
        "input_relative_need_score": round(float(need), 4),
        "blend_db": round(float(blend), 3),
        "method": "v6380_chebyshev_2nd_3rd_bass_harmonic_translation_with_dc_blocker_mono_anchor_vocal_conflict_notch",
        "harmonic_generation": {
            "type": "chebyshev_second_third_bounded",
            "fundamental_hp_hz": round(float(fund_hp), 1),
            "fundamental_lp_hz": round(float(fund_lp), 1),
            "harmonic_band_hz": [round(float(harm_hp), 1), round(float(harm_lp), 1)],
            "t2_mix": round(float(t2_mix), 4),
            "t3_mix": round(float(t3_mix), 4),
            "drive": round(float(drive), 4),
            "second_harmonic_ratio": round(float(second_ratio), 6),
            "third_harmonic_ratio": round(float(third_ratio), 6),
        },
        "second_harmonic_ratio": round(float(second_ratio), 6),
        "third_harmonic_ratio": round(float(third_ratio), 6),
        "mono_anchor": {
            "active": True,
            "strict": True,
            "xover_hz": round(float(mono_xover), 1),
            "side_low_over_mid_db": round(float(side_low_over_mid_db), 3),
            "side_low_trim_db": round(float(side_trim_db), 3),
            "correlation_pre": round(float(corr_pre), 4),
            "correlation_post": round(float(corr_post), 4),
        },
        "dc_blocker": {
            "active": True,
            "hp_hz": round(float(dc_hp), 1),
            "dc_before": round(float(dc_before), 8),
            "dc_after": round(float(dc_after), 8),
        },
        "vocal_low_mid_conflict_notch": {
            "active": bool(vocal_bus is not None and np.asarray(vocal_bus).size),
            "applied": bool(vocal_duck_db < -0.001),
            "need_score": round(float(vocal_conflict_need), 4),
            "duck_db": round(float(vocal_duck_db), 3),
            "vocal_lowmid_db": round(float(vocal_lm_db), 3) if vocal_lm_db is not None and math.isfinite(float(vocal_lm_db)) else None,
            "harmonic_lowmid_db": round(float(harm_lm_db), 3) if harm_lm_db is not None and math.isfinite(float(harm_lm_db)) else None,
            "policy": "downscale_generated_bass_harmonics_only_vocal_bus_is_unchanged",
        },
        "guards": {
            "sub_boost_forbidden": True,
            "dc_offset_guard": True,
            "strict_mono_anchor": True,
            "low_side_phase_guard": True,
            "vocal_priority_guard": True,
            "peak_guard_db": round(float(peak_guard_db), 3),
            "mono_guard_scale": round(float(mono_guard_scale), 4),
        },
        "fundamental_rms_db": round(_db(fund_rms), 3),
        "existing_harmonic_rms_db": round(_db(existing_harm_rms), 3),
        "harmonic_deficit_db": round(float(harmonic_deficit_db), 3),
        "bass_translation_amount": round(float(amount), 4),
    })
    _aug_metric_max_db(state, "input_peak_dbfs", _db(_peak(y)))
    _aug_metric_max_db(state, "assist_peak_dbfs", _db(_peak(wet)))
    _aug_metric_max_db(state, "assist_rms_db", _db(assist_rms))
    _aug_metric_max(state, "second_harmonic_ratio_max", second_ratio)
    _aug_metric_max(state, "third_harmonic_ratio_max", third_ratio)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _augment_low_mid_body_fill(music_bed: np.ndarray, drum_bus: np.ndarray, vocal_bus: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0:
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return music_bed, state
    if music_bed.size == 0 or _rms(music_bed) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        return music_bed, state
    y = _ensure_stereo(music_bed).astype(np.float32, copy=False)
    source = y + _ensure_stereo(drum_bus).astype(np.float32, copy=False) * _env_float("BUSY_BAMIX_V633_BODY_DRUM_FEED", 0.18, minimum=0.0, maximum=0.6)
    fill, state = _stateful_sos_filter_stereo(source, state, "body_fill_band", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V633_BODY_HP_HZ", 160.0, minimum=90.0, maximum=260.0), _env_float("BUSY_BAMIX_V633_BODY_LP_HZ", 520.0, minimum=300.0, maximum=800.0)], order=2)
    # Vocal-aware scalar ducking: intentionally cheaper and safer than sample-wise coefficient modulation.
    vocal_mid, state = _stateful_sos_filter_stereo(vocal_bus, state, "body_fill_vocal_band", sr=sr, btype="bandpass", cutoff=[150.0, 520.0], order=2)
    v_env = _rms(vocal_mid)
    f_env = _rms(fill)
    duck_db = 0.0
    if v_env > f_env * _env_float("BUSY_BAMIX_V633_BODY_VOCAL_DUCK_RATIO", 0.70, minimum=0.1, maximum=3.0):
        duck_db = -_env_float("BUSY_BAMIX_V633_BODY_VOCAL_DUCK_DB", 3.5, minimum=0.0, maximum=9.0) * float(intensity)
    fill = fill * _amp(duck_db)
    # v63.4.3: keep low-mid body mostly center-owned before harmonic fill.
    # The v63.4.2 body engine improved audibility, but the latest run still
    # carried a low_mid_bottleneck blocker.  Low-mid assist that leaks into the
    # side channel is more likely to become mud after final limiting, so this
    # stage narrows only the derived 160-520 Hz assist layer, not the source bed.
    body_side_scale = 1.0
    body_ms_separation_active = False
    if _env_on("BUSY_BAMIX_V6343_BODY_MS_LOW_MID_SEPARATION", "1"):
        mid_fill = ((fill[:, 0] + fill[:, 1]) * 0.5).astype(np.float32, copy=False)
        side_fill = ((fill[:, 0] - fill[:, 1]) * 0.5).astype(np.float32, copy=False)
        body_side_scale = 1.0 - _env_float("BUSY_BAMIX_V6343_BODY_SIDE_RETAIN_REDUCTION", 0.58, minimum=0.0, maximum=0.9) * float(np.clip(intensity, 0.0, 1.0))
        body_side_scale = float(np.clip(body_side_scale, _env_float("BUSY_BAMIX_V6343_BODY_SIDE_RETAIN_MIN", 0.32, minimum=0.0, maximum=1.0), 1.0))
        fill = np.stack([mid_fill + side_fill * body_side_scale, mid_fill - side_fill * body_side_scale], axis=1).astype(np.float32, copy=False)
        body_ms_separation_active = True
    # v63.4.2: role-gated dynamic harmonic body fill.  The previous proxy was a
    # quiet band-limited copy, which often reported applied=true while the final
    # low_mid_bottleneck blocker stayed.  Add bounded harmonic body and upward
    # leveling before the low-level blend; keep vocal ducking and assist peak
    # guards as the safety boundary.
    drive = _env_float("BUSY_BAMIX_V633_BODY_SAT_DRIVE", 0.55, minimum=0.0, maximum=2.0) * float(intensity)
    body_engine = "v63310_band_limited_body_proxy"
    if _env_on("BUSY_BAMIX_V6342_DYNAMIC_BODY_FILL", "1"):
        body_engine = "v6342_dynamic_harmonic_vocal_ducked_low_mid_body_fill"
        mono = np.mean(fill, axis=1).astype(np.float32, copy=False)
        pk = float(np.percentile(np.abs(mono), 96.0)) if mono.size else 0.0
        if pk > 1e-8:
            norm = np.clip(mono / max(pk, 1e-8), -1.0, 1.0).astype(np.float32, copy=False)
            t2 = 2.0 * norm * norm - 1.0
            t3 = 4.0 * norm * norm * norm - 3.0 * norm
            h2 = _env_float("BUSY_BAMIX_V6342_BODY_H2", 0.18, minimum=0.0, maximum=0.8) * float(intensity)
            h3 = _env_float("BUSY_BAMIX_V6342_BODY_H3", 0.10, minimum=0.0, maximum=0.6) * float(intensity)
            harm = (norm * 0.60 + h2 * t2 + h3 * t3).astype(np.float32, copy=False)
            harm -= float(np.mean(harm)) if harm.size else 0.0
            harm = np.stack([harm, harm], axis=1).astype(np.float32, copy=False)
            harm, state = _stateful_sos_filter_stereo(harm, state, "body_fill_harm_band", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6342_BODY_HARM_HP_HZ", 135.0, minimum=80.0, maximum=260.0), _env_float("BUSY_BAMIX_V6342_BODY_HARM_LP_HZ", 640.0, minimum=350.0, maximum=900.0)], order=2)
            fill = (fill * 0.78 + harm * _env_float("BUSY_BAMIX_V6342_BODY_HARM_MIX", 0.22, minimum=0.0, maximum=0.55)).astype(np.float32, copy=False)
        vocal_fund_duck_db = 0.0
        if _env_on("BUSY_BAMIX_V6343_BODY_VOCAL_FUNDAMENTAL_DUCK", "1"):
            try:
                vf, state = _stateful_sos_filter_stereo(vocal_bus, state, "body_fill_vocal_fund_v6343", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6343_VOCAL_FUND_HP_HZ", 115.0, minimum=70.0, maximum=220.0), _env_float("BUSY_BAMIX_V6343_VOCAL_FUND_LP_HZ", 360.0, minimum=220.0, maximum=520.0)], order=2)
                bf, state = _stateful_sos_filter_stereo(fill, state, "body_fill_conflict_band_v6343", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6343_VOCAL_FUND_HP_HZ", 115.0, minimum=70.0, maximum=220.0), _env_float("BUSY_BAMIX_V6343_VOCAL_FUND_LP_HZ", 360.0, minimum=220.0, maximum=520.0)], order=2)
                vf_r = _rms(vf); bf_r = _rms(bf)
                conflict_db = _db(vf_r + 1e-12) - _db(bf_r + 1e-12)
                if vf_r > 1e-8 and conflict_db > _env_float("BUSY_BAMIX_V6343_BODY_VOCAL_CONFLICT_START_DB", -2.5, minimum=-12.0, maximum=8.0):
                    duck_need = float(np.clip((conflict_db + 2.5) / 9.0, 0.0, 1.0))
                    vocal_fund_duck_db = -_env_float("BUSY_BAMIX_V6343_BODY_VOCAL_FUND_DUCK_MAX_DB", 2.6, minimum=0.0, maximum=6.0) * duck_need * float(np.clip(intensity, 0.0, 1.0))
                    fill = (fill * np.float32(_amp(vocal_fund_duck_db))).astype(np.float32, copy=False)
            except Exception:
                vocal_fund_duck_db = 0.0
        fr = _rms(fill)
        yr = _rms(y)
        rel_target_db = _env_float("BUSY_BAMIX_V6342_BODY_PREBLEND_RMS_REL_DB", -3.2, minimum=-18.0, maximum=3.0)
        if yr > 1e-8 and fr > 1e-10:
            target = yr * _amp(rel_target_db)
            max_mu = _amp(_env_float("BUSY_BAMIX_V6342_BODY_MAX_PREBLEND_MAKEUP_DB", 7.5, minimum=0.0, maximum=18.0))
            fill = (fill * np.float32(min(max_mu, max(0.15, target / max(fr, 1e-10))))).astype(np.float32, copy=False)
    if drive > 0.01:
        fill = np.tanh(fill * (1.0 + drive)) / max(np.tanh(1.0 + drive), 1e-6)
    blend = _env_float("BUSY_BAMIX_V633_BODY_BLEND_DB", -15.6, minimum=-28.0, maximum=-8.0) + 3.2 * float(intensity)
    body_lift_db = 0.0
    if _env_on("BUSY_BAMIX_V63310_AUG_EFFECTIVENESS_LIFT", "1") and float(intensity) >= 0.44:
        body_lift_db = _env_float("BUSY_BAMIX_V63310_BODY_BLEND_LIFT_DB", 1.35, minimum=0.0, maximum=3.0)
        blend += body_lift_db
    v6342_body_lift_db = 0.0
    if _env_on("BUSY_BAMIX_V6342_DYNAMIC_BODY_FILL", "1") and float(intensity) >= 0.60:
        v6342_body_lift_db = _env_float("BUSY_BAMIX_V6342_BODY_BLEND_LIFT_DB", 1.15, minimum=0.0, maximum=3.5)
        blend += v6342_body_lift_db
    body_max_peak_ratio = _env_float("BUSY_BAMIX_V633_BODY_MAX_PEAK_RATIO", 0.54, minimum=0.05, maximum=0.85)
    if _env_on("BUSY_BAMIX_V6342_DYNAMIC_BODY_FILL", "1"):
        body_max_peak_ratio = max(float(body_max_peak_ratio), _env_float("BUSY_BAMIX_V6342_BODY_MIN_PEAK_RATIO", 0.58, minimum=0.05, maximum=0.85))
    wet = _limit_assist_layer(fill, y, blend_db=blend, max_peak_ratio=body_max_peak_ratio)
    mud_rollback_db = 0.0
    low_mid_delta_db = 0.0
    body_mid_side_ratio_db = 0.0
    if _env_on("BUSY_BAMIX_V6343_BODY_MUD_ROLLBACK", "1"):
        try:
            proposed = (y + wet).astype(np.float32, copy=False)
            y_lm, state = _stateful_sos_filter_stereo(y, state, "body_mud_y_lm_v6343", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6343_BODY_MUD_HP_HZ", 180.0, minimum=100.0, maximum=300.0), _env_float("BUSY_BAMIX_V6343_BODY_MUD_LP_HZ", 430.0, minimum=300.0, maximum=650.0)], order=2)
            p_lm, state = _stateful_sos_filter_stereo(proposed, state, "body_mud_p_lm_v6343", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6343_BODY_MUD_HP_HZ", 180.0, minimum=100.0, maximum=300.0), _env_float("BUSY_BAMIX_V6343_BODY_MUD_LP_HZ", 430.0, minimum=300.0, maximum=650.0)], order=2)
            low_mid_delta_db = _db(_rms(p_lm) + 1e-12) - _db(_rms(y_lm) + 1e-12)
            w_mid = ((wet[:, 0] + wet[:, 1]) * 0.5).astype(np.float32, copy=False)
            w_side = ((wet[:, 0] - wet[:, 1]) * 0.5).astype(np.float32, copy=False)
            body_mid_side_ratio_db = _db(_rms(w_side) + 1e-12) - _db(_rms(w_mid) + 1e-12)
            max_delta = _env_float("BUSY_BAMIX_V6343_BODY_MAX_LOWMID_DELTA_DB", 1.25, minimum=0.05, maximum=3.5) + 0.55 * float(np.clip(intensity, 0.0, 1.0))
            if low_mid_delta_db > max_delta:
                rollback = min(_env_float("BUSY_BAMIX_V6343_BODY_MUD_ROLLBACK_MAX_DB", 4.5, minimum=0.0, maximum=9.0), (low_mid_delta_db - max_delta) * _env_float("BUSY_BAMIX_V6343_BODY_MUD_ROLLBACK_RATIO", 0.85, minimum=0.1, maximum=2.0))
                mud_rollback_db = -float(rollback)
                wet = (wet * np.float32(_amp(mud_rollback_db))).astype(np.float32, copy=False)
        except Exception:
            mud_rollback_db = 0.0
    out = (y + wet).astype(np.float32, copy=False)
    state = _augmentation_mark_block(state, applied=True)
    v6370_active = _env_on("BUSY_BAMIX_V6370_BODY_CENTER_VOCAL_SUPPORT", "1")
    state.update({
        "active": True, "applied": True, "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend), 3), "effectiveness_lift_db": round(float(body_lift_db), 3), "v6342_body_lift_db": round(float(v6342_body_lift_db), 3), "vocal_duck_db": round(float(duck_db), 3),
        "v6343_vocal_fund_duck_db": round(float(vocal_fund_duck_db), 3) if 'vocal_fund_duck_db' in locals() else 0.0,
        "v6343_ms_low_mid_separation": bool(body_ms_separation_active),
        "v6343_body_side_scale": round(float(body_side_scale), 4),
        "v6343_low_mid_delta_db": round(float(low_mid_delta_db), 3),
        "v6343_body_mid_side_ratio_db": round(float(body_mid_side_ratio_db), 3),
        "v6343_mud_rollback_db": round(float(mud_rollback_db), 3),
        "damage_gate_status": bool(mud_rollback_db < -0.01),
        "max_peak_ratio": round(float(body_max_peak_ratio), 3),
        "v6370_body_center_vocal_support_active": bool(v6370_active),
        "v6370_dynamic_harmonic_body_fill": bool(v6370_active and _env_on("BUSY_BAMIX_V6342_DYNAMIC_BODY_FILL", "1")),
        "v6370_vocal_fundamental_ducking": bool(v6370_active and _env_on("BUSY_BAMIX_V6343_BODY_VOCAL_FUNDAMENTAL_DUCK", "1")),
        "v6370_mid_side_low_mid_separation": bool(v6370_active and body_ms_separation_active),
        "v6370_mud_rollback": bool(v6370_active and _env_on("BUSY_BAMIX_V6343_BODY_MUD_ROLLBACK", "1")),
        "method": "v6370_complete_dynamic_harmonic_center_owned_low_mid_body_fill_with_vocal_duck_and_mud_rollback" if v6370_active else ("v6343_ms_vocal_ducked_dynamic_harmonic_body_fill_with_mud_rollback" if _env_on("BUSY_BAMIX_V6343_BODY_MUD_ROLLBACK", "1") else body_engine),
    })
    _aug_metric_max_db(state, "input_rms_db", _db(_rms(y)))
    _aug_metric_max_db(state, "assist_rms_db", _db(_rms(wet)))
    _aug_metric_max_db(state, "assist_peak_dbfs", _db(_peak(wet)))
    return out, state


def _augment_vocal_support_body_layer(vocal_bus: np.ndarray, music_bed: np.ndarray, bass_bus: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.7.0 center/vocal hollow support layer.

    The layer is strictly derived from the vocal bus.  It does not raise the
    vocal stem with a static gain boost; it extracts a bounded center-owned
    fundamental/body band, adds very light even/odd harmonic support, ducks when
    bed/bass low-mid conflict is high, and rolls back when mud or peak pressure
    would increase.  This keeps low_mid_bottleneck work inside the Body/Center/
    Vocal capability instead of hiding it with DML or limiter push.
    """
    if intensity <= 0.0:
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return vocal_bus, state
    if vocal_bus.size == 0 or _rms(vocal_bus) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_vocal_block")
        return vocal_bus, state
    y = _ensure_stereo(vocal_bus).astype(np.float32, copy=False)
    bed = _ensure_stereo(music_bed).astype(np.float32, copy=False) if music_bed.size else np.zeros_like(y)
    bass = _ensure_stereo(bass_bus).astype(np.float32, copy=False) if bass_bus.size else np.zeros_like(y)

    hp = _env_float("BUSY_BAMIX_V6370_VOCAL_BODY_HP_HZ", 115.0, minimum=70.0, maximum=220.0)
    lp = _env_float("BUSY_BAMIX_V6370_VOCAL_BODY_LP_HZ", 430.0, minimum=260.0, maximum=720.0)
    body, state = _stateful_sos_filter_stereo(y, state, "v6370_vocal_body_band", sr=sr, btype="bandpass", cutoff=[hp, lp], order=2)
    body_mid = ((body[:, 0] + body[:, 1]) * 0.5).astype(np.float32, copy=False)
    pk = float(np.percentile(np.abs(body_mid), 96.0)) if body_mid.size else 0.0
    if pk < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="vocal_fundamental_band_too_quiet")
        state.update({"active": True, "method": "v6370_vocal_support_body_layer_bypass_quiet_fundamental"})
        return y, state

    norm = np.clip(body_mid / max(pk, 1e-8), -1.0, 1.0).astype(np.float32, copy=False)
    t2 = 2.0 * norm * norm - 1.0
    t3 = 4.0 * norm * norm * norm - 3.0 * norm
    h2 = _env_float("BUSY_BAMIX_V6370_VOCAL_BODY_H2", 0.12, minimum=0.0, maximum=0.55) * float(intensity)
    h3 = _env_float("BUSY_BAMIX_V6370_VOCAL_BODY_H3", 0.06, minimum=0.0, maximum=0.40) * float(intensity)
    layer_mono = (0.72 * norm + h2 * t2 + h3 * t3).astype(np.float32, copy=False)
    layer_mono -= float(np.mean(layer_mono)) if layer_mono.size else 0.0
    drive = _env_float("BUSY_BAMIX_V6370_VOCAL_BODY_DRIVE", 0.42, minimum=0.0, maximum=2.0) * float(intensity)
    if drive > 0.01:
        layer_mono = (np.tanh(layer_mono * (1.0 + drive)) / max(np.tanh(1.0 + drive), 1e-6)).astype(np.float32, copy=False)
    layer = np.stack([layer_mono, layer_mono], axis=1).astype(np.float32, copy=False)
    layer, state = _stateful_sos_filter_stereo(layer, state, "v6370_vocal_support_band_limit", sr=sr, btype="bandpass", cutoff=[hp, lp], order=2)

    yr = _rms(y)
    lr = _rms(layer)
    target_rel_db = _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_PREBLEND_RMS_REL_DB", -5.6, minimum=-22.0, maximum=0.0)
    preblend_makeup = 1.0
    if yr > 1e-8 and lr > 1e-10:
        target = yr * _amp(target_rel_db)
        preblend_makeup = float(np.clip(target / max(lr, 1e-10), _amp(-9.0), _amp(_env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MAX_MAKEUP_DB", 8.0, minimum=0.0, maximum=18.0))))
        layer = (layer * np.float32(preblend_makeup)).astype(np.float32, copy=False)

    conflict_duck_db = 0.0
    try:
        bed_lm, state = _stateful_sos_filter_stereo(bed + bass * np.float32(0.5), state, "v6370_vocal_support_conflict_bus", sr=sr, btype="bandpass", cutoff=[hp, lp], order=2)
        conflict_db = _db(_rms(bed_lm) + 1e-12) - _db(_rms(body) + 1e-12)
        if conflict_db > _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_CONFLICT_START_DB", -3.0, minimum=-12.0, maximum=8.0):
            conflict_need = float(np.clip((conflict_db + 3.0) / 10.0, 0.0, 1.0))
            conflict_duck_db = -_env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_CONFLICT_DUCK_MAX_DB", 2.8, minimum=0.0, maximum=8.0) * conflict_need * float(np.clip(intensity, 0.0, 1.0))
            layer = (layer * np.float32(_amp(conflict_duck_db))).astype(np.float32, copy=False)
    except Exception:
        conflict_db = 0.0

    blend = _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_BLEND_DB", -16.5, minimum=-30.0, maximum=-8.5) + 3.2 * float(intensity)
    max_peak_ratio = _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MAX_PEAK_RATIO", 0.44, minimum=0.04, maximum=0.8)
    wet = _limit_assist_layer(layer, y, blend_db=blend, max_peak_ratio=max_peak_ratio)

    mud_rollback_db = 0.0
    low_mid_delta_db = 0.0
    try:
        proposed = (y + wet).astype(np.float32, copy=False)
        y_lm, state = _stateful_sos_filter_stereo(y, state, "v6370_vocal_support_y_lm", sr=sr, btype="bandpass", cutoff=[150.0, 450.0], order=2)
        p_lm, state = _stateful_sos_filter_stereo(proposed, state, "v6370_vocal_support_p_lm", sr=sr, btype="bandpass", cutoff=[150.0, 450.0], order=2)
        low_mid_delta_db = _db(_rms(p_lm) + 1e-12) - _db(_rms(y_lm) + 1e-12)
        max_delta = _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MAX_LOWMID_DELTA_DB", 1.00, minimum=0.05, maximum=3.0) + 0.38 * float(np.clip(intensity, 0.0, 1.0))
        if low_mid_delta_db > max_delta:
            rollback = min(_env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MUD_ROLLBACK_MAX_DB", 4.0, minimum=0.0, maximum=9.0), (low_mid_delta_db - max_delta) * _env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MUD_ROLLBACK_RATIO", 0.95, minimum=0.1, maximum=2.0))
            mud_rollback_db = -float(rollback)
            wet = (wet * np.float32(_amp(mud_rollback_db))).astype(np.float32, copy=False)
    except Exception:
        pass

    out = (y + wet).astype(np.float32, copy=False)
    pk_in = _peak(y); pk_out = _peak(out)
    peak_guard_db = 0.0
    peak_bump = _amp(_env_float("BUSY_BAMIX_V6370_VOCAL_SUPPORT_MAX_PEAK_BUMP_DB", 0.18, minimum=0.0, maximum=0.8))
    if pk_in > 1e-8 and pk_out > pk_in * peak_bump:
        scale = (pk_in * peak_bump) / max(pk_out, 1e-8)
        out = (out * np.float32(scale)).astype(np.float32, copy=False)
        peak_guard_db = _db(scale)
    state = _augmentation_mark_block(state, applied=True)
    state.update({
        "active": True, "applied": True, "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend), 3),
        "preblend_makeup_db": round(float(_db(preblend_makeup)), 3),
        "target_preblend_rms_rel_db": round(float(target_rel_db), 3),
        "h2_mix": round(float(h2), 4), "h3_mix": round(float(h3), 4),
        "conflict_duck_db": round(float(conflict_duck_db), 3),
        "conflict_db": round(float(conflict_db), 3) if 'conflict_db' in locals() else 0.0,
        "low_mid_delta_db": round(float(low_mid_delta_db), 3),
        "mud_rollback_db": round(float(mud_rollback_db), 3),
        "peak_guard_db": round(float(peak_guard_db), 3),
        "max_peak_ratio": round(float(max_peak_ratio), 3),
        "center_owned_mono_layer": True,
        "method": "v6370_vocal_fundamental_body_support_layer_with_conflict_duck_and_mud_rollback",
    })
    _aug_metric_max_db(state, "vocal_input_rms_db", _db(_rms(y)))
    _aug_metric_max_db(state, "assist_rms_db", _db(_rms(wet)))
    _aug_metric_max_db(state, "assist_peak_dbfs", _db(_peak(wet)))
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _augment_drum_parallel_density(drum_bus: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0:
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return drum_bus, state
    if drum_bus.size == 0 or _rms(drum_bus) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        return drum_bus, state
    y = _ensure_stereo(drum_bus).astype(np.float32, copy=False)
    hp, state = _stateful_sos_filter_stereo(y, state, "drum_density_hp", sr=sr, btype="highpass", cutoff=_env_float("BUSY_BAMIX_V633_DRUM_DENSITY_HP_HZ", 90.0, minimum=45.0, maximum=160.0), order=2)
    # v63.4.3: decay-tail upward compression before the parallel saturator.
    # This targets the exact remaining blocker from v63.4.2.1: crest/transient
    # loss after final limiting.  It fills drum decay density without pushing
    # attack peaks harder, then the existing correlation and peak-ratio guards
    # decide whether the layer is safe.
    tail_amount = 0.0
    tail_active_ratio = 0.0
    if _env_on("BUSY_BAMIX_V6343_DRUM_DECAY_TAIL_UPWARD", "1"):
        try:
            rect = np.mean(np.abs(hp), axis=1).astype(np.float32, copy=False)
            ref = float(np.percentile(rect, 94.0)) if rect.size else 0.0
            floor = ref * _env_float("BUSY_BAMIX_V6343_DRUM_TAIL_FLOOR_RATIO", 0.075, minimum=0.0, maximum=0.5)
            ceil = ref * _env_float("BUSY_BAMIX_V6343_DRUM_TAIL_CEIL_RATIO", 0.62, minimum=0.1, maximum=1.0)
            if ref > 1e-8 and ceil > floor:
                aud = np.clip((rect - floor) / max(ceil - floor, 1e-8), 0.0, 1.0)
                below_attack = np.clip((ceil - rect) / max(ceil - floor, 1e-8), 0.0, 1.0)
                ctrl = (aud * below_attack).astype(np.float32, copy=False)
                tail_amount = _env_float("BUSY_BAMIX_V6343_DRUM_TAIL_UPWARD_AMOUNT", 0.48, minimum=0.0, maximum=1.2) * float(np.clip(intensity, 0.0, 1.0))
                hp = (hp * (1.0 + tail_amount * ctrl[:, None])).astype(np.float32, copy=False)
                tail_active_ratio = float(np.mean(ctrl > 0.08)) if ctrl.size else 0.0
        except Exception:
            tail_amount = 0.0
            tail_active_ratio = 0.0
    # Upward-ish tail density proxy: soft-saturate a filtered parallel layer and keep it low.
    drive = _env_float("BUSY_BAMIX_V633_DRUM_DENSITY_DRIVE", 1.75, minimum=0.5, maximum=4.5) * (0.75 + float(intensity))
    wet_src = np.tanh(hp * drive) / max(np.tanh(drive), 1e-6)
    corr = _corr(np.stack([np.mean(y, axis=1), np.mean(wet_src, axis=1)], axis=1)) if wet_src.shape[0] > 32 else 1.0
    if corr < _env_float("BUSY_BAMIX_V633_DRUM_DENSITY_MIN_CORR", 0.45, minimum=-0.2, maximum=0.95):
        state = _augmentation_mark_block(state, applied=False, reason="parallel_density_correlation_rejected")
        state.update({"reason": "parallel_density_correlation_rejected", "correlation": round(float(corr), 4)})
        return y, state
    blend = _env_float("BUSY_BAMIX_V633_DRUM_DENSITY_BLEND_DB", -13.0, minimum=-24.0, maximum=-5.5) + 3.2 * float(intensity)
    bass_mask_guard_db = 0.0
    bass_mask_scale = 1.0
    if _env_on("BUSY_BAMIX_V6343_DRUM_BASS_MASKING_GUARD", "1"):
        try:
            low, state = _stateful_sos_filter_stereo(y, state, "drum_density_bass_mask_low_v6343", sr=sr, btype="lowpass", cutoff=_env_float("BUSY_BAMIX_V6343_DRUM_BASS_MASK_LP_HZ", 135.0, minimum=70.0, maximum=220.0), order=2)
            body, state = _stateful_sos_filter_stereo(wet_src, state, "drum_density_bass_mask_body_v6343", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6343_DRUM_BODY_HP_HZ", 150.0, minimum=90.0, maximum=240.0), _env_float("BUSY_BAMIX_V6343_DRUM_BODY_LP_HZ", 950.0, minimum=450.0, maximum=1800.0)], order=2)
            bass_mask_guard_db = _db(_rms(low) + 1e-12) - _db(_rms(body) + 1e-12)
            start = _env_float("BUSY_BAMIX_V6343_DRUM_BASS_MASK_START_DB", 6.0, minimum=-6.0, maximum=18.0)
            if bass_mask_guard_db > start:
                trim = min(_env_float("BUSY_BAMIX_V6343_DRUM_BASS_MASK_MAX_TRIM_DB", 2.8, minimum=0.0, maximum=8.0), (bass_mask_guard_db - start) * 0.22)
                bass_mask_scale = _amp(-trim)
                blend -= trim
        except Exception:
            bass_mask_guard_db = 0.0
            bass_mask_scale = 1.0
    wet = _limit_assist_layer(wet_src, y, blend_db=blend, max_peak_ratio=_env_float("BUSY_BAMIX_V633_DRUM_DENSITY_MAX_PEAK_RATIO", 0.64, minimum=0.05, maximum=0.95))
    out = (y + wet).astype(np.float32, copy=False)
    state = _augmentation_mark_block(state, applied=True)
    state.update({
        "active": True, "applied": True, "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend), 3), "correlation": round(float(corr), 4),
        "v6343_tail_upward_amount": round(float(tail_amount), 4),
        "v6343_tail_active_ratio": round(float(tail_active_ratio), 4),
        "v6343_bass_mask_guard_db": round(float(bass_mask_guard_db), 3),
        "v6343_bass_mask_scale": round(float(bass_mask_scale), 4),
        "method": "v6343_decay_tail_parallel_drum_density_with_correlation_bass_mask_guard" if _env_on("BUSY_BAMIX_V6343_DRUM_DECAY_TAIL_UPWARD", "1") else "v6337_stateful_parallel_drum_density_assist_effective_telemetry",
    })
    _aug_metric_max_db(state, "input_rms_db", _db(_rms(y)))
    _aug_metric_max_db(state, "assist_rms_db", _db(_rms(wet)))
    _aug_metric_max_db(state, "assist_peak_dbfs", _db(_peak(wet)))
    return out, state



def _augment_transient_ghost(drum_bus: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.3.5 bounded transient ghost/punch support derived from the drum bus.

    This layer is intentionally peak-neutral and derived only from existing drum
    attacks.  It uses fast/slow envelope differential control to isolate attack
    moments, then blends a band-limited ghost layer at a low level.  The global
    v63.3 augmentation bus peak guard still clamps the summed result.
    """
    if intensity <= 0.0:
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return drum_bus, state
    if drum_bus.size == 0 or _rms(drum_bus) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        return drum_bus, state
    y = _ensure_stereo(drum_bus).astype(np.float32, copy=False)
    # Keep detector out of sub-bass so kick sustain does not falsely trigger.
    det, state = _stateful_sos_filter_stereo(
        y, state, "transient_ghost_detector_hp", sr=sr, btype="highpass",
        cutoff=_env_float("BUSY_BAMIX_V6335_TRANSIENT_GHOST_DETECT_HP_HZ", 140.0, minimum=70.0, maximum=320.0), order=2,
    )
    rect = np.abs(det).astype(np.float32, copy=False)
    prev_fast = np.asarray(state.get("fast_env", [0.0, 0.0]), dtype=np.float32).reshape(2)
    prev_slow = np.asarray(state.get("slow_env", [0.0, 0.0]), dtype=np.float32).reshape(2)
    atk_fast = _env_float("BUSY_BAMIX_V6335_TRANSIENT_FAST_MS", 1.0, minimum=0.1, maximum=4.0) / 1000.0
    atk_slow = _env_float("BUSY_BAMIX_V6335_TRANSIENT_SLOW_MS", 16.0, minimum=6.0, maximum=40.0) / 1000.0
    rel = _env_float("BUSY_BAMIX_V6335_TRANSIENT_RELEASE_MS", 28.0, minimum=8.0, maximum=90.0) / 1000.0
    a_fast = float(np.exp(-1.0 / max(float(sr) * atk_fast, 1.0)))
    a_slow = float(np.exp(-1.0 / max(float(sr) * atk_slow, 1.0)))
    a_rel = float(np.exp(-1.0 / max(float(sr) * rel, 1.0)))
    fast = np.zeros_like(rect, dtype=np.float32)
    slow = np.zeros_like(rect, dtype=np.float32)
    for i in range(rect.shape[0]):
        x = rect[i]
        prev_fast = np.where(x > prev_fast, a_fast * prev_fast + (1.0 - a_fast) * x, a_rel * prev_fast + (1.0 - a_rel) * x)
        prev_slow = np.where(x > prev_slow, a_slow * prev_slow + (1.0 - a_slow) * x, a_rel * prev_slow + (1.0 - a_rel) * x)
        fast[i] = prev_fast
        slow[i] = prev_slow
    state["fast_env"] = prev_fast
    state["slow_env"] = prev_slow
    diff = np.maximum(fast - slow, 0.0).astype(np.float32, copy=False)
    diff_peak = float(np.percentile(diff, 98.0)) if diff.size else 0.0
    if diff_peak < _env_float("BUSY_BAMIX_V6335_TRANSIENT_DIFF_MIN", 0.0008, minimum=0.00005, maximum=0.02):
        # v63.4.2.1: do not let a quiet/fade-out final block downgrade the
        # aggregate telemetry method after earlier blocks successfully applied
        # the v63.4.2 transient reconstruction path.  Keep the last-block
        # bypass reason visible, but preserve the aggregate method/effectiveness
        # identity so debug reports do not falsely suggest the old v63.3.6
        # transient proxy was used for the whole render.
        prev_applied = int(state.get("applied_block_count") or 0) > 0 or bool(state.get("applied"))
        prev_method = str(state.get("method") or "")
        if prev_applied and prev_method:
            method = prev_method
        elif _env_on("BUSY_BAMIX_V6342_TRANSIENT_RECONSTRUCTION", "1"):
            method = "v6342_envelope_transient_reconstruction_with_attack_lift"
        else:
            method = "v6336_effectiveness_normalized_transient_ghost"
        state = _augmentation_mark_block(state, applied=False, reason="transient_differential_below_threshold")
        state.update({
            "active": True,
            "transient_max_differential_last": round(float(diff_peak), 6),
            "last_bypass_reason": "transient_differential_below_threshold",
            "method": method,
            "aggregate_method_preserved_after_final_bypass": bool(prev_applied and prev_method),
        })
        _aug_metric_max(state, "transient_max_differential", float(diff_peak))
        return y, state
    ctrl = np.clip(diff / max(diff_peak, 1e-8), 0.0, 1.0)
    # Smoothly emphasize attacks without multiplying the dry bus directly.
    attack_layer = (det * ctrl).astype(np.float32, copy=False)
    attack_layer, state = _stateful_sos_filter_stereo(
        attack_layer, state, "transient_ghost_band_lp", sr=sr, btype="lowpass",
        cutoff=_env_float("BUSY_BAMIX_V6335_TRANSIENT_GHOST_LP_HZ", 7200.0, minimum=2500.0, maximum=12000.0), order=2,
    )
    drive = _env_float("BUSY_BAMIX_V6335_TRANSIENT_GHOST_SAT_DRIVE", 0.65, minimum=0.0, maximum=2.5) * float(intensity)
    if drive > 0.01:
        attack_layer = np.tanh(attack_layer * (1.0 + drive)) / max(np.tanh(1.0 + drive), 1e-6)

    # v63.3.5.2: the original executable transient ghost often reported
    # applied=true while the derived layer was far below useful audibility after
    # block filtering and blend.  Normalize the attack layer to a bounded RMS
    # target relative to the drum bus before the final low-level blend, then let
    # the per-layer peak limiter and bus guard enforce safety.  This remains a
    # deterministic, source-derived attack support layer; it does not invent new
    # hits.
    dry_rms = _rms(y)
    attack_rms_pre = _rms(attack_layer)
    effectiveness_makeup = 1.0
    target_rel_db = _env_float("BUSY_BAMIX_V6335_2_TRANSIENT_GHOST_PREBLEND_RMS_REL_DB", -1.5, minimum=-18.0, maximum=6.0)
    if dry_rms > 1e-8 and attack_rms_pre > 1e-10:
        target_attack_rms = dry_rms * _amp(target_rel_db)
        max_makeup = _amp(_env_float("BUSY_BAMIX_V6335_2_TRANSIENT_GHOST_MAX_MAKEUP_DB", 18.0, minimum=0.0, maximum=30.0))
        min_makeup = _amp(-_env_float("BUSY_BAMIX_V6335_2_TRANSIENT_GHOST_MAX_TRIM_DB", 9.0, minimum=0.0, maximum=24.0))
        effectiveness_makeup = float(np.clip(target_attack_rms / max(attack_rms_pre, 1e-10), min_makeup, max_makeup))
        attack_layer = (attack_layer * np.float32(effectiveness_makeup)).astype(np.float32, copy=False)
    attack_rms_post = _rms(attack_layer)

    blend = _env_float("BUSY_BAMIX_V6335_TRANSIENT_GHOST_BLEND_DB", -19.5, minimum=-34.0, maximum=-10.5) + 4.4 * float(intensity)
    transient_lift_db = 0.0
    max_peak_ratio = _env_float("BUSY_BAMIX_V6335_TRANSIENT_GHOST_MAX_PEAK_RATIO", 0.36, minimum=0.03, maximum=0.7)
    if _env_on("BUSY_BAMIX_V63310_AUG_EFFECTIVENESS_LIFT", "1") and float(intensity) >= 0.60:
        transient_lift_db = _env_float("BUSY_BAMIX_V63310_TRANSIENT_GHOST_BLEND_LIFT_DB", 1.10, minimum=0.0, maximum=3.0)
        blend += transient_lift_db
        max_peak_ratio = max(float(max_peak_ratio), _env_float("BUSY_BAMIX_V63310_TRANSIENT_GHOST_MIN_PEAK_RATIO", 0.40, minimum=0.03, maximum=0.7))
    v6342_transient_lift_db = 0.0
    if _env_on("BUSY_BAMIX_V6342_TRANSIENT_RECONSTRUCTION", "1") and float(intensity) >= 0.60:
        v6342_transient_lift_db = _env_float("BUSY_BAMIX_V6342_TRANSIENT_BLEND_LIFT_DB", 1.35, minimum=0.0, maximum=4.0)
        blend += v6342_transient_lift_db
        max_peak_ratio = max(float(max_peak_ratio), _env_float("BUSY_BAMIX_V6342_TRANSIENT_MIN_PEAK_RATIO", 0.48, minimum=0.03, maximum=0.75))
    wet = _limit_assist_layer(attack_layer, y, blend_db=blend, max_peak_ratio=max_peak_ratio)
    out = (y + wet).astype(np.float32, copy=False)
    state = _augmentation_mark_block(state, applied=True)
    state.update({
        "active": True, "applied": True, "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend), 3),
        "effectiveness_lift_db": round(float(transient_lift_db), 3),
        "v6342_transient_lift_db": round(float(v6342_transient_lift_db), 3),
        "effectiveness_makeup_db": round(_db(effectiveness_makeup), 3),
        "target_preblend_rms_rel_db": round(float(target_rel_db), 3),
        "max_peak_ratio": round(float(max_peak_ratio), 3),
        "method": "v6342_envelope_transient_reconstruction_with_attack_lift" if _env_on("BUSY_BAMIX_V6342_TRANSIENT_RECONSTRUCTION", "1") else "v63310_effectiveness_normalized_transient_ghost_with_attack_lift_telemetry",
    })
    _aug_metric_max(state, "transient_max_differential", float(diff_peak))
    _aug_metric_max_db(state, "dry_rms_db", _db(dry_rms))
    _aug_metric_max_db(state, "attack_rms_pre_db", _db(attack_rms_pre))
    _aug_metric_max_db(state, "attack_rms_post_db", _db(attack_rms_post))
    _aug_metric_max_db(state, "assist_peak_dbfs", _db(_peak(wet)))
    _aug_metric_max_db(state, "assist_rms_db", _db(_rms(wet)))
    try:
        state["attack_rms_delta_db"] = round(float(state.get("attack_rms_post_db", -120.0)) - float(state.get("attack_rms_pre_db", -120.0)), 3)
    except Exception:
        pass
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state

def _augment_center_anchor_mix(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0:
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return mix, state
    if mix.size == 0 or _rms(mix) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        return mix, state
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    corr_pre = _corr(y)
    mid = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    anchor, state = _stateful_sos_filter_1d(mid, state, "center_anchor_hp", sr=sr, btype="highpass", cutoff=_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_HP_HZ", 120.0, minimum=70.0, maximum=220.0), order=2)
    anchor, state = _stateful_sos_filter_1d(anchor, state, "center_anchor_lp", sr=sr, btype="lowpass", cutoff=_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_LP_HZ", 6500.0, minimum=2500.0, maximum=12000.0), order=2)

    v6370_active = _env_on("BUSY_BAMIX_V6370_BODY_CENTER_VOCAL_SUPPORT", "1")
    center_body_mix = 0.0
    center_body_rms_db = -120.0
    if v6370_active:
        try:
            body, state = _stateful_sos_filter_1d(mid, state, "v6370_center_anchor_body_band", sr=sr, btype="bandpass", cutoff=[_env_float("BUSY_BAMIX_V6370_CENTER_BODY_HP_HZ", 135.0, minimum=80.0, maximum=260.0), _env_float("BUSY_BAMIX_V6370_CENTER_BODY_LP_HZ", 560.0, minimum=320.0, maximum=900.0)], order=2)
            body_pk = float(np.percentile(np.abs(body), 96.0)) if body.size else 0.0
            if body_pk > 1e-8:
                bn = np.clip(body / max(body_pk, 1e-8), -1.0, 1.0).astype(np.float32, copy=False)
                body_h = (0.86 * bn + (_env_float("BUSY_BAMIX_V6370_CENTER_BODY_H2", 0.09, minimum=0.0, maximum=0.35) * float(intensity)) * (2.0 * bn * bn - 1.0)).astype(np.float32, copy=False)
                body_h -= float(np.mean(body_h)) if body_h.size else 0.0
                body_h, state = _stateful_sos_filter_1d(body_h, state, "v6370_center_anchor_body_band_limit", sr=sr, btype="bandpass", cutoff=[135.0, 560.0], order=2)
                center_body_mix = _env_float("BUSY_BAMIX_V6370_CENTER_BODY_MIX", 0.20, minimum=0.0, maximum=0.60) * float(np.clip(intensity, 0.0, 1.0))
                anchor = (anchor * (1.0 - center_body_mix) + body_h * center_body_mix).astype(np.float32, copy=False)
                center_body_rms_db = _db(_rms(body_h))
        except Exception:
            center_body_mix = 0.0

    target_rms = _amp(_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_TARGET_RMS_DB", -22.0, minimum=-32.0, maximum=-14.0))
    ar = _rms(anchor)
    makeup = 1.0
    if ar > 1e-8 and ar < target_rms:
        makeup = min(target_rms / max(ar, 1e-8), _amp(_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_MAX_MAKEUP_DB", 2.0, minimum=0.0, maximum=5.0) * float(intensity)))
    anchor = anchor * float(makeup)
    layer = np.stack([anchor, anchor], axis=1)
    blend = _env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_BLEND_DB", -16.3, minimum=-28.0, maximum=-8.5) + 3.7 * float(intensity)
    if v6370_active and float(intensity) >= 0.60:
        blend += _env_float("BUSY_BAMIX_V6370_CENTER_ANCHOR_BLEND_LIFT_DB", 0.55, minimum=0.0, maximum=2.5)
    wet = _limit_assist_layer(layer, y, blend_db=blend, max_peak_ratio=_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_MAX_PEAK_RATIO", 0.50, minimum=0.05, maximum=0.85))
    out = (y + wet).astype(np.float32, copy=False)

    side_trim_db = 0.0
    side_lowmid_trim_db = 0.0
    side_lowmid_over_mid_db = 0.0
    if corr_pre < _env_float("BUSY_BAMIX_V633_CENTER_SIDE_TRIM_CORR_BELOW", 0.45, minimum=-0.5, maximum=0.95):
        side_trim_db = -_env_float("BUSY_BAMIX_V633_CENTER_SIDE_TRIM_DB", 0.35, minimum=0.0, maximum=1.5) * float(intensity)
        mid_o = (out[:, 0] + out[:, 1]) * 0.5
        side_o = (out[:, 0] - out[:, 1]) * 0.5 * _amp(side_trim_db)
        out = np.stack([mid_o + side_o, mid_o - side_o], axis=1).astype(np.float32, copy=False)

    # v63.7.0: center hollow often survives as low-mid energy living in the side
    # channel.  Trim only the side low-mid component when it dominates mid low-mid;
    # leave the rest of the side channel untouched to avoid a width collapse.
    if v6370_active and _env_on("BUSY_BAMIX_V6370_CENTER_SIDE_LOWMID_GUARD", "1"):
        try:
            mid_o = ((out[:, 0] + out[:, 1]) * 0.5).astype(np.float32, copy=False)
            side_o = ((out[:, 0] - out[:, 1]) * 0.5).astype(np.float32, copy=False)
            mid_lm, state = _stateful_sos_filter_1d(mid_o, state, "v6370_center_mid_lm", sr=sr, btype="bandpass", cutoff=[140.0, 520.0], order=2)
            side_lm, state = _stateful_sos_filter_1d(side_o, state, "v6370_center_side_lm", sr=sr, btype="bandpass", cutoff=[140.0, 520.0], order=2)
            side_lowmid_over_mid_db = _db(_rms(side_lm) + 1e-12) - _db(_rms(mid_lm) + 1e-12)
            start = _env_float("BUSY_BAMIX_V6370_CENTER_SIDE_LM_START_DB", -8.0, minimum=-24.0, maximum=6.0)
            need = float(np.clip((side_lowmid_over_mid_db - start) / _env_float("BUSY_BAMIX_V6370_CENTER_SIDE_LM_RANGE_DB", 10.0, minimum=2.0, maximum=24.0), 0.0, 1.0))
            if need > 0.02:
                side_lowmid_trim_db = -_env_float("BUSY_BAMIX_V6370_CENTER_SIDE_LM_MAX_TRIM_DB", 1.8, minimum=0.0, maximum=6.0) * need * float(np.clip(intensity, 0.0, 1.0))
                side_o = (side_o + side_lm * np.float32(_amp(side_lowmid_trim_db) - 1.0)).astype(np.float32, copy=False)
                out = np.stack([mid_o + side_o, mid_o - side_o], axis=1).astype(np.float32, copy=False)
        except Exception:
            side_lowmid_trim_db = 0.0

    pk_in = _peak(y); pk_out = _peak(out)
    peak_guard_db = 0.0
    center_peak_bump = _amp(_env_float("BUSY_BAMIX_V633_CENTER_ANCHOR_MAX_PEAK_BUMP_DB", 0.22, minimum=0.0, maximum=0.8))
    if pk_in > 1e-8 and pk_out > pk_in * center_peak_bump:
        scale = (pk_in * center_peak_bump) / max(pk_out, 1e-8)
        out *= np.float32(scale)
        peak_guard_db = _db(scale)
    corr_post = _corr(out)
    state = _augmentation_mark_block(state, applied=True)
    state.update({
        "active": True, "applied": True, "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend), 3), "mid_makeup": round(float(makeup), 4),
        "side_trim_db": round(float(side_trim_db), 3),
        "v6370_center_body_mix": round(float(center_body_mix), 4),
        "v6370_center_body_rms_db": round(float(center_body_rms_db), 3),
        "v6370_side_lowmid_over_mid_db": round(float(side_lowmid_over_mid_db), 3),
        "v6370_side_lowmid_trim_db": round(float(side_lowmid_trim_db), 3),
        "v6370_peak_guard_db": round(float(peak_guard_db), 3),
        "correlation_pre": round(float(corr_pre), 4), "correlation_post": round(float(corr_post), 4),
        "method": "v6370_center_hollow_protection_with_center_body_anchor_and_side_lowmid_guard" if v6370_active else "v6337_band_limited_strict_mono_center_anchor_assist_effective_telemetry",
    })
    _aug_metric_max_db(state, "anchor_rms_db", _db(_rms(wet)))
    _aug_metric_max_db(state, "anchor_peak_dbfs", _db(_peak(wet)))
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _v645_flatness_1d(x: np.ndarray) -> float:
    try:
        arr = np.asarray(x, dtype=np.float32).reshape(-1)
        n = int(min(arr.size, _env_int("BUSY_BAMIX_V645_FLATNESS_FFT_N", 8192, minimum=2048, maximum=32768)))
        if n < 512 or _rms(arr[:n]) < 1e-10:
            return 0.0
        seg = arr[:n].astype(np.float64, copy=False)
        seg = (seg - float(np.mean(seg))) * np.hanning(n)
        mag = np.abs(np.fft.rfft(seg)) + 1e-12
        return float(np.clip(math.exp(float(np.mean(np.log(mag)))) / (float(np.mean(mag)) + 1e-12), 0.0, 1.0))
    except Exception:
        return 0.0


def _v645_crest_db(x: np.ndarray) -> float:
    try:
        arr = np.asarray(x, dtype=np.float32)
        return float(_db(_peak(arr) / max(_rms(arr), 1e-12)))
    except Exception:
        return 0.0


def _v645_flux_proxy(x: np.ndarray) -> float:
    try:
        arr = _ensure_stereo(np.asarray(x, dtype=np.float32))
        if arr.shape[0] < 3 or _rms(arr) < 1e-10:
            return 0.0
        mono = np.mean(arr, axis=1).astype(np.float32, copy=False)
        diff = np.diff(mono, prepend=mono[:1]).astype(np.float32, copy=False)
        return float(np.clip(_rms(diff) / max(_rms(mono), 1e-12), 0.0, 8.0))
    except Exception:
        return 0.0


def _v645_erb_ms_dynamic_resonance_suppressor_mix(
    mix: np.ndarray,
    *,
    sr: int,
    intensity: float,
    state: dict[str, Any],
    residue_pressure: dict[str, Any] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or not _env_on("BUSY_BAMIX_V645_ERB_MS_RESONANCE_SUPPRESSOR", "1"):
        state.update({"active": False, "applied": False, "reason": "disabled"})
        return mix, state
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    if y.size == 0 or _rms(y) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        state.update({"active": True, "applied": False, "method": "v645_erb_ms_resonance_bypass_silent"})
        return y, state

    pressure = residue_pressure if isinstance(residue_pressure, dict) else {}
    pressure_score = float(pressure.get("residue_pressure") or 0.0) if bool(pressure.get("active")) else 0.0
    side_pressure = float(pressure.get("effective_side_hf_hash_pressure", pressure.get("side_hf_hash_pressure")) or 0.0) if bool(pressure.get("active")) else 0.0
    mid = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    side = ((y[:, 0] - y[:, 1]) * 0.5).astype(np.float32, copy=False)
    mid_orig = mid.copy()
    side_orig = side.copy()
    corr_pre = _corr(y)
    peak_pre = _peak(y)
    rms_pre = _rms(y)
    crest_pre = _v645_crest_db(y)
    flux_pre = _v645_flux_proxy(y)

    bands = [
        (2800.0, 3800.0),
        (3800.0, 5200.0),
        (5200.0, 7200.0),
        (7200.0, 9500.0),
    ]
    reports: list[dict[str, Any]] = []
    applied_any = False
    intensity_s = float(np.clip(float(intensity), 0.0, 1.0))
    min_risk = _env_float("BUSY_BAMIX_V645_ERB_MIN_RISK", 0.18, minimum=0.0, maximum=0.8)
    max_mid_cut = _env_float("BUSY_BAMIX_V645_ERB_MID_MAX_CUT_DB", 0.85, minimum=0.0, maximum=3.0)
    max_side_cut = _env_float("BUSY_BAMIX_V645_ERB_SIDE_MAX_CUT_DB", 2.15, minimum=0.0, maximum=5.0)

    mid_ref, state = _stateful_sos_filter_1d(mid_orig, state, "v645_mid_presence_ref", sr=sr, btype="bandpass", cutoff=[2500.0, 9800.0], order=2)
    side_ref, state = _stateful_sos_filter_1d(side_orig, state, "v645_side_presence_ref", sr=sr, btype="bandpass", cutoff=[2500.0, 9800.0], order=2)
    mid_ref_rms = _rms(mid_ref)
    side_ref_rms = _rms(side_ref)

    for idx, (lo, hi) in enumerate(bands, start=1):
        if hi >= float(sr) * 0.48:
            continue
        for lane_name in ("mid", "side"):
            src = mid_orig if lane_name == "mid" else side_orig
            ref = mid_ref_rms if lane_name == "mid" else side_ref_rms
            band, state = _stateful_sos_filter_1d(src, state, f"v645_{lane_name}_erb_band_{idx}", sr=sr, btype="bandpass", cutoff=[lo, hi], order=2)
            brms = _rms(band)
            if brms < 1e-10:
                continue
            concentration = float(np.clip((_db(brms + 1e-12) - _db(ref + 1e-12) + 13.0) / 18.0, 0.0, 1.0))
            flat = _v645_flatness_1d(band)
            lane_pressure = side_pressure if lane_name == "side" else max(0.0, pressure_score - side_pressure * 0.25)
            risk = float(np.clip(0.42 * concentration + 0.28 * flat + 0.30 * lane_pressure, 0.0, 1.0))
            if risk <= min_risk:
                reports.append({"lane": lane_name, "band_hz": [lo, hi], "risk": round(risk, 4), "applied": False})
                continue
            max_cut = max_side_cut if lane_name == "side" else max_mid_cut
            cut_db = -max_cut * intensity_s * float(np.clip((risk - min_risk) / max(1.0 - min_risk, 1e-6), 0.0, 1.0))
            if lane_name == "mid" and side_pressure > pressure_score * 0.75:
                cut_db *= 0.55
            gain = np.float32(_amp(cut_db) - 1.0)
            if lane_name == "mid":
                mid = (mid + band * gain).astype(np.float32, copy=False)
            else:
                side = (side + band * gain).astype(np.float32, copy=False)
            applied_any = True
            reports.append({
                "lane": lane_name,
                "band_hz": [round(lo, 1), round(hi, 1)],
                "risk": round(risk, 4),
                "concentration": round(concentration, 4),
                "flatness": round(flat, 4),
                "cut_db": round(float(cut_db), 3),
                "applied": True,
            })

    candidate = np.stack([mid + side, mid - side], axis=1).astype(np.float32, copy=False)
    corr_post = _corr(candidate)
    peak_post = _peak(candidate)
    rms_post = _rms(candidate)
    crest_post = _v645_crest_db(candidate)
    flux_post = _v645_flux_proxy(candidate)
    rollback_reasons: list[str] = []
    if peak_pre > 1e-8 and peak_post > peak_pre * _amp(_env_float("BUSY_BAMIX_V645_ERB_MAX_PEAK_BUMP_DB", 0.03, minimum=0.0, maximum=0.4)):
        rollback_reasons.append("peak_bump_guard")
    if rms_pre > 1e-9 and _db(rms_post + 1e-12) - _db(rms_pre + 1e-12) < -_env_float("BUSY_BAMIX_V645_ERB_MAX_RMS_LOSS_DB", 0.42, minimum=0.0, maximum=2.0):
        rollback_reasons.append("rms_loss_guard")
    if crest_post < crest_pre - _env_float("BUSY_BAMIX_V645_ERB_MAX_CREST_LOSS_DB", 0.18, minimum=0.0, maximum=1.2):
        rollback_reasons.append("crest_loss_guard")
    if flux_pre > 1e-8 and flux_post / max(flux_pre, 1e-12) < _env_float("BUSY_BAMIX_V645_ERB_MIN_FLUX_RATIO", 0.72, minimum=0.2, maximum=1.0):
        rollback_reasons.append("hf_flux_flattening_guard")
    if corr_post < corr_pre - _env_float("BUSY_BAMIX_V645_ERB_MAX_CORR_DROP", 0.08, minimum=0.0, maximum=0.5):
        rollback_reasons.append("correlation_drop_guard")
    if rollback_reasons:
        candidate = (y + (candidate - y) * np.float32(_env_float("BUSY_BAMIX_V645_ERB_ROLLBACK_BLEND", 0.35, minimum=0.0, maximum=1.0))).astype(np.float32, copy=False)
        corr_post = _corr(candidate)
        peak_post = _peak(candidate)
        rms_post = _rms(candidate)
        crest_post = _v645_crest_db(candidate)
        flux_post = _v645_flux_proxy(candidate)

    if applied_any:
        state = _augmentation_mark_block(state, applied=True)
    else:
        state = _augmentation_mark_block(state, applied=False, reason="erb_resonance_risk_below_threshold")
    state.update({
        "active": True,
        "applied": bool(applied_any),
        "intensity": round(float(intensity_s), 4),
        "residue_pressure": pressure,
        "band_reports": reports[:16],
        "rollback_reasons": rollback_reasons,
        "correlation_pre": round(float(corr_pre), 4),
        "correlation_post": round(float(corr_post), 4),
        "peak_delta_db": round(float(_db(peak_post + 1e-12) - _db(peak_pre + 1e-12)), 3),
        "rms_delta_db": round(float(_db(rms_post + 1e-12) - _db(rms_pre + 1e-12)), 3),
        "crest_delta_db": round(float(crest_post - crest_pre), 3),
        "flux_ratio": round(float(flux_post / max(flux_pre, 1e-12)) if flux_pre > 1e-12 else 1.0, 4),
        "method": "v645_erb_scaled_mid_side_dynamic_resonance_suppression",
        "policy": "pre-master deterministic dynamic EQ; retained signal energy only, no spectral subtraction or phase-inverted cancellation",
    })
    return np.nan_to_num(candidate, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _v645_upward_body_density_polynomial_mix(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    if intensity <= 0.0 or not _env_on("BUSY_BAMIX_V645_UPWARD_BODY_POLYNOMIAL_DENSITY", "1"):
        state.update({"active": False, "applied": False, "reason": "disabled"})
        return mix, state
    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    if y.size == 0 or _rms(y) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        return y, state
    mid = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    side = ((y[:, 0] - y[:, 1]) * 0.5).astype(np.float32, copy=False)
    body, state = _stateful_sos_filter_1d(mid, state, "v645_body_density_band", sr=sr, btype="bandpass", cutoff=[150.0, 520.0], order=2)
    if _rms(body) < 1e-10:
        state = _augmentation_mark_block(state, applied=False, reason="no_body_band_energy")
        return y, state
    rect = np.abs(body)
    p80 = float(np.percentile(rect, 80.0)) if rect.size else 0.0
    p35 = float(np.percentile(rect, 35.0)) if rect.size else 0.0
    if p80 <= 1e-9:
        state = _augmentation_mark_block(state, applied=False, reason="body_detector_too_low")
        return y, state
    upward = np.clip((p80 - rect) / max(p80 - p35, 1e-9), 0.0, 1.0).astype(np.float32)
    norm = body / np.float32(max(float(np.percentile(np.abs(body), 98.0)), 1e-8))
    h2 = (2.0 * norm * norm - 1.0).astype(np.float32)
    h3 = (norm * norm * norm).astype(np.float32)
    # Even-order polynomial density can create a tiny DC component.  Re-center
    # and re-bandlimit the derived layer so this remains body density, not a
    # hidden offset or sub boost.
    h2 = (h2 - np.float32(np.mean(h2))).astype(np.float32, copy=False)
    h3 = (h3 - np.float32(np.mean(h3))).astype(np.float32, copy=False)
    raw_layer = (body * (0.42 + 0.58 * upward) + h2 * np.float32(_rms(body) * 0.18) + h3 * np.float32(_rms(body) * 0.22)).astype(np.float32, copy=False)
    layer, state = _stateful_sos_filter_1d(raw_layer, state, "v645_body_density_layer_bandlimit", sr=sr, btype="bandpass", cutoff=[130.0, 620.0], order=2)
    layer = (layer - np.float32(np.mean(layer))).astype(np.float32, copy=False)
    blend_db = _env_float("BUSY_BAMIX_V645_BODY_DENSITY_BLEND_DB", -20.5, minimum=-34.0, maximum=-10.0) + 3.0 * float(np.clip(intensity, 0.0, 1.0))
    wet = layer * np.float32(_amp(blend_db))
    candidate_mid = (mid + wet).astype(np.float32, copy=False)
    candidate = np.stack([candidate_mid + side, candidate_mid - side], axis=1).astype(np.float32, copy=False)
    peak_pre = _peak(y)
    crest_pre = _v645_crest_db(y)
    peak_post = _peak(candidate)
    peak_trim_db = 0.0
    if peak_pre > 1e-8 and peak_post > peak_pre * _amp(_env_float("BUSY_BAMIX_V645_BODY_MAX_PEAK_BUMP_DB", 0.08, minimum=0.0, maximum=0.6)):
        scale = (peak_pre * _amp(_env_float("BUSY_BAMIX_V645_BODY_MAX_PEAK_BUMP_DB", 0.08, minimum=0.0, maximum=0.6))) / max(peak_post, 1e-8)
        candidate *= np.float32(scale)
        peak_trim_db = _db(float(scale))
    candidate_mid_final = ((candidate[:, 0] + candidate[:, 1]) * 0.5).astype(np.float32, copy=False)
    crest_post = _v645_crest_db(candidate)
    sub_pre, state = _stateful_sos_filter_1d(mid, state, "v645_body_validation_sub_pre", sr=sr, btype="lowpass", cutoff=115.0, order=2)
    sub_post, state = _stateful_sos_filter_1d(candidate_mid_final, state, "v645_body_validation_sub_post", sr=sr, btype="lowpass", cutoff=115.0, order=2)
    body_post, state = _stateful_sos_filter_1d(candidate_mid_final, state, "v645_body_validation_body_post", sr=sr, btype="bandpass", cutoff=[150.0, 520.0], order=2)
    sub_delta_db = float(_db(_rms(sub_post) + 1e-12) - _db(_rms(sub_pre) + 1e-12))
    body_delta_db = float(_db(_rms(body_post) + 1e-12) - _db(_rms(body) + 1e-12))
    validation_reasons: list[str] = []
    if crest_post < crest_pre - _env_float("BUSY_BAMIX_V645_BODY_MAX_CREST_LOSS_DB", 0.28, minimum=0.0, maximum=1.5):
        validation_reasons.append("crest_loss_guard")
    if sub_delta_db > _env_float("BUSY_BAMIX_V645_BODY_MAX_SUB_DELTA_DB", 0.20, minimum=0.0, maximum=1.5):
        validation_reasons.append("sub_growth_guard")
    if body_delta_db > _env_float("BUSY_BAMIX_V645_BODY_MAX_BODY_DELTA_DB", 0.72, minimum=0.05, maximum=3.0):
        validation_reasons.append("low_mid_overfill_guard")
    if validation_reasons:
        blend = np.float32(_env_float("BUSY_BAMIX_V645_BODY_ROLLBACK_BLEND", 0.40, minimum=0.0, maximum=1.0))
        candidate = (y + (candidate - y) * blend).astype(np.float32, copy=False)
        crest_post = _v645_crest_db(candidate)
    state = _augmentation_mark_block(state, applied=True)
    state.update({
        "active": True,
        "applied": True,
        "intensity": round(float(intensity), 4),
        "blend_db": round(float(blend_db), 3),
        "peak_trim_db": round(float(peak_trim_db), 3),
        "validation_rollback_reasons": validation_reasons,
        "crest_delta_db": round(float(crest_post - crest_pre), 3),
        "sub_delta_db": round(float(sub_delta_db), 3),
        "body_delta_db": round(float(body_delta_db), 3),
        "body_rms_db": round(_db(_rms(body)), 3),
        "wet_rms_db": round(_db(_rms(wet)), 3),
        "upward_active_ratio": round(float(np.mean(upward > 0.15)) if upward.size else 0.0, 4),
        "method": "v645_upward_low_mid_body_density_with_polynomial_existing_energy_layer",
        "policy": "density is derived from existing mid/body energy only; no resynthesis, no external generation",
    })
    return np.nan_to_num(candidate, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _augment_side_texture_control_mix(mix: np.ndarray, *, sr: int, intensity: float, state: dict[str, Any], recipe: str | None = None, residue_pressure: dict[str, Any] | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """v63.9.0 side texture / stereo cleanliness final form.

    This is a side-only, band-limited dynamic suppressor for AI/Suno-style
    side-high hash/fizz.  It is not a broad high-shelf cut and not a width
    reducer: the center/lead brightness lane is measured separately, low-side
    mono anchoring remains owned by v63.8, and suppression can be downscaled or
    rolled back when ambience, mono fold-down, crest, or peak guards object.
    """
    if intensity <= 0.0 or not _env_on("BUSY_BAMIX_V6390_SIDE_TEXTURE_STEREO_CLEANLINESS", "1"):
        state.update({"active": False, "applied": False, "last_block_applied": False, "reason": "disabled"})
        return mix, state
    if mix.size == 0 or _rms(mix) < 1e-8:
        state = _augmentation_mark_block(state, applied=False, reason="silent_block")
        state.update({"active": True, "applied": False, "method": "v6390_side_texture_bypass_silent"})
        return mix, state

    y = _ensure_stereo(mix).astype(np.float32, copy=False)
    corr_pre = _corr(y)
    mid = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    side = ((y[:, 0] - y[:, 1]) * 0.5).astype(np.float32, copy=False)
    side_rms = _rms(side)
    mid_rms = _rms(mid)
    if side_rms < _amp(_env_float("BUSY_BAMIX_V6390_SIDE_TEXTURE_MIN_SIDE_RMS_DB", -74.0, minimum=-110.0, maximum=-36.0)):
        state = _augmentation_mark_block(state, applied=False, reason="side_channel_too_quiet")
        state.update({
            "active": True,
            "applied": bool(state.get("applied", False)),
            "side_rms_db": round(_db(side_rms), 3),
            "method": "v6390_side_only_dynamic_hash_fizz_suppressor_bypass_quiet_side",
            "side_high_hash_fizz_suppressor": {"active": True, "applied": False, "reason": "side_channel_too_quiet"},
        })
        return y, state

    def _bounded_flatness(x: np.ndarray) -> float:
        try:
            arr = np.asarray(x, dtype=np.float32).reshape(-1)
            n = int(min(arr.size, _env_int("BUSY_BAMIX_V6390_FLATNESS_FFT_N", 16384, minimum=2048, maximum=65536)))
            if n < 512 or _rms(arr[:n]) < 1e-9:
                return 0.0
            seg = arr[:n].astype(np.float64, copy=False)
            seg = seg - float(np.mean(seg))
            if n >= 32:
                seg = seg * np.hanning(n)
            mag = np.abs(np.fft.rfft(seg)) + 1e-12
            return float(np.clip(math.exp(float(np.mean(np.log(mag)))) / (float(np.mean(mag)) + 1e-12), 0.0, 1.0))
        except Exception:
            return 0.0

    # Band selection is side-relative and input-relative.  Choose the roughest
    # high-side band instead of darkening the entire top end.
    bands = [
        (_env_float("BUSY_BAMIX_V6390_BAND1_LO_HZ", 5000.0, minimum=3500.0, maximum=8000.0), _env_float("BUSY_BAMIX_V6390_BAND1_HI_HZ", 8000.0, minimum=5500.0, maximum=11000.0)),
        (_env_float("BUSY_BAMIX_V6390_BAND2_LO_HZ", 8000.0, minimum=5500.0, maximum=11000.0), _env_float("BUSY_BAMIX_V6390_BAND2_HI_HZ", 11000.0, minimum=8000.0, maximum=14500.0)),
        (_env_float("BUSY_BAMIX_V6390_BAND3_LO_HZ", 11000.0, minimum=8000.0, maximum=14500.0), _env_float("BUSY_BAMIX_V6390_BAND3_HI_HZ", 14000.0, minimum=10500.0, maximum=19000.0)),
    ]
    selected: dict[str, Any] | None = None
    side_high_total, state = _stateful_sos_filter_1d(side, state, "v6390_side_high_total", sr=sr, btype="bandpass", cutoff=[bands[0][0], bands[-1][1]], order=2)
    mid_high_total, state = _stateful_sos_filter_1d(mid, state, "v6390_mid_high_total", sr=sr, btype="bandpass", cutoff=[bands[0][0], bands[-1][1]], order=2)
    side_high_total_rms = _rms(side_high_total)
    mid_high_total_rms = _rms(mid_high_total)
    for idx, (lo, hi) in enumerate(bands, start=1):
        if hi <= lo + 200.0 or lo >= float(sr) * 0.48:
            continue
        hi = min(float(hi), float(sr) * 0.48)
        side_band, state = _stateful_sos_filter_1d(side, state, f"v6390_side_hash_band_{idx}", sr=sr, btype="bandpass", cutoff=[lo, hi], order=2)
        mid_band, state = _stateful_sos_filter_1d(mid, state, f"v6390_mid_hash_band_{idx}", sr=sr, btype="bandpass", cutoff=[lo, hi], order=2)
        srms = _rms(side_band)
        mrms = _rms(mid_band)
        side_mid_excess_db = _db(srms + 1e-12) - _db(mrms + 1e-12)
        side_total_fraction = float(np.clip(srms / max(side_high_total_rms, 1e-12), 0.0, 2.0))
        flat = _bounded_flatness(side_band)
        # High spectral flatness and side dominance are hash/fizz evidence; a
        # strong mid band is treated as lead/vocal/center brightness protection.
        excess_need = float(np.clip((side_mid_excess_db - _env_float("BUSY_BAMIX_V6390_HASH_EXCESS_START_DB", 1.0, minimum=-4.0, maximum=8.0)) / _env_float("BUSY_BAMIX_V6390_HASH_EXCESS_RANGE_DB", 7.0, minimum=1.0, maximum=18.0), 0.0, 1.0))
        flat_need = float(np.clip((flat - _env_float("BUSY_BAMIX_V6390_HASH_FLATNESS_START", 0.20, minimum=0.02, maximum=0.8)) / _env_float("BUSY_BAMIX_V6390_HASH_FLATNESS_RANGE", 0.45, minimum=0.05, maximum=0.95), 0.0, 1.0))
        fraction_need = float(np.clip((side_total_fraction - _env_float("BUSY_BAMIX_V6390_HASH_FRACTION_START", 0.22, minimum=0.02, maximum=0.9)) / _env_float("BUSY_BAMIX_V6390_HASH_FRACTION_RANGE", 0.55, minimum=0.05, maximum=1.5), 0.0, 1.0))
        center_brightness_guard = float(np.clip((_db(mrms + 1e-12) - _db(srms + 1e-12) + _env_float("BUSY_BAMIX_V6390_CENTER_BRIGHTNESS_GUARD_DB", 1.25, minimum=-3.0, maximum=8.0)) / 8.0, 0.0, 1.0))
        risk = float(np.clip(0.52 * excess_need + 0.30 * flat_need + 0.18 * fraction_need, 0.0, 1.0))
        risk *= float(1.0 - 0.45 * center_brightness_guard)
        if selected is None or risk > float(selected.get("hash_risk", 0.0)):
            selected = {
                "band_index": idx,
                "lo_hz": float(lo),
                "hi_hz": float(hi),
                "side_band": side_band,
                "mid_band_rms": mrms,
                "side_band_rms": srms,
                "side_mid_excess_db": side_mid_excess_db,
                "side_total_fraction": side_total_fraction,
                "spectral_flatness": flat,
                "center_brightness_guard": center_brightness_guard,
                "hash_risk": risk,
                "raw_hash_risk": float(np.clip(0.52 * excess_need + 0.30 * flat_need + 0.18 * fraction_need, 0.0, 1.0)),
            }

    if not selected:
        state = _augmentation_mark_block(state, applied=False, reason="no_valid_side_high_band")
        state.update({"active": True, "applied": bool(state.get("applied", False)), "method": "v6390_side_only_dynamic_hash_fizz_suppressor_no_valid_band"})
        return y, state

    risk = float(selected.get("hash_risk") or 0.0)
    pressure = residue_pressure if isinstance(residue_pressure, dict) else {}
    pressure_floor = 0.0
    if bool(pressure.get("active")) and _env_on("BUSY_BAMIX_V645_SIDE_TEXTURE_WITNESS_FLOOR", "1"):
        pressure_floor = float(np.clip(
            0.56 * float(pressure.get("effective_side_hf_hash_pressure", pressure.get("side_hf_hash_pressure")) or 0.0)
            + 0.25 * float(pressure.get("effective_fakeprint_pressure", pressure.get("fakeprint_pressure")) or 0.0)
            + 0.19 * float(pressure.get("effective_hf_floor_pressure", pressure.get("hf_floor_pressure")) or 0.0),
            0.0,
            _env_float("BUSY_BAMIX_V645_SIDE_TEXTURE_MAX_WITNESS_FLOOR", 0.58, minimum=0.0, maximum=1.0),
        ))
        if pressure_floor > risk:
            selected["pre_v645_hash_risk"] = round(float(risk), 4)
            risk = pressure_floor
            selected["hash_risk"] = risk
            selected["raw_hash_risk"] = max(float(selected.get("raw_hash_risk") or 0.0), pressure_floor)
    min_risk = _env_float("BUSY_BAMIX_V6390_HASH_MIN_RISK", 0.10, minimum=0.0, maximum=0.6)
    if risk <= min_risk:
        state = _augmentation_mark_block(state, applied=False, reason="side_hash_fizz_risk_below_threshold")
        state.update({
            "active": True,
            "applied": bool(state.get("applied", False)),
            "need_score": round(float(risk), 4),
            "hash_risk": round(float(risk), 4),
            "v645_residue_witness_floor": round(float(pressure_floor), 4),
            "v645_residue_pressure": pressure,
            "side_high_over_mid_high_db": round(float(_db(side_high_total_rms + 1e-12) - _db(mid_high_total_rms + 1e-12)), 3),
            "correlation_pre": round(float(corr_pre), 4),
            "side_high_hash_fizz_suppressor": {
                "active": True,
                "applied": False,
                "detected_band_hz": [round(float(selected.get("lo_hz")), 1), round(float(selected.get("hi_hz")), 1)],
                "hash_risk": round(float(risk), 4),
                "suppression_db": 0.0,
                "reason": "side_hash_fizz_risk_below_threshold",
            },
            "method": "v6390_side_only_dynamic_hash_fizz_suppressor_guarded_bypass",
        })
        return y, state

    intensity_s = float(np.clip(float(intensity), 0.0, 1.0))
    max_supp_db = _env_float("BUSY_BAMIX_V6390_HASH_MAX_SUPPRESSION_DB", 3.2, minimum=0.25, maximum=7.5)
    suppression_db = -float(max_supp_db) * intensity_s * (0.35 + 0.65 * risk)
    # Center/lead protection: if the same band is also strong in the mid lane,
    # reduce side suppression rather than darkening the whole master.
    selected_center_guard = float(selected.get("center_brightness_guard") or 0.0)
    total_center_guard = float(np.clip((_db(mid_high_total_rms + 1e-12) - _db(side_high_total_rms + 1e-12) + _env_float("BUSY_BAMIX_V6390_CENTER_BRIGHTNESS_GUARD_DB", 1.25, minimum=-3.0, maximum=8.0)) / 8.0, 0.0, 1.0))
    center_guard = float(max(selected_center_guard, total_center_guard))
    center_protection_active = bool(center_guard > _env_float("BUSY_BAMIX_V6390_CENTER_PROTECTION_ACTIVE_AT", 0.25, minimum=0.0, maximum=1.0))
    if center_protection_active:
        suppression_db *= float(1.0 - _env_float("BUSY_BAMIX_V6390_CENTER_PROTECTION_DOWNSCALE", 0.42, minimum=0.0, maximum=0.85) * center_guard)
    suppression_db = float(np.clip(suppression_db, -_env_float("BUSY_BAMIX_V6390_HASH_HARD_SUPPRESSION_LIMIT_DB", 3.8, minimum=0.5, maximum=8.0), 0.0))

    side_band = np.asarray(selected["side_band"], dtype=np.float32)
    band_abs = np.abs(side_band)
    det_rms = _rms(side_band)
    detector_floor = max(det_rms * _env_float("BUSY_BAMIX_V6390_DETECTOR_RMS_MULT", 0.78, minimum=0.15, maximum=3.0), 1e-9)
    attack_ms = _env_float("BUSY_BAMIX_V6390_SUPPRESS_ATTACK_MS", 4.0, minimum=0.5, maximum=50.0)
    release_ms = _env_float("BUSY_BAMIX_V6390_SUPPRESS_RELEASE_MS", 85.0, minimum=10.0, maximum=500.0)
    alpha_a = math.exp(-1.0 / max(float(sr) * attack_ms / 1000.0, 1.0))
    alpha_r = math.exp(-1.0 / max(float(sr) * release_ms / 1000.0, 1.0))
    env = float(state.get("v6390_hash_env", detector_floor))
    env_arr = np.empty_like(band_abs, dtype=np.float32)
    for i, x in enumerate(band_abs):
        xv = float(x)
        if xv > env:
            env = alpha_a * env + (1.0 - alpha_a) * xv
        else:
            env = alpha_r * env + (1.0 - alpha_r) * xv
        env_arr[i] = env
    state["v6390_hash_env"] = float(env)
    dyn = np.clip((env_arr / np.float32(detector_floor)) - 0.72, 0.0, 1.35) / 1.35
    gain_db = suppression_db * (0.28 + 0.72 * dyn)
    gain = np.power(10.0, gain_db / 20.0).astype(np.float32)
    processed_side = (side + side_band * (gain - 1.0)).astype(np.float32, copy=False)
    out = np.stack([mid + processed_side, mid - processed_side], axis=1).astype(np.float32, copy=False)

    def _side_loss_db(candidate_side: np.ndarray) -> float:
        return _db(_rms(candidate_side) + 1e-12) - _db(side_rms + 1e-12)

    corr_candidate = _corr(out)
    total_side_change_db = _side_loss_db(processed_side)
    high_side_after = side_band * gain
    high_side_change_db = _db(_rms(high_side_after) + 1e-12) - _db(float(selected.get("side_band_rms") or 0.0) + 1e-12)
    mono_pre = ((y[:, 0] + y[:, 1]) * 0.5).astype(np.float32, copy=False)
    mono_post = ((out[:, 0] + out[:, 1]) * 0.5).astype(np.float32, copy=False)
    mono_delta_db = _db(_rms(mono_post - mono_pre) + 1e-12) - _db(_rms(mono_pre) + 1e-12)
    corr_delta = corr_candidate - corr_pre
    ambience_risk = float(np.clip((abs(min(total_side_change_db, 0.0)) - _env_float("BUSY_BAMIX_V6390_AMBIENCE_SIDE_LOSS_START_DB", 0.85, minimum=0.0, maximum=4.0)) / _env_float("BUSY_BAMIX_V6390_AMBIENCE_SIDE_LOSS_RANGE_DB", 2.0, minimum=0.25, maximum=8.0), 0.0, 1.0))
    if corr_delta > _env_float("BUSY_BAMIX_V6390_AMBIENCE_CORR_JUMP_DB", 0.16, minimum=0.0, maximum=0.8):
        ambience_risk = max(ambience_risk, float(np.clip(corr_delta / 0.45, 0.0, 1.0)))
    mono_delta_start_db = _env_float("BUSY_BAMIX_V6390_MONO_DELTA_START_DB", -72.0, minimum=-100.0, maximum=-30.0)
    mono_risk = float(np.clip((mono_delta_db - mono_delta_start_db) / 24.0, 0.0, 1.0))
    if corr_candidate < _env_float("BUSY_BAMIX_V6390_MONO_CORR_RISK_AT", -0.05, minimum=-0.8, maximum=0.6):
        mono_risk = max(mono_risk, float(np.clip((-0.05 - corr_candidate) / 0.55, 0.0, 1.0)))

    rollback_reasons: list[str] = []
    rollback_scale = 1.0
    if ambience_risk >= _env_float("BUSY_BAMIX_V6390_AMBIENCE_ROLLBACK_AT", 0.48, minimum=0.0, maximum=1.0):
        rollback_reasons.append("ambience_collapse_risk")
        rollback_scale = min(rollback_scale, _env_float("BUSY_BAMIX_V6390_AMBIENCE_ROLLBACK_SCALE", 0.52, minimum=0.0, maximum=1.0))
    if mono_risk >= _env_float("BUSY_BAMIX_V6390_MONO_ROLLBACK_AT", 0.42, minimum=0.0, maximum=1.0):
        rollback_reasons.append("mono_fold_down_compatibility_risk")
        rollback_scale = min(rollback_scale, _env_float("BUSY_BAMIX_V6390_MONO_ROLLBACK_SCALE", 0.55, minimum=0.0, maximum=1.0))
    # Peak and crest/LRA proxy guard: do not let side cleanup increase bus peak
    # or make block RMS notably denser.  This protects later limiter workload.
    pk_in = _peak(y)
    pk_out = _peak(out)
    peak_trim_db = 0.0
    if pk_in > 1e-8 and pk_out > pk_in * _amp(_env_float("BUSY_BAMIX_V6390_MAX_PEAK_BUMP_DB", 0.04, minimum=0.0, maximum=0.5)):
        rollback_reasons.append("side_texture_peak_bump_guard")
        scale = (pk_in * _amp(_env_float("BUSY_BAMIX_V6390_MAX_PEAK_BUMP_DB", 0.04, minimum=0.0, maximum=0.5))) / max(pk_out, 1e-8)
        out *= np.float32(scale)
        peak_trim_db = _db(float(scale))
    if rollback_scale < 0.999:
        scaled_gain_db = gain_db * np.float32(rollback_scale)
        scaled_gain = np.power(10.0, scaled_gain_db / 20.0).astype(np.float32)
        processed_side = (side + side_band * (scaled_gain - 1.0)).astype(np.float32, copy=False)
        out = np.stack([mid + processed_side, mid - processed_side], axis=1).astype(np.float32, copy=False)
        corr_candidate = _corr(out)
        total_side_change_db = _side_loss_db(processed_side)
        high_side_after = side_band * scaled_gain
        high_side_change_db = _db(_rms(high_side_after) + 1e-12) - _db(float(selected.get("side_band_rms") or 0.0) + 1e-12)
        suppression_db *= float(rollback_scale)
        mono_post = ((out[:, 0] + out[:, 1]) * 0.5).astype(np.float32, copy=False)
        mono_delta_db = _db(_rms(mono_post - mono_pre) + 1e-12) - _db(_rms(mono_pre) + 1e-12)

    applied = bool(abs(float(suppression_db)) > 0.03 and risk > min_risk)
    if applied:
        state = _augmentation_mark_block(state, applied=True)
    else:
        state = _augmentation_mark_block(state, applied=False, reason="suppression_downscaled_to_zero")
    side_width_ratio = float(_rms(processed_side) / max(side_rms, 1e-12))
    rollback_reason = "+".join(rollback_reasons) if rollback_reasons else "none"
    affected_band = [round(float(selected.get("lo_hz")), 1), round(float(selected.get("hi_hz")), 1)]
    state.update({
        "active": True,
        "applied": applied,
        "intensity": round(float(intensity_s), 4),
        "need_score": round(float(risk), 4),
        "hash_risk": round(float(risk), 4),
        "raw_hash_risk": round(float(selected.get("raw_hash_risk") or risk), 4),
        "v645_residue_witness_floor": round(float(pressure_floor), 4),
        "v645_residue_pressure": pressure,
        "suppression_db": round(float(suppression_db), 3),
        "duck_db": round(float(suppression_db), 3),
        "detected_band_hz": affected_band,
        "hpf_hz": affected_band[0],
        "side_high_over_mid_high_db": round(float(_db(side_high_total_rms + 1e-12) - _db(mid_high_total_rms + 1e-12)), 3),
        "selected_band_side_mid_excess_db": round(float(selected.get("side_mid_excess_db") or 0.0), 3),
        "selected_band_spectral_flatness": round(float(selected.get("spectral_flatness") or 0.0), 4),
        "side_hi_rms_db": round(_db(side_high_total_rms), 3),
        "mid_hi_rms_db": round(_db(mid_high_total_rms), 3),
        "correlation_pre": round(float(corr_pre), 4),
        "correlation_post": round(float(corr_candidate), 4),
        "peak_trim_db": round(float(peak_trim_db), 3),
        "total_side_change_db": round(float(total_side_change_db), 3),
        "high_side_change_db": round(float(high_side_change_db), 3),
        "width_change_ratio": round(float(side_width_ratio), 4),
        "rollback_reason": rollback_reason,
        "side_high_hash_fizz_suppressor": {
            "active": True,
            "applied": applied,
            "detected_band_hz": affected_band,
            "hash_risk": round(float(risk), 4),
            "suppression_db": round(float(suppression_db), 3),
            "side_mid_excess_db": round(float(selected.get("side_mid_excess_db") or 0.0), 3),
            "spectral_flatness": round(float(selected.get("spectral_flatness") or 0.0), 4),
            "method": "side_only_band_limited_dynamic_eq_not_broad_high_shelf",
        },
        "stereo_cleanliness_guard": {
            "active": True,
            "side_incoherence_risk": round(float(np.clip((0.22 - corr_pre) / 0.72, 0.0, 1.0)) if corr_pre < 0.22 else 0.0, 4),
            "center_protection_active": bool(center_protection_active),
            "center_brightness_guard": round(float(center_guard), 4),
            "width_change_db_or_ratio": {"total_side_change_db": round(float(total_side_change_db), 3), "high_side_change_db": round(float(high_side_change_db), 3), "side_width_ratio": round(float(side_width_ratio), 4)},
            "protection_reason": "center_or_vocal_brightness_present" if center_protection_active else "side_hash_dominates_mid_brightness",
        },
        "ambience_collapse_detection": {
            "active": True,
            "risk": round(float(ambience_risk), 4),
            "rollback_active": bool("ambience_collapse_risk" in rollback_reasons),
            "rollback_reason": "ambience_collapse_risk" if "ambience_collapse_risk" in rollback_reasons else "none",
            "side_loss_db": round(float(total_side_change_db), 3),
            "correlation_delta": round(float(corr_candidate - corr_pre), 4),
        },
        "mono_fold_down_rollback": {
            "active": True,
            "compatibility_risk": round(float(mono_risk), 4),
            "rollback_active": bool("mono_fold_down_compatibility_risk" in rollback_reasons),
            "rollback_reason": "mono_fold_down_compatibility_risk" if "mono_fold_down_compatibility_risk" in rollback_reasons else "none",
            "mono_delta_db": round(float(mono_delta_db), 3),
            "correlation_post": round(float(corr_candidate), 4),
        },
        "guards": {
            "center_vocal_brightness_protection": bool(center_protection_active),
            "ambience_collapse_guard": True,
            "mono_fold_down_guard": True,
            "peak_guard_db": round(float(peak_trim_db), 3),
            "lra_crest_preservation_proxy": True,
            "rollback_scale": round(float(rollback_scale), 4),
            "rollback_reason": rollback_reason,
        },
        "method": "v6390_side_only_band_limited_dynamic_hash_fizz_suppressor_with_ambience_mono_rollback",
    })
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state

def _v6332_augmentation_bus_peak_guard(pre_mix: np.ndarray, post_mix: np.ndarray, state: dict[str, Any], *, post_gain_db: float = 0.0, tp_abs_target_dbfs: float | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Keep derived assist layers from stealing premaster headroom.

    v63.3.3 keeps both a last-block report and aggregate counters.  The older
    v63.3.2 telemetry could show only the last fade-out/silent block, making a
    run with active augmentation look like ``scale=0`` or ``applied=false``.
    The DSP behavior remains peak-neutral; this revision makes the runtime
    evidence trustworthy for Supabase/debug decisions.
    """
    if not isinstance(state, dict):
        state = {}
    if not _env_on("BUSY_BAMIX_V6332_AUG_BUS_PEAK_GUARD", "1"):
        state.update({"active": False, "reason": "disabled"})
        return post_mix, state
    pre = _ensure_stereo(pre_mix).astype(np.float32, copy=False)
    post = _ensure_stereo(post_mix).astype(np.float32, copy=False)
    if pre.size == 0 or post.size == 0 or pre.shape != post.shape:
        state.update({"active": False, "reason": "shape_mismatch_or_empty"})
        return post_mix, state

    delta = (post - pre).astype(np.float32, copy=False)
    # v63.3.5.2: evaluate the guard in the actual post-global-gain
    # premaster domain.  The previous pre-gain guard could scale useful assist
    # layers down to near-zero when the internal float summing bus was hot even
    # though the later global gain would have restored headroom.
    gain = _amp(float(post_gain_db))
    pre_ref = (pre * np.float32(gain)).astype(np.float32, copy=False)
    post_ref = (post * np.float32(gain)).astype(np.float32, copy=False)
    ref_delta = (post_ref - pre_ref).astype(np.float32, copy=False)
    pre_peak = _peak(pre_ref)
    post_peak = _peak(post_ref)
    delta_peak = _peak(ref_delta)
    delta_rms = _rms(ref_delta)
    max_bump_db = _env_float("BUSY_BAMIX_V6332_AUG_MAX_PEAK_BUMP_DB", 0.34, minimum=0.0, maximum=1.8)
    if tp_abs_target_dbfs is None:
        abs_guard_dbfs = _env_float("BUSY_BAMIX_V6335_2_AUG_GUARD_ABS_TP_DBFS", -0.95, minimum=-6.0, maximum=-0.25)
    else:
        try:
            abs_guard_dbfs = float(tp_abs_target_dbfs)
        except Exception:
            abs_guard_dbfs = _env_float("BUSY_BAMIX_V6335_2_AUG_GUARD_ABS_TP_DBFS", -0.95, minimum=-6.0, maximum=-0.25)
    abs_guard_peak = _amp(abs_guard_dbfs)
    target_peak = max(pre_peak * _amp(max_bump_db), abs_guard_peak, pre_peak + 1e-8)
    min_delta_rms_db = _env_float("BUSY_BAMIX_V6332_AUG_MIN_DELTA_RMS_DB", -96.0, minimum=-140.0, maximum=-48.0)

    def _agg_update(*, applied: bool, scale: float, reason: str, guarded: np.ndarray) -> None:
        try:
            prev_blocks = int(state.get("block_count") or 0)
        except Exception:
            prev_blocks = 0
        try:
            prev_applied = int(state.get("applied_block_count") or 0)
        except Exception:
            prev_applied = 0
        try:
            prev_below = int(state.get("delta_below_noise_block_count") or 0)
        except Exception:
            prev_below = 0
        below = bool(reason == "augmentation_delta_below_noise_floor")
        block_count = prev_blocks + 1
        applied_count = prev_applied + (1 if applied else 0)
        below_count = prev_below + (1 if below else 0)
        def _max_db(prev: Any, cur: float) -> float:
            try:
                if prev is None:
                    return float(cur)
                return max(float(prev), float(cur))
            except Exception:
                return float(cur)
        def _min_scale(prev: Any, cur: float) -> float:
            try:
                if prev is None:
                    return float(cur)
                return min(float(prev), float(cur))
            except Exception:
                return float(cur)
        guarded_ref = (_ensure_stereo(guarded).astype(np.float32, copy=False) * np.float32(gain)).astype(np.float32, copy=False)
        guarded_peak = _peak(guarded_ref)
        state.update({
            "active": True,
            "applied": bool(applied_count > 0),
            "block_count": block_count,
            "applied_block_count": applied_count,
            "applied_fraction": round(float(applied_count) / max(1.0, float(block_count)), 4),
            "delta_below_noise_block_count": below_count,
            "last_block_applied": bool(applied),
            "last_reason": reason,
            "last_scale": round(float(scale), 4),
            "scale": round(_min_scale(state.get("scale_min"), float(scale)), 4),
            "scale_min": round(_min_scale(state.get("scale_min"), float(scale)), 4),
            "max_peak_bump_db": round(float(max_bump_db), 3),
            "post_gain_reference": True,
            "post_gain_db": round(float(post_gain_db), 3),
            "tp_abs_target_dbfs": round(float(abs_guard_dbfs), 3),
            "last_pre_peak_dbfs": round(_db(pre_peak), 3),
            "last_post_peak_dbfs": round(_db(post_peak), 3),
            "last_guarded_peak_dbfs": round(_db(guarded_peak), 3),
            "last_delta_rms_db": round(_db(delta_rms), 3),
            "pre_peak_dbfs_max": round(_max_db(state.get("pre_peak_dbfs_max"), _db(pre_peak)), 3),
            "post_peak_dbfs_max": round(_max_db(state.get("post_peak_dbfs_max"), _db(post_peak)), 3),
            "guarded_peak_dbfs_max": round(_max_db(state.get("guarded_peak_dbfs_max"), _db(guarded_peak)), 3),
            "delta_peak_dbfs_max": round(_max_db(state.get("delta_peak_dbfs_max"), _db(delta_peak)), 3),
            "delta_rms_db_max": round(_max_db(state.get("delta_rms_db_max"), _db(delta_rms)), 3),
            # Compatibility aliases used by older debug summaries.
            "pre_peak_dbfs": round(_max_db(state.get("pre_peak_dbfs_max"), _db(pre_peak)), 3),
            "post_peak_dbfs": round(_max_db(state.get("post_peak_dbfs_max"), _db(post_peak)), 3),
            "guarded_peak_dbfs": round(_max_db(state.get("guarded_peak_dbfs_max"), _db(guarded_peak)), 3),
            "delta_rms_db": round(_max_db(state.get("delta_rms_db_max"), _db(delta_rms)), 3),
            "method": "v6336_post_gain_effective_delta_guard",
            "reason": "aggregate_peak_guard_applied" if applied_count > 0 else reason,
        })

    if delta_rms <= _amp(min_delta_rms_db):
        # Delta is inaudible; keep post_mix to avoid silently deleting tiny but
        # harmless assist content.  Telemetry marks the last block as noise-floor.
        _agg_update(applied=False, scale=1.0, reason="augmentation_delta_below_noise_floor", guarded=post)
        return post, state

    scale = 1.0
    guarded = post
    whole_mix_trim_db = 0.0
    preserve_mode = "delta_full"
    if post_peak > target_peak and delta_peak > 1e-10:
        lo, hi = 0.0, 1.0
        for _ in range(10):
            mid = (lo + hi) * 0.5
            cand = pre + delta * np.float32(mid)
            cand_ref = (cand * np.float32(gain)).astype(np.float32, copy=False)
            if _peak(cand_ref) <= target_peak:
                lo = mid
            else:
                hi = mid
        scale = float(lo)
        guarded = (pre + delta * np.float32(scale)).astype(np.float32, copy=False)
        preserve_mode = "delta_scaled"

        # v63.3.6: when the peak-only delta scale would erase a useful assist
        # layer, preserve the complete post-augmentation balance and trim the whole
        # block by a tiny amount instead.  This remains safe because it is checked
        # in the same post-global-gain domain against the same TP target; it simply
        # avoids deleting the musical delta to save a few tenths of a dB.
        if _env_on("BUSY_BAMIX_V6336_AUG_PRESERVE_DELTA_TRIM", "1"):
            min_effective_scale = _env_float("BUSY_BAMIX_V6336_AUG_MIN_EFFECTIVE_SCALE", 0.18, minimum=0.0, maximum=0.95)
            preserve_min_delta_db = _env_float("BUSY_BAMIX_V6336_AUG_PRESERVE_MIN_DELTA_RMS_DB", -78.0, minimum=-120.0, maximum=-36.0)
            max_whole_trim_db = _env_float("BUSY_BAMIX_V6336_AUG_MAX_WHOLE_MIX_TRIM_DB", 0.45, minimum=0.0, maximum=1.5)
            v645_coordinator_active = False
            if _env_on("BUSY_BAMIX_V645_BODY_DENSITY_PEAK_GUARD_COORDINATOR", "1") and scale < _env_float("BUSY_BAMIX_V645_COORDINATOR_SCALE_TRIGGER", 0.14, minimum=0.0, maximum=0.9):
                min_effective_scale = min(min_effective_scale, _env_float("BUSY_BAMIX_V645_COORDINATOR_MIN_EFFECTIVE_SCALE", 0.10, minimum=0.0, maximum=0.9))
                max_whole_trim_db = max(max_whole_trim_db, _env_float("BUSY_BAMIX_V645_COORDINATOR_MAX_WHOLE_TRIM_DB", 0.72, minimum=0.0, maximum=1.5))
                v645_coordinator_active = True
            if scale < min_effective_scale and delta_rms > _amp(preserve_min_delta_db):
                trim = min(1.0, target_peak / max(post_peak, 1e-10))
                trim_db = -_db(trim) if trim > 0 else 999.0
                if trim > 0.0 and trim_db <= max_whole_trim_db:
                    guarded = (post * np.float32(trim)).astype(np.float32, copy=False)
                    whole_mix_trim_db = -float(trim_db)
                    scale = 1.0
                    preserve_mode = "v645_density_coordinator_whole_mix_trim_preserved_full_delta" if v645_coordinator_active else "whole_mix_trim_preserved_full_delta"
    applied = bool(scale < 0.999 or abs(whole_mix_trim_db) > 1e-6)
    _agg_update(applied=applied, scale=scale, reason=preserve_mode if applied else "within_peak_budget", guarded=guarded)
    state["delta_preservation_mode"] = preserve_mode
    state["whole_mix_trim_db"] = round(float(whole_mix_trim_db), 3)
    state["v645_body_density_peak_guard_coordinator"] = {
        "active": bool(str(preserve_mode).startswith("v645_density_coordinator")),
        "preserve_mode": preserve_mode,
        "policy": "when peak-only delta scaling would erase body/density layers, allow a bounded whole-block trim instead of reducing useful derived density to near-zero",
    }
    return np.nan_to_num(guarded, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False), state


def _render_single_premaster(stems: list[dict[str, Any]], stem_metrics: list[dict[str, Any]], recipe: str, output_path: Path, *, target_sr: int = TARGET_SR, correction_gain_db: float = 0.0, width_correction: float = 1.0, density_drive_db: float = 0.0, mix_strategy: dict[str, Any] | None = None, original_decision: dict[str, Any] | None = None, reference_db: dict[str, Any] | None = None, source_morphology_cache_dir: Path | None = None) -> dict[str, Any]:
    max_frames = 0
    for m in stems:
        sr = int(m.get("sample_rate") or target_sr)
        frames = int(round(float(m.get("frames") or 0) * float(target_sr) / max(float(sr), 1.0)))
        max_frames = max(max_frames, frames)
    if max_frames <= 0:
        raise RuntimeError("no stem frames available for render")

    metric_by_path = {str(Path(s.get("local_path") or "")): stem_metrics[i] for i, s in enumerate(stems[:len(stem_metrics)])}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    initial_premix_gain_db, initial_gain_report = _estimate_initial_premix_gain_db(stems, stem_metrics, recipe, original_decision=original_decision, reference_db=reference_db)
    premaster_targets = _recipe_premaster_targets(recipe, stem_metrics, original_decision=original_decision, reference_db=reference_db)
    total_gain_db = float(initial_premix_gain_db) + float(correction_gain_db)
    global_gain = _amp(total_gain_db)
    stem_plan: list[dict[str, Any]] = []
    for s in stems:
        p = Path(str(s.get("local_path") or ""))
        metric = metric_by_path.get(str(p), {})
        role = str(metric.get("role") or "unknown")
        conf = float(metric.get("role_confidence") or 0.0)
        art = float(metric.get("artifact_risk") or 0.0)
        gain_db = _role_gain_db(role, recipe, art, conf)
        width = _width_scalar(role, recipe, art) * float(width_correction)
        crest = float(metric.get("crest_factor_db") or 0.0)
        repair_needed = bool(
            _env_on("BUSY_BAMIX_SOURCE_MORPHOLOGY_REPAIR", "1")
            and (
                art >= _env_float("BUSY_BAMIX_SMR_ARTIFACT_MIN", 0.22, minimum=0.0, maximum=1.0)
                or (role in {"drums", "kick", "snare", "hats"} and crest <= _env_float("BUSY_BAMIX_SMR_PERC_CREST_BELOW", 10.5, minimum=4.0, maximum=18.0))
                or (role in {"bass", "kick"} and float(metric.get("low_focus", 0.0) or 0.0) > 0.45)
            )
            and not (role in {"vocal", "vocals", "lead_vocal"} and art < _env_float("BUSY_BAMIX_SMR_VOCAL_ARTIFACT_MIN", 0.38, minimum=0.0, maximum=1.0))
        )
        stem_plan.append({
            "filename": s.get("filename"),
            "role": role,
            "role_confidence": round(conf, 4),
            "artifact_risk": round(art, 4),
            "crest_factor_db": round(crest, 3),
            "source_morphology_repair_needed": bool(repair_needed),
            "gain_db": round(gain_db, 3),
            "width_scalar": round(width, 4),
        })
    block_size, v6463_block_sizing = _v6463_select_adaptive_block_size(
        max_frames=max_frames,
        target_sr=target_sr,
        stem_count=len(stems),
        smr_stem_count=int(sum(1 for p in stem_plan if p.get("source_morphology_repair_needed"))),
    )

    v645_stem_residue_map = _v645_stem_neural_codec_residue_map(stem_metrics, stem_plan)
    v645_residue_pressure = _v645_residue_pressure_from_map(v645_stem_residue_map)

    reader_paths: list[Path] = []
    smr_cache_reports: list[dict[str, Any]] = []
    for idx, s in enumerate(stems):
        p = Path(str(s.get("local_path") or ""))
        plan = stem_plan[idx] if idx < len(stem_plan) else {}
        if bool(plan.get("source_morphology_repair_needed")) and source_morphology_cache_dir is not None:
            metric = stem_metrics[idx] if idx < len(stem_metrics) and isinstance(stem_metrics[idx], dict) else {}
            cached_path, cache_report = _prepare_bamix_smr_cached_stem(
                s,
                metric,
                idx=idx,
                role=str(plan.get("role") or metric.get("role") or "unknown"),
                cache_dir=source_morphology_cache_dir,
                target_frames=max_frames,
                target_sr=target_sr,
                block_size=block_size,
                gain_db=float(plan.get("gain_db") or 0.0),
                width_scalar=float(plan.get("width_scalar") or 1.0),
                original_decision=original_decision if isinstance(original_decision, dict) else {},
            )
            if isinstance(cache_report, dict):
                smr_cache_reports.append(cache_report)
                plan["source_morphology_cache"] = {
                    "enabled": bool(cache_report.get("enabled")),
                    "used": bool(cache_report.get("cache_used")),
                    "hit": bool(cache_report.get("cache_hit")),
                    "built": bool(cache_report.get("cache_built")),
                    "reason": cache_report.get("reason"),
                    "cache_domain": cache_report.get("cache_domain"),
                    "gain_db": cache_report.get("gain_db"),
                    "width_scalar": cache_report.get("width_scalar"),
                    "estimated_cache_mb": cache_report.get("estimated_cache_mb"),
                    "max_cache_stem_mb": cache_report.get("max_cache_stem_mb"),
                    "cache_path": cache_report.get("cache_path") if bool(cache_report.get("cache_used")) else None,
                }
            reader_paths.append(cached_path)
        else:
            reader_paths.append(p)
    readers = [_iter_blocks_for_stem(p, target_frames=max_frames, target_sr=target_sr, block_size=block_size) for p in reader_paths]

    blocks = 0
    peak_seen = 0.0
    density_active_blocks = 0
    density_report_last: dict[str, Any] = {"active": False, "density_drive_db": round(float(density_drive_db or 0.0), 3)}
    # v63.3.9: aggregate density-drive telemetry across audible blocks.
    # v63.3.8 reported the last active block; if the last block was a fade-out,
    # the report showed -120 dBFS values even though the module acted earlier.
    density_report_agg: dict[str, Any] = {
        "active": False,
        "density_drive_db": round(float(density_drive_db or 0.0), 3),
        "active_block_count": 0,
        "audible_block_count": 0,
        "before_peak_dbfs_max": None,
        "after_peak_dbfs_max": None,
        "peak_relief_db_max": 0.0,
        "peak_relief_db_sum": 0.0,
        "rms_delta_db_sum": 0.0,
        "rms_makeup_db_max": 0.0,
    }
    density_audible_floor_db = _env_float("BUSY_BAMIX_V6339_DENSITY_REPORT_AUDIBLE_FLOOR_DB", -82.0, minimum=-140.0, maximum=-20.0)
    v631_state: dict[str, Any] = {
        "kick_bass": {},
        "vocal_pocket": {},
        "drum_punch": {},
        "harmonic_density": {},
        "elliptical": {},
        "stereo_safety": {},
        "glue": {},
        "translation_qc": {},
        "stem_augmentation": {
            "bass_harmonic_translation": {},
            "low_mid_body_fill": {},
            "vocal_support_body_layer": {},
            "center_anchor": {},
            "drum_parallel_density": {},
            "transient_ghost": {},
            "side_texture_control": {},
            "v645_erb_ms_resonance_suppressor": {},
            "v645_upward_body_density_polynomial": {},
        },
        "v645_stem_neural_codec_residue_map": v645_stem_residue_map,
        "v645_residue_pressure": v645_residue_pressure,
        "v6463_adaptive_block_sizing": v6463_block_sizing,
        "source_morphology_repair": {
            "schema_version": _SCHEMA + ".source_morphology_repair_v6444",
            "enabled": bool(_env_on("BUSY_BAMIX_SOURCE_MORPHOLOGY_REPAIR", "1")),
            "active": False,
            "needed_stem_count": int(sum(1 for p in stem_plan if p.get("source_morphology_repair_needed"))),
            "processed_block_count": 0,
            "active_block_count": 0,
            "dry_rollback_block_count": 0,
            "by_role": {},
            "policy": "applies deterministic source-conditioning only to stems whose artifact/role metrics require repair; no external generation and no alternate final render",
        },
        "blocks_processed": 0,
    }
    if smr_cache_reports:
        smr_state = v631_state.get("source_morphology_repair") if isinstance(v631_state.get("source_morphology_repair"), dict) else {}
        cache_summary = {
            "enabled": bool(any(r.get("enabled") for r in smr_cache_reports)),
            "cache_attempted_count": int(len(smr_cache_reports)),
            "stem_cache_count": int(len(smr_cache_reports)),
            "cache_hit_count": int(sum(1 for r in smr_cache_reports if r.get("cache_hit"))),
            "cache_miss_build_count": int(sum(1 for r in smr_cache_reports if r.get("cache_built"))),
            "cache_used_count": int(sum(1 for r in smr_cache_reports if r.get("cache_used"))),
            "cache_active": bool(any(r.get("cache_used") for r in smr_cache_reports)),
            "reused_block_count": int(sum(int(r.get("reused_block_count") or 0) for r in smr_cache_reports)),
            "built_block_count": int(sum(int(r.get("processed_block_count") or 0) for r in smr_cache_reports if r.get("cache_built"))),
            "cache_domain": "post_role_gain_width_pre_global_gain",
            "policy": "SMR stem cache prevents correction rerenders from repeating the same source-conditioning STFT/DSP pass",
        }
        smr_state["stem_cache"] = cache_summary
        smr_state["cache_reports"] = [{k: r.get(k) for k in ["filename", "role", "enabled", "cache_used", "cache_hit", "cache_built", "reason", "cache_domain", "gain_db", "width_scalar", "estimated_cache_mb", "max_cache_stem_mb", "processed_block_count", "reused_block_count", "active_block_count", "dry_rollback_block_count", "cache_path", "v6463_smr_stem_cache_seam_smoother"]} for r in smr_cache_reports[:12]]
        for r in smr_cache_reports:
            role = str(r.get("role") or "unknown")
            role_bucket = smr_state.setdefault("by_role", {})
            if isinstance(role_bucket, dict):
                rb = role_bucket.setdefault(role, {"processed": 0, "active": 0, "rollback": 0, "reused": 0})
                if isinstance(rb, dict):
                    rb["processed"] = int(rb.get("processed") or 0) + int(r.get("processed_block_count") or 0)
                    rb["active"] = int(rb.get("active") or 0) + int(r.get("active_block_count") or 0)
                    rb["rollback"] = int(rb.get("rollback") or 0) + int(r.get("dry_rollback_block_count") or 0)
                    rb["reused"] = int(rb.get("reused") or 0) + int(r.get("reused_block_count") or 0)
            if r.get("cache_built") or r.get("cache_hit"):
                smr_state["active_block_count"] = int(smr_state.get("active_block_count") or 0) + int(r.get("active_block_count") or 0)
                smr_state["dry_rollback_block_count"] = int(smr_state.get("dry_rollback_block_count") or 0) + int(r.get("dry_rollback_block_count") or 0)
            if r.get("cache_built"):
                smr_state["processed_block_count"] = int(smr_state.get("processed_block_count") or 0) + int(r.get("processed_block_count") or 0)
            if r.get("cache_hit"):
                smr_state["reused_block_count"] = int(smr_state.get("reused_block_count") or 0) + int(r.get("reused_block_count") or 0)
                smr_state["cached_active_block_count"] = int(smr_state.get("cached_active_block_count") or 0) + int(r.get("active_block_count") or 0)
            if r.get("active") or int(r.get("active_block_count") or 0) > 0:
                smr_state["active"] = True
            examples = smr_state.setdefault("examples", [])
            if isinstance(examples, list):
                for ex in (r.get("examples") or [])[: max(0, 12 - len(examples))]:
                    if isinstance(ex, dict):
                        ex2 = dict(ex)
                        ex2["cache_hit"] = bool(r.get("cache_hit"))
                        ex2["cache_built"] = bool(r.get("cache_built"))
                        examples.append(ex2)
        v631_state["source_morphology_repair"] = smr_state
    v631_strategy = mix_strategy if isinstance(mix_strategy, dict) else _build_v631_mix_strategy(recipe, stem_metrics, ai_payload=None)
    v631_reports: dict[str, Any] = {
        "schema_version": _SCHEMA + ".blockwise_professional_module_render",
        "active": bool(v631_strategy.get("active")),
        "strategy": v631_strategy,
        "module_runtime": {},
        "policy": "v63.7.0 applies peak-efficient density plus complete body/center/vocal support: dynamic harmonic low-mid body fill, vocal fundamental body support, center hollow protection, M/S low-mid separation and mud rollback, all before the existing post-gain bus peak guard; no extra GPT calls or full-length candidate buffers.",
    }
    output_prev_tail: np.ndarray | None = None
    with sf.SoundFile(str(output_path), "w", samplerate=target_sr, channels=2, subtype="FLOAT", format="WAV") as out:
        for _start in range(0, max_frames, block_size):
            need = min(block_size, max_frames - _start)
            role_buses: dict[str, np.ndarray] = {}
            for idx, it in enumerate(readers):
                try:
                    b = next(it)
                except StopIteration:
                    b = np.zeros((need, 2), dtype=np.float32)
                if b.shape[0] != need:
                    tmp = np.zeros((need, 2), dtype=np.float32)
                    tmp[:min(need, b.shape[0])] = _ensure_stereo(b)[:min(need, b.shape[0])]
                    b = tmp
                plan = stem_plan[idx]
                role = str(plan.get("role") or "unknown")
                cache_used = bool((plan.get("source_morphology_cache") or {}).get("used") if isinstance(plan.get("source_morphology_cache"), dict) else False)
                if not cache_used:
                    b = _apply_width(b, float(plan.get("width_scalar") or 1.0))
                    b *= _amp(float(plan.get("gain_db") or 0.0))
                if bool(plan.get("source_morphology_repair_needed")) and not cache_used:
                    smr_state = v631_state.get("source_morphology_repair") if isinstance(v631_state.get("source_morphology_repair"), dict) else {}
                    try:
                        stem_metric = stem_metrics[idx] if idx < len(stem_metrics) and isinstance(stem_metrics[idx], dict) else {}
                        repaired_b, smr_rep = apply_source_morphology_repair(
                            b,
                            target_sr,
                            stem_metric,
                            original_decision if isinstance(original_decision, dict) else {},
                            role=role,
                            stem_metric=stem_metric,
                        )
                        smr_state["processed_block_count"] = int(smr_state.get("processed_block_count") or 0) + 1
                        role_bucket = smr_state.setdefault("by_role", {})
                        if isinstance(role_bucket, dict):
                            rb = role_bucket.setdefault(role, {"processed": 0, "active": 0, "rollback": 0})
                            if isinstance(rb, dict):
                                rb["processed"] = int(rb.get("processed") or 0) + 1
                        if isinstance(smr_rep, dict) and smr_rep.get("active"):
                            b = np.asarray(repaired_b, dtype=np.float32)
                            smr_state["active"] = True
                            smr_state["active_block_count"] = int(smr_state.get("active_block_count") or 0) + 1
                            if isinstance(role_bucket, dict) and isinstance(role_bucket.get(role), dict):
                                role_bucket[role]["active"] = int(role_bucket[role].get("active") or 0) + 1
                        elif isinstance(smr_rep, dict) and smr_rep.get("rolled_back_to_dry"):
                            smr_state["dry_rollback_block_count"] = int(smr_state.get("dry_rollback_block_count") or 0) + 1
                            if isinstance(role_bucket, dict) and isinstance(role_bucket.get(role), dict):
                                role_bucket[role]["rollback"] = int(role_bucket[role].get("rollback") or 0) + 1
                        # Keep only compact examples so the report does not balloon.
                        examples = smr_state.setdefault("examples", [])
                        if isinstance(examples, list) and len(examples) < 12 and isinstance(smr_rep, dict):
                            examples.append({
                                "filename": plan.get("filename"),
                                "role": role,
                                "active": bool(smr_rep.get("active")),
                                "needed": bool(smr_rep.get("needed")),
                                "reason": smr_rep.get("reason"),
                                "actions": [a.get("action") for a in (smr_rep.get("actions") or []) if isinstance(a, dict)][:4],
                            })
                    except Exception as exc:
                        smr_state["error_count"] = int(smr_state.get("error_count") or 0) + 1
                        examples = smr_state.setdefault("examples", [])
                        if isinstance(examples, list) and len(examples) < 12:
                            examples.append({"filename": plan.get("filename"), "role": role, "active": False, "reason": "exception", "error": str(exc)[:120]})
                    v631_state["source_morphology_repair"] = smr_state
                # Normalize roles into buses for v63.1 module processing.
                bus = "drums" if role in {"drums", "kick", "snare", "hats"} else "music_bed" if role in {"music", "music_bed", "unknown"} else "fx_ambience" if role in {"fx", "fx_ambience"} else role
                role_buses[bus] = role_buses.get(bus, np.zeros((need, 2), dtype=np.float32)) + b.astype(np.float32, copy=False)

            vocal_bus = role_buses.get("vocal", np.zeros((need, 2), dtype=np.float32))
            drum_bus = role_buses.get("drums", np.zeros((need, 2), dtype=np.float32))
            bass_bus = role_buses.get("bass", np.zeros((need, 2), dtype=np.float32))
            music_bed = role_buses.get("music_bed", np.zeros((need, 2), dtype=np.float32))
            fx_bus = role_buses.get("fx_ambience", np.zeros((need, 2), dtype=np.float32))

            kb_int = _module_gain(v631_strategy, "kick_bass", "light")
            if kb_int > 0.0:
                bass_bus, v631_state["kick_bass"] = _low_band_scalar_duck(bass_bus, drum_bus, sr=target_sr, intensity=kb_int, state=v631_state.get("kick_bass", {}))
            vp_int = _module_gain(v631_strategy, "vocal_pocket", "medium")
            if vp_int > 0.0:
                bed = music_bed + fx_bus
                bed, v631_state["vocal_pocket"] = _vocal_pocket_bed(bed, vocal_bus, intensity=vp_int, state=v631_state.get("vocal_pocket", {}))
                # Split the pocketed bed back proportionally so ambience is not over-ducked.
                mb_r = _rms(music_bed); fx_r = _rms(fx_bus); den = mb_r + fx_r + 1e-9
                music_bed = bed * float(mb_r / den)
                fx_bus = bed * float(fx_r / den)
            dp_int = _module_gain(v631_strategy, "drum_punch", "light")
            if dp_int > 0.0:
                drum_bus, v631_state["drum_punch"] = _drum_soft_peak_rounding(drum_bus, intensity=dp_int, state=v631_state.get("drum_punch", {}))

            aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
            pre_aug_mix = (vocal_bus + drum_bus + bass_bus + music_bed + fx_bus).astype(np.float32, copy=False)
            bh_int = _augmentation_gain(v631_strategy, "bass_harmonic_translation", "off")
            if bh_int > 0.0:
                bass_bus, aug_state["bass_harmonic_translation"] = _augment_bass_harmonic_translation(bass_bus, vocal_bus, sr=target_sr, intensity=bh_int, state=aug_state.get("bass_harmonic_translation", {}), recipe=recipe)
            body_int = _augmentation_gain(v631_strategy, "low_mid_body_fill", "off")
            if body_int > 0.0:
                music_bed, aug_state["low_mid_body_fill"] = _augment_low_mid_body_fill(music_bed, drum_bus, vocal_bus, sr=target_sr, intensity=body_int, state=aug_state.get("low_mid_body_fill", {}))
            vocal_body_int = _augmentation_gain(v631_strategy, "vocal_support_body_layer", "off")
            if vocal_body_int > 0.0:
                vocal_bus, aug_state["vocal_support_body_layer"] = _augment_vocal_support_body_layer(vocal_bus, music_bed, bass_bus, sr=target_sr, intensity=vocal_body_int, state=aug_state.get("vocal_support_body_layer", {}))
            drum_den_int = _augmentation_gain(v631_strategy, "drum_parallel_density", "off")
            transient_int = _augmentation_gain(v631_strategy, "transient_ghost", "off")
            side_tex_int = _augmentation_gain(v631_strategy, "side_texture_control", "off")
            raw_erb_int = max(float(side_tex_int or 0.0), _augmentation_gain(v631_strategy, "v645_erb_ms_resonance_suppressor", "light") if bool(v645_residue_pressure.get("active")) else 0.0) if _env_on("BUSY_BAMIX_V645_ERB_MS_RESONANCE_SUPPRESSOR", "1") else 0.0
            ownership = _v6464_transient_hf_ownership_plan(
                drum_den_int=drum_den_int,
                transient_int=transient_int,
                side_tex_int=side_tex_int,
                erb_int=raw_erb_int,
                residue_pressure=v645_residue_pressure,
            )
            aug_state["v6464_transient_hf_ownership_lock"] = ownership
            eff = ownership.get("effective") if isinstance(ownership.get("effective"), dict) else {}
            drum_den_eff = float(eff.get("drum_parallel_density", drum_den_int) or 0.0)
            transient_eff = float(eff.get("transient_ghost", transient_int) or 0.0)
            side_tex_eff = float(eff.get("side_texture_control", side_tex_int) or 0.0)
            erb_eff = float(eff.get("erb_ms_dynamic_resonance_suppressor", raw_erb_int) or 0.0)
            if drum_den_eff > 0.0:
                drum_bus, _drum_den_state = _augment_drum_parallel_density(drum_bus, sr=target_sr, intensity=drum_den_eff, state=aug_state.get("drum_parallel_density", {}))
                aug_state["drum_parallel_density"] = _v6464_scale_reported_intensity(_drum_den_state, drum_den_int, drum_den_eff, "transient_ownership_lock")
            if transient_eff > 0.0:
                drum_bus, _transient_state = _augment_transient_ghost(drum_bus, sr=target_sr, intensity=transient_eff, state=aug_state.get("transient_ghost", {}))
                aug_state["transient_ghost"] = _v6464_scale_reported_intensity(_transient_state, transient_int, transient_eff, "transient_ownership_lock")
            v631_state["stem_augmentation"] = aug_state

            mix = (vocal_bus + drum_bus + bass_bus + music_bed + fx_bus).astype(np.float32, copy=False)
            center_int = _augmentation_gain(v631_strategy, "center_anchor", "off")
            if center_int > 0.0:
                aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
                mix, aug_state["center_anchor"] = _augment_center_anchor_mix(mix, sr=target_sr, intensity=center_int, state=aug_state.get("center_anchor", {}))
                v631_state["stem_augmentation"] = aug_state
            if side_tex_eff > 0.0:
                aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
                mix, _side_tex_state = _augment_side_texture_control_mix(mix, sr=target_sr, intensity=side_tex_eff, state=aug_state.get("side_texture_control", {}), recipe=recipe, residue_pressure=v645_residue_pressure)
                aug_state["side_texture_control"] = _v6464_scale_reported_intensity(_side_tex_state, side_tex_int, side_tex_eff, "side_hf_ownership_lock")
                v631_state["stem_augmentation"] = aug_state
            if _env_on("BUSY_BAMIX_V645_ERB_MS_RESONANCE_SUPPRESSOR", "1"):
                aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
                erb_int = erb_eff
                if erb_int > 0.0:
                    mix, _erb_state = _v645_erb_ms_dynamic_resonance_suppressor_mix(
                        mix,
                        sr=target_sr,
                        intensity=erb_int,
                        state=aug_state.get("v645_erb_ms_resonance_suppressor", {}),
                        residue_pressure=v645_residue_pressure,
                    )
                    aug_state["v645_erb_ms_resonance_suppressor"] = _v6464_scale_reported_intensity(_erb_state, raw_erb_int, erb_int, "side_hf_ownership_lock")
                    v631_state["stem_augmentation"] = aug_state
            if _env_on("BUSY_BAMIX_V645_UPWARD_BODY_POLYNOMIAL_DENSITY", "1"):
                aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
                body_density_int = max(
                    _augmentation_gain(v631_strategy, "low_mid_body_fill", "off") * 0.75,
                    _module_gain(v631_strategy, "harmonic_density", "off") * 0.55,
                )
                if body_density_int > _env_float("BUSY_BAMIX_V645_BODY_DENSITY_MIN_INTENSITY", 0.18, minimum=0.0, maximum=1.0):
                    mix, aug_state["v645_upward_body_density_polynomial"] = _v645_upward_body_density_polynomial_mix(
                        mix,
                        sr=target_sr,
                        intensity=body_density_int,
                        state=aug_state.get("v645_upward_body_density_polynomial", {}),
                    )
                v631_state["stem_augmentation"] = aug_state
            aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
            # Guard assist-layer peak cost in the actual post-global-gain
            # premaster domain.  This prevents the internal float summing bus from
            # over-suppressing useful derived layers before the planned headroom
            # gain is applied.
            mix, aug_state["v6464_assist_delta_consolidator"] = _v6464_assist_delta_consolidator(
                pre_aug_mix,
                mix,
                sr=target_sr,
                state=aug_state.get("v6464_assist_delta_consolidator", {}),
            )
            try:
                _tp_abs = float(premaster_targets.get("tp_max", -1.5)) - _env_float("BUSY_BAMIX_V6335_2_AUG_GUARD_TP_MARGIN_DB", 0.15, minimum=0.0, maximum=1.5)
            except Exception:
                _tp_abs = -1.65
            mix, aug_state["bus_peak_guard"] = _v6332_augmentation_bus_peak_guard(
                pre_aug_mix, mix, state=aug_state.get("bus_peak_guard", {}),
                post_gain_db=total_gain_db, tp_abs_target_dbfs=_tp_abs,
            )
            v631_state["stem_augmentation"] = aug_state

            # Global premaster gain for headroom.  No final limiting/maximization.
            mix *= global_gain

            hd_int = _module_gain(v631_strategy, "harmonic_density", "light")
            if hd_int > 0.0:
                mix, v631_state["harmonic_density"] = _harmonic_density(mix, intensity=hd_int, state=v631_state.get("harmonic_density", {}))
            ell_int = _module_gain(v631_strategy, "elliptical", "medium")
            if ell_int > 0.0:
                mix, v631_state["elliptical"] = _elliptical_stereo_safety(mix, sr=target_sr, intensity=ell_int, state=v631_state.get("elliptical", {}), recipe=recipe)
            ss_int = _module_gain(v631_strategy, "stereo_safety", "medium")
            if ss_int > 0.0:
                mix, v631_state["stereo_safety"] = _stereo_depth_safety(mix, sr=target_sr, intensity=ss_int, state=v631_state.get("stereo_safety", {}), recipe=recipe)
            glue_int = _module_gain(v631_strategy, "glue", "medium")
            if glue_int > 0.0:
                mix, v631_state["glue"] = _glue_compressor_block(mix, sr=target_sr, intensity=glue_int, state=v631_state.get("glue", {}), recipe=recipe)

            density_block_report = {"active": False}
            if float(density_drive_db or 0.0) > 1e-6:
                mix, density_block_report = _apply_density_drive_block(mix, float(density_drive_db or 0.0))
            # Emergency anti-NaN and non-destructive peak protection. The normal path
            # should be handled by a deterministic one-time rerender correction.
            mix = np.nan_to_num(mix, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
            if _module_gain(v631_strategy, "translation_qc", "medium") > 0.0:
                v631_state["translation_qc"] = _translation_qc_update(mix, v631_state.get("translation_qc", {}))
            if density_block_report.get("active"):
                density_active_blocks += 1
                density_report_last = density_block_report
                try:
                    density_report_agg["active"] = True
                    density_report_agg["active_block_count"] = int(density_active_blocks)
                    br = float(density_block_report.get("before_rms_db", -999.0))
                    bp = float(density_block_report.get("before_peak_dbfs", -999.0))
                    ap = float(density_block_report.get("after_peak_dbfs", -999.0))
                    pr = float(density_block_report.get("peak_relief_db", 0.0))
                    rd = float(density_block_report.get("rms_delta_db", 0.0))
                    mk = float(density_block_report.get("rms_makeup_db", 0.0))
                    if br >= float(density_audible_floor_db):
                        density_report_agg["audible_block_count"] = int(density_report_agg.get("audible_block_count") or 0) + 1
                        density_report_agg["peak_relief_db_sum"] = float(density_report_agg.get("peak_relief_db_sum") or 0.0) + pr
                        density_report_agg["rms_delta_db_sum"] = float(density_report_agg.get("rms_delta_db_sum") or 0.0) + rd
                        old_bp = density_report_agg.get("before_peak_dbfs_max")
                        old_ap = density_report_agg.get("after_peak_dbfs_max")
                        density_report_agg["before_peak_dbfs_max"] = bp if old_bp is None else max(float(old_bp), bp)
                        density_report_agg["after_peak_dbfs_max"] = ap if old_ap is None else max(float(old_ap), ap)
                        density_report_agg["peak_relief_db_max"] = max(float(density_report_agg.get("peak_relief_db_max") or 0.0), pr)
                        density_report_agg["rms_makeup_db_max"] = max(float(density_report_agg.get("rms_makeup_db_max") or 0.0), mk)
                except Exception:
                    pass
            peak_seen = max(peak_seen, _peak(mix))
            aug_state = v631_state.get("stem_augmentation", {}) if isinstance(v631_state.get("stem_augmentation"), dict) else {}
            mix, output_prev_tail, aug_state["v6463_output_seam_smoother"] = _v6463_seam_smooth_block(
                mix,
                output_prev_tail,
                sr=target_sr,
                state=aug_state.get("v6463_output_seam_smoother", {}),
                label="output",
            )
            v631_state["stem_augmentation"] = aug_state
            out.write(mix)
            blocks += 1
            v631_state["blocks_processed"] = blocks
            del mix, vocal_bus, drum_bus, bass_bus, music_bed, fx_bus, role_buses
    if _module_gain(v631_strategy, "translation_qc", "medium") > 0.0:
        v631_state["translation_qc"] = _translation_qc_finalize(v631_state.get("translation_qc", {}))
    def _public_state(d: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not isinstance(d, dict):
            return out
        for k, v in d.items():
            # Do not store IIR coefficients/filter delay arrays or any block buffers in the report.
            _lk = str(k).lower()
            if ("sos" in _lk) or ("zi" in _lk) or _lk.endswith("_cfg") or isinstance(v, np.ndarray):
                continue
            out[str(k)] = _jsonable(v)
        return out
    v631_reports["module_runtime"] = {
        "kick_bass": _public_state(v631_state.get("kick_bass", {})),
        "vocal_pocket": _public_state(v631_state.get("vocal_pocket", {})),
        "drum_punch": _public_state(v631_state.get("drum_punch", {})),
        "harmonic_density": _public_state(v631_state.get("harmonic_density", {})),
        "elliptical": _public_state(v631_state.get("elliptical", {})),
        "stereo_safety": _public_state(v631_state.get("stereo_safety", {})),
        "glue": _public_state(v631_state.get("glue", {})),
        "translation_qc": _public_state(v631_state.get("translation_qc", {})),
        "source_morphology_repair": _public_state(v631_state.get("source_morphology_repair", {})),
            "stem_augmentation": {
            "bass_harmonic_translation": _public_state((v631_state.get("stem_augmentation") or {}).get("bass_harmonic_translation", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "low_mid_body_fill": _public_state((v631_state.get("stem_augmentation") or {}).get("low_mid_body_fill", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "vocal_support_body_layer": _public_state((v631_state.get("stem_augmentation") or {}).get("vocal_support_body_layer", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "center_anchor": _public_state((v631_state.get("stem_augmentation") or {}).get("center_anchor", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "drum_parallel_density": _public_state((v631_state.get("stem_augmentation") or {}).get("drum_parallel_density", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "transient_ghost": _public_state((v631_state.get("stem_augmentation") or {}).get("transient_ghost", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "side_texture_control": _public_state((v631_state.get("stem_augmentation") or {}).get("side_texture_control", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "v645_erb_ms_resonance_suppressor": _public_state((v631_state.get("stem_augmentation") or {}).get("v645_erb_ms_resonance_suppressor", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "v645_upward_body_density_polynomial": _public_state((v631_state.get("stem_augmentation") or {}).get("v645_upward_body_density_polynomial", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "v6464_transient_hf_ownership_lock": _public_state((v631_state.get("stem_augmentation") or {}).get("v6464_transient_hf_ownership_lock", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "v6464_assist_delta_consolidator": _public_state((v631_state.get("stem_augmentation") or {}).get("v6464_assist_delta_consolidator", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "v6463_output_seam_smoother": _public_state((v631_state.get("stem_augmentation") or {}).get("v6463_output_seam_smoother", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
            "bus_peak_guard": _public_state((v631_state.get("stem_augmentation") or {}).get("bus_peak_guard", {})) if isinstance(v631_state.get("stem_augmentation"), dict) else {},
        },
        "v645_stem_neural_codec_residue_map": _public_state(v631_state.get("v645_stem_neural_codec_residue_map", {})),
        "v645_residue_pressure": _public_state(v631_state.get("v645_residue_pressure", {})),
        "v6463_adaptive_block_sizing": _public_state(v631_state.get("v6463_adaptive_block_sizing", {})),
        "blocks_processed": int(blocks),
    }
    if bool(density_report_agg.get("active")):
        try:
            audible_n = max(1, int(density_report_agg.get("audible_block_count") or 0))
            density_report_public = {
                "active": True,
                "schema_version": _SCHEMA + ".density_drive_aggregate_v6339",
                "density_drive_db": round(float(density_drive_db or 0.0), 3),
                "curve": str(density_report_last.get("curve") or "unknown"),
                "active_block_count": int(density_report_agg.get("active_block_count") or density_active_blocks),
                "audible_block_count": int(density_report_agg.get("audible_block_count") or 0),
                "before_peak_dbfs": round(float(density_report_agg.get("before_peak_dbfs_max")), 3) if density_report_agg.get("before_peak_dbfs_max") is not None else None,
                "after_peak_dbfs": round(float(density_report_agg.get("after_peak_dbfs_max")), 3) if density_report_agg.get("after_peak_dbfs_max") is not None else None,
                "peak_relief_db": round(float(density_report_agg.get("peak_relief_db_max") or 0.0), 3),
                "peak_relief_db_avg_audible": round(float(density_report_agg.get("peak_relief_db_sum") or 0.0) / float(audible_n), 3) if int(density_report_agg.get("audible_block_count") or 0) > 0 else 0.0,
                "rms_delta_db_avg_audible": round(float(density_report_agg.get("rms_delta_db_sum") or 0.0) / float(audible_n), 3) if int(density_report_agg.get("audible_block_count") or 0) > 0 else 0.0,
                "rms_makeup_db_max": round(float(density_report_agg.get("rms_makeup_db_max") or 0.0), 3),
                "last_block": density_report_last,
                "report_policy": "v63.3.9 reports aggregate audible-block density action; last_block is diagnostic only",
            }
        except Exception:
            density_report_public = {**density_report_last, "active_block_count": int(density_active_blocks), "report_policy": "v63.3.9 aggregate fallback"}
    else:
        density_report_public = {**density_report_last, "active_block_count": int(density_active_blocks)}
    return {
        "output_path": str(output_path),
        "target_sr": int(target_sr),
        "frames": int(max_frames),
        "duration_sec": round(max_frames / float(target_sr), 3),
        "block_size": int(block_size),
        "rendered_block_count": int(blocks),
        "peak_seen_dbfs_proxy": round(_db(peak_seen), 3),
        "stem_processing_plan": stem_plan,
        "initial_premix_gain_db": round(float(initial_premix_gain_db), 3),
        "global_correction_gain_db": round(float(correction_gain_db), 3),
        "total_global_gain_db": round(float(total_gain_db), 3),
        "density_drive_db": round(float(density_drive_db or 0.0), 3),
        "density_drive_report": density_report_public,
        "v631_professional_module_render": v631_reports,
        "v645_stem_neural_codec_residue_map": v645_stem_residue_map,
        "v645_residue_pressure": v645_residue_pressure,
        "prerender_gain_estimator": initial_gain_report,
        "premaster_targets": premaster_targets,
        "width_correction": round(float(width_correction), 4),
        "policy": {
            "single_full_premaster_render": True,
            "full_candidate_render_count": 1,
            "parallel_full_candidate_buffers": 0,
            "final_limiter_or_maximizer_applied": False,
        },
    }



def _v6350_density_limiter_workload_plan_from_metrics(
    *,
    targets: dict[str, Any],
    lufs: Any,
    tp: Any,
    crest: Any,
    corr: Any,
    hard_warnings: list[str] | None = None,
    soft_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Plan priority-1 density work from the handoff contract, not blockers.

    The plan estimates how much work would be forced onto the final limiter and
    allocates the excess to BAMix density_drive / TP-safe makeup inside the one
    allowed correction render.
    """
    enabled = _env_on("BUSY_BAMIX_V6350_WORKLOAD_ROUTER", "1")
    report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".density_limiter_workload_router_v6350",
        "enabled": bool(enabled),
        "active": False,
    }
    if not enabled:
        report["reason"] = "disabled_by_env"
        return report
    try:
        lufs_f = float(lufs)
        tp_f = float(tp)
        crest_f = float(crest)
    except Exception:
        report["reason"] = "missing_metrics"
        return report
    try:
        corr_f = float(corr)
    except Exception:
        corr_f = 1.0
    contract = targets.get("v6341_reference_db_handoff_contract") if isinstance(targets.get("v6341_reference_db_handoff_contract"), dict) else (targets.get("v6340_premaster_handoff_contract") if isinstance(targets.get("v6340_premaster_handoff_contract"), dict) else {})
    budget = contract.get("final_limiter_gain_budget_lu") if isinstance(contract.get("final_limiter_gain_budget_lu"), (list, tuple)) else targets.get("expected_mastering_gain_window_lu")
    try:
        budget_min = float(budget[0]) if isinstance(budget, (list, tuple)) and len(budget) >= 1 else 2.0
        budget_target = float(budget[1]) if isinstance(budget, (list, tuple)) and len(budget) >= 2 else 3.4
        budget_max = float(budget[2]) if isinstance(budget, (list, tuple)) and len(budget) >= 3 else max(4.4, budget_target)
    except Exception:
        budget_min, budget_target, budget_max = 2.0, 3.4, 4.4
    try:
        delivery_target = float(contract.get("delivery_target_lufs") if contract.get("delivery_target_lufs") is not None else _env_float("BUSY_BAMIX_V632_COMMERCIAL_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0))
    except Exception:
        delivery_target = _env_float("BUSY_BAMIX_V632_COMMERCIAL_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0)
    required_gain = float(delivery_target) - float(lufs_f)
    limiter_excess = max(0.0, float(required_gain) - float(budget_max))
    target_excess = max(0.0, float(required_gain) - float(budget_target))
    lufs_min = float(targets.get("lufs_min") or contract.get("premaster_lufs_min") or (delivery_target - budget_max))
    lufs_target = float(targets.get("lufs_target") or contract.get("premaster_lufs_target") or (delivery_target - budget_target))
    tp_max = float(targets.get("tp_max") or contract.get("premaster_tp_max_dbtp") or -1.6)
    tp_target = float(targets.get("tp_target") or contract.get("premaster_tp_target_dbtp") or -2.4)
    tp_room_to_max = max(0.0, float(tp_max) - float(tp_f) - _env_float("BUSY_BAMIX_V6350_TP_MAX_MARGIN_DB", 0.12, minimum=0.0, maximum=1.0))
    tp_room_to_target = max(0.0, float(tp_target) - float(tp_f) - _env_float("BUSY_BAMIX_V6350_TP_TARGET_MARGIN_DB", 0.18, minimum=0.0, maximum=1.0))
    hard = {str(w) for w in (hard_warnings or []) if str(w)}
    soft = {str(w) for w in (soft_warnings or []) if str(w)}
    fragile = bool(
        corr_f < _env_float("BUSY_BAMIX_V6350_MIN_CORR_FOR_DENSITY", 0.25, minimum=-0.5, maximum=0.95)
        or crest_f < _env_float("BUSY_BAMIX_V6350_MIN_CREST_FOR_DENSITY_DB", 8.2, minimum=4.0, maximum=14.0)
        or any("phase" in w for w in hard | soft)
    )
    enough_reason = bool(
        limiter_excess >= _env_float("BUSY_BAMIX_V6350_LIMITER_EXCESS_TRIGGER_LU", 0.18, minimum=0.0, maximum=2.0)
        or target_excess >= _env_float("BUSY_BAMIX_V6350_TARGET_EXCESS_TRIGGER_LU", 0.75, minimum=0.0, maximum=3.0)
        or lufs_f < lufs_min - _env_float("BUSY_BAMIX_V6350_LUFS_MIN_TOLERANCE_LU", 0.12, minimum=0.0, maximum=1.0)
    )
    drive_db = 0.0
    makeup_db = 0.0
    reasons: list[str] = []
    if enough_reason and not fragile:
        drive_base = _env_float("BUSY_BAMIX_V6350_DRIVE_BASE_DB", 1.15, minimum=0.0, maximum=3.5)
        drive_db = drive_base + limiter_excess * _env_float("BUSY_BAMIX_V6350_DRIVE_EXCESS_SCALE", 0.72, minimum=0.0, maximum=2.0) + max(0.0, lufs_min - lufs_f) * _env_float("BUSY_BAMIX_V6350_DRIVE_LUFS_GAP_SCALE", 0.30, minimum=0.0, maximum=1.2)
        drive_db = min(drive_db, _env_float("BUSY_BAMIX_V6350_DRIVE_MAX_DB", 3.8, minimum=0.0, maximum=6.0), max(0.0, 1.25 + tp_room_to_max * _env_float("BUSY_BAMIX_V6350_DRIVE_TP_ROOM_SCALE", 1.8, minimum=0.0, maximum=5.0)))
        if drive_db > _env_float("BUSY_BAMIX_V6350_DRIVE_MIN_DB", 0.55, minimum=0.0, maximum=2.0):
            reasons.append("v6350_route_limiter_budget_excess_to_upstream_density_chain")
        else:
            drive_db = 0.0
        makeup_db = min(
            max(0.0, min(lufs_target, lufs_min + 0.45) - lufs_f),
            tp_room_to_target,
            _env_float("BUSY_BAMIX_V6350_TP_SAFE_MAKEUP_MAX_DB", 1.15, minimum=0.0, maximum=3.0),
        )
        if makeup_db > _env_float("BUSY_BAMIX_V6350_MAKEUP_MIN_DB", 0.18, minimum=0.0, maximum=1.0):
            reasons.append("v6350_tp_safe_handoff_makeup_after_density")
        else:
            makeup_db = 0.0
    elif fragile:
        reasons.append("v6350_density_router_fragile_no_upstream_density")
    else:
        reasons.append("v6350_handoff_budget_ok_no_density_correction")
    active = bool((drive_db > 0.0 or makeup_db > 0.0) and not fragile)
    report.update({
        "active": active,
        "contract_active": bool(contract.get("active")),
        "profile": contract.get("profile"),
        "genre_bucket": contract.get("genre_bucket"),
        "delivery_target_lufs": round(float(delivery_target), 3),
        "final_limiter_budget_lu": [round(float(budget_min), 3), round(float(budget_target), 3), round(float(budget_max), 3)],
        "estimated_final_limiter_workload_lu": round(float(required_gain), 3),
        "workload_excess_over_target_lu": round(float(target_excess), 3),
        "workload_excess_over_max_lu": round(float(limiter_excess), 3),
        "premaster_lufs": round(float(lufs_f), 3),
        "premaster_true_peak_dbtp": round(float(tp_f), 3),
        "premaster_crest_db": round(float(crest_f), 3),
        "premaster_corr": round(float(corr_f), 4),
        "tp_room_to_premaster_max_db": round(float(tp_room_to_max), 3),
        "tp_room_to_premaster_target_db": round(float(tp_room_to_target), 3),
        "recommended_density_drive_db": round(float(max(0.0, drive_db)), 3),
        "recommended_tp_safe_makeup_db": round(float(max(0.0, makeup_db)), 3),
        "fragile": bool(fragile),
        "reasons": reasons,
        "policy": "Use premaster handoff budget to select upstream density/clipper/parallel RMS work before final limiting; this is not a blocker-name patch and not a target-LUFS chase.",
    })
    return _jsonable(report)

def _premaster_qc(path: Path, sr: int = TARGET_SR, recipe: str | None = None, stem_metrics: list[dict[str, Any]] | None = None, *, original_decision: dict[str, Any] | None = None, reference_db: dict[str, Any] | None = None) -> dict[str, Any]:
    y, sr2 = load_audio_file(str(path), target_sr=sr, dtype="float32")
    y = _ensure_stereo(y)
    fast = analyze_audio_fast_qc(y, sr2)
    peak = _peak(y)
    r = _rms(y)
    crest = _db(peak / max(r, 1e-9)) if peak > 0 and r > 0 else 0.0
    corr = _corr(y)
    lufs = fast.get("integrated_lufs")
    tp = fast.get("approx_true_peak_dbfs") or fast.get("true_peak_dbfs") or _db(peak)
    lra = fast.get("lra_lu") or fast.get("loudness_range_lu")
    if lra is None:
        try:
            lra = (((fast.get("loudness") or {}).get("short_term_lufs") or {}).get("lra_lu"))
        except Exception:
            lra = None
    if lra is None:
        lra = _runtime_safe_lra_lu(y, sr2)
    targets = _recipe_premaster_targets(recipe, stem_metrics or [], original_decision=original_decision, reference_db=reference_db)
    warnings: list[str] = []
    soft_warnings: list[str] = []
    hard_fail = False
    underdriven = False
    try:
        tp_f = float(tp)
        if tp_f > _env_float("BUSY_AUTOMIX_PREMASTER_HARD_TP_FAIL_DBTP", 0.1, minimum=-12.0, maximum=1.0):
            warnings.append("true_peak_over_hard_fail")
            hard_fail = True
        elif tp_f > float(targets.get("tp_max") or -2.0):
            soft_warnings.append("true_peak_above_recipe_target_window")
        if tp_f < float(targets.get("tp_min") or -5.0):
            soft_warnings.append("true_peak_below_recipe_target_window")
    except Exception:
        pass
    try:
        lufs_f = float(lufs)
        if lufs_f < _env_float("BUSY_AUTOMIX_PREMASTER_UNDERDRIVEN_HARD_LUFS", -20.0, minimum=-30.0, maximum=-12.0):
            warnings.append("integrated_lufs_underdriven_hard_fail")
            hard_fail = True
            underdriven = True
        elif lufs_f < float(targets.get("lufs_min") or -18.0):
            soft_warnings.append("integrated_lufs_below_recipe_target_window")
        if lufs_f > float(targets.get("lufs_max") or -14.0):
            soft_warnings.append("integrated_lufs_above_recipe_target_window")
    except Exception:
        pass
    try:
        crest_f = float(crest)
        if crest_f > _env_float("BUSY_AUTOMIX_PREMASTER_HIGH_CREST_HARD_DB", 16.5, minimum=10.0, maximum=24.0):
            warnings.append("crest_factor_under_glued_hard_fail")
            hard_fail = True
            underdriven = True
        elif crest_f > float(targets.get("crest_max") or 15.5):
            soft_warnings.append("crest_factor_above_recipe_target_window")
        if crest_f < float(targets.get("crest_min") or 10.5):
            soft_warnings.append("crest_factor_below_recipe_target_window")
    except Exception:
        pass
    try:
        if float(corr) < _env_float("BUSY_AUTOMIX_PREMASTER_MIN_CORRELATION", 0.12, minimum=-0.5, maximum=0.9):
            warnings.append("phase_correlation_low")
            hard_fail = True
    except Exception:
        pass
    try:
        lufs_f = float(lufs)
        tp_f = float(tp)
        crest_f = float(crest)
        mastering_target = _env_float("BUSY_AUTOMIX_EXPECTED_MASTERING_TARGET_LUFS", _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0), minimum=-16.0, maximum=-8.0)
        required_gain = mastering_target - lufs_f
        expected_window = targets.get("expected_mastering_gain_window_lu") if isinstance(targets.get("expected_mastering_gain_window_lu"), (list, tuple)) else None
        expected_gain_max = float(expected_window[1]) if expected_window and len(expected_window) >= 2 else _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_GAIN_MAX_LU", 4.6, minimum=2.0, maximum=8.0)
        if _env_on("BUSY_BAMIX_V6322_QC_EXPECTED_GAIN_WINDOW", "1") and bool(targets.get("commercial_premaster_builder")):
            if required_gain > expected_gain_max + _env_float("BUSY_BAMIX_V6322_EXPECTED_GAIN_WARNING_MARGIN_LU", 0.25, minimum=0.0, maximum=2.0):
                soft_warnings.append("mastering_gain_above_commercial_premaster_window")
        if (lufs_f < -19.0 and tp_f < -6.0) or (required_gain > _env_float("BUSY_AUTOMIX_MAX_SAFE_MASTERING_GAIN_LUFS", 8.0, minimum=3.0, maximum=14.0)):
            underdriven = True
            if "bamix_premaster_underdriven" not in warnings:
                warnings.append("bamix_premaster_underdriven")
            hard_fail = True
        if lufs_f < -19.0 and crest_f > 15.0:
            underdriven = True
            if "high_crest_low_density_underdriven" not in warnings:
                warnings.append("high_crest_low_density_underdriven")
            hard_fail = True
    except Exception:
        required_gain = None
    try:
        required_gain = round(float(_env_float("BUSY_AUTOMIX_EXPECTED_MASTERING_TARGET_LUFS", _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0), minimum=-16.0, maximum=-8.0)) - float(lufs), 3)
    except Exception:
        required_gain = None
    try:
        contract = targets.get("v6340_premaster_handoff_contract") if isinstance(targets.get("v6340_premaster_handoff_contract"), dict) else {}
        budget = contract.get("final_limiter_gain_budget_lu") if isinstance(contract.get("final_limiter_gain_budget_lu"), (list, tuple)) else targets.get("expected_mastering_gain_window_lu")
        budget_max = float(budget[-1]) if isinstance(budget, (list, tuple)) and len(budget) >= 1 else float((targets.get("expected_mastering_gain_window_lu") or [0.0, 4.6])[-1])
        budget_target = float(budget[1]) if isinstance(budget, (list, tuple)) and len(budget) >= 3 else max(0.0, budget_max - 0.8)
        limiter_excess = float(required_gain) - float(budget_max) if required_gain is not None else None
        plr = float(tp) - float(lufs) if tp is not None and lufs is not None else None
        contract_assessment = {
            "schema_version": _SCHEMA + ".v6340_contract_assessment",
            "active": bool(targets.get("v6340_contract_applied")),
            "profile": contract.get("profile"),
            "genre_bucket": contract.get("genre_bucket"),
            "delivery_target_lufs": contract.get("delivery_target_lufs"),
            "final_limiter_budget_target_lu": round(float(budget_target), 3),
            "final_limiter_budget_max_lu": round(float(budget_max), 3),
            "estimated_mastering_gain_required_lu": required_gain,
            "final_limiter_budget_excess_lu": round(float(limiter_excess), 3) if limiter_excess is not None else None,
            "plr_db": round(float(plr), 3) if plr is not None else None,
            "handoff_lufs_gap_to_min_lu": round(float(float(targets.get("lufs_min")) - float(lufs)), 3) if lufs is not None and targets.get("lufs_min") is not None else None,
            "handoff_tp_room_to_max_db": round(float(float(targets.get("tp_max")) - float(tp)), 3) if tp is not None and targets.get("tp_max") is not None else None,
            "action_hint": "route_density_upstream_before_limiter" if limiter_excess is not None and limiter_excess > 0.0 else "handoff_budget_ok",
        }
    except Exception as _contract_exc:
        contract_assessment = {"schema_version": _SCHEMA + ".v6340_contract_assessment", "active": False, "reason": "exception", "error": str(_contract_exc)[:180]}
    try:
        v6350_density_limiter_workload_plan = _v6350_density_limiter_workload_plan_from_metrics(
            targets=targets,
            lufs=lufs,
            tp=tp,
            crest=crest,
            corr=corr,
            hard_warnings=warnings,
            soft_warnings=soft_warnings,
        )
    except Exception as _v6350_exc:
        v6350_density_limiter_workload_plan = {"schema_version": _SCHEMA + ".density_limiter_workload_router_v6350", "enabled": True, "active": False, "reason": "exception", "error": str(_v6350_exc)[:180]}
    return {
        "schema_version": _SCHEMA + ".premaster_qc",
        "path": str(path),
        "recipe": str(recipe or ""),
        "premaster_targets": targets,
        "integrated_lufs": lufs,
        "true_peak_dbtp": tp,
        "peak_dbfs_proxy": round(_db(peak), 3),
        "rms_db": round(_db(r), 3),
        "crest_factor_db": round(float(crest), 3),
        "lra_lu": round(float(lra), 3) if lra is not None else None,
        "phase_correlation": round(float(corr), 4),
        "estimated_mastering_gain_required_lufs": required_gain,
        "v6340_contract_assessment": contract_assessment,
        "v6350_density_limiter_workload_plan": v6350_density_limiter_workload_plan,
        "underdriven_premaster": bool(underdriven),
        "v632_commercial_premaster_density": {
            "active": _env_on("BUSY_BAMIX_V632_COMMERCIAL_PREMASTER_DENSITY", "1"),
            "target_lufs": round(float(targets.get("lufs_target") or 0.0), 3) if targets.get("lufs_target") is not None else None,
            "min_lufs": round(float(targets.get("lufs_min") or 0.0), 3) if targets.get("lufs_min") is not None else None,
            "target_gap_lu": round(float((targets.get("lufs_target") or 0.0) - float(lufs)), 3) if lufs is not None and targets.get("lufs_target") is not None else None,
            "mastering_gain_required_lu": required_gain,
            "expected_mastering_gain_window_lu": targets.get("expected_mastering_gain_window_lu"),
            "mastering_gain_excess_lu": round(float(required_gain - float((targets.get("expected_mastering_gain_window_lu") or [0.0, _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_GAIN_MAX_LU", 4.6, minimum=2.0, maximum=8.0)])[1])), 3) if required_gain is not None and isinstance((targets.get("expected_mastering_gain_window_lu") or None), (list, tuple)) and len(targets.get("expected_mastering_gain_window_lu") or []) >= 2 else None,
        },
        "warnings": warnings + [w for w in soft_warnings if w not in warnings],
        "hard_warnings": warnings,
        "soft_warnings": soft_warnings,
        "warning_count": len(warnings) + len(soft_warnings),
        "hard_fail": bool(hard_fail),
        "policy": "QC treats under-driven premasters as correctable before mastering so the downstream limiter does not perform destructive rescue gain",
    }


def _apply_v6339_peak_safe_handoff_makeup(
    path: Path,
    qc: dict[str, Any],
    *,
    recipe: str,
    stem_metrics: list[dict[str, Any]] | None = None,
    original_decision: dict[str, Any] | None = None,
    reference_db: dict[str, Any] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Apply one final peak-safe premaster handoff makeup when v63.3.8 created headroom.

    v63.3.8 correctly changed the density curve so it could reduce peak cost, but
    it preserved RMS and could leave the premaster quieter than v63.3.7 after a
    conservative pre-trim.  This step is not a limiter or an additional mix
    candidate: it is a single linear gain applied only when the corrected
    premaster is below the recipe LUFS window and still has true-peak room before
    the premaster handoff ceiling.
    """
    report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".v6339_peak_safe_handoff_makeup",
        "enabled": _env_on("BUSY_BAMIX_V6339_PEAK_SAFE_HANDOFF_MAKEUP", "1"),
        "input_path": str(path),
        "applied": False,
        "reason": None,
    }
    if not report["enabled"]:
        report["reason"] = "disabled"
        return None, report
    if not isinstance(qc, dict):
        report["reason"] = "invalid_qc"
        return None, report
    report["input_hard_fail"] = bool(qc.get("hard_fail"))
    report["input_hard_warnings"] = list(qc.get("hard_warnings") or []) if isinstance(qc.get("hard_warnings"), list) else []
    try:
        targets = qc.get("premaster_targets") if isinstance(qc.get("premaster_targets"), dict) else _recipe_premaster_targets(recipe, stem_metrics or [], original_decision=original_decision, reference_db=reference_db)
        lufs = float(qc.get("integrated_lufs"))
        tp = float(qc.get("true_peak_dbtp"))
        required_gain = float(qc.get("estimated_mastering_gain_required_lufs") or 0.0)
        lufs_min = float(targets.get("lufs_min") or -15.5)
        lufs_target = float(targets.get("lufs_target") or (lufs_min + 1.0))
        expected_window = targets.get("expected_mastering_gain_window_lu") if isinstance(targets.get("expected_mastering_gain_window_lu"), (list, tuple)) else None
        expected_gain_max = float(expected_window[1]) if expected_window and len(expected_window) >= 2 else _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_GAIN_MAX_LU", 4.6, minimum=2.0, maximum=8.0)
        trigger_margin = _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_GAIN_EXCESS_TRIGGER_LU", 0.35, minimum=0.0, maximum=3.0)
        below_window = lufs < (lufs_min - _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_LUFS_TOLERANCE_LU", 0.12, minimum=0.0, maximum=1.0))
        gain_excess = required_gain > (expected_gain_max + trigger_margin)
        if not (below_window or gain_excess):
            report["reason"] = "premaster_density_within_handoff_window"
            report["lufs"] = round(lufs, 3)
            report["required_mastering_gain_lu"] = round(required_gain, 3)
            return None, report
        tp_limit = float(targets.get("tp_max") or -1.5) - _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_TP_MARGIN_DB", 0.12, minimum=0.0, maximum=1.0)
        tp_room = max(0.0, tp_limit - tp)
        # v63.4.0: for contract-driven handoff, aim far enough into the
        # target window to bring projected final-limiter work back under budget.
        # Existing v63.3.9 behavior only moved to lufs_min+0.35, which often
        # left +5~7 LU rescue gain for the limiter.
        if bool(targets.get("v6340_contract_applied")):
            desired_lufs = min(lufs_target, lufs_min + _env_float("BUSY_BAMIX_V6340_HANDOFF_MAKEUP_INTO_WINDOW_LU", 0.75, minimum=0.0, maximum=2.5))
        else:
            desired_lufs = min(lufs_target, lufs_min + _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_INTO_WINDOW_LU", 0.35, minimum=0.0, maximum=2.0))

        # v63.3.10: if v63.3.8/9 created some room but not enough to get
        # inside the premaster handoff window, add one more peak-efficient relief
        # pass before linear makeup.  This is a deterministic handoff repair, not
        # a second mix candidate or a final limiter.  It only accepts the relief
        # file if true-peak room improves and LUFS does not materially retreat.
        if _env_on("BUSY_BAMIX_V63310_PRE_MAKEUP_PEAK_RELIEF", "1"):
            try:
                requested_gain = max(0.0, float(desired_lufs) - float(lufs))
                missing_room = max(0.0, requested_gain - float(tp_room))
                crest_now = float(qc.get("crest_factor_db") or 0.0)
                if missing_room >= _env_float("BUSY_BAMIX_V63310_RELIEF_TRIGGER_MISSING_ROOM_DB", 0.28, minimum=0.0, maximum=2.0) and crest_now >= _env_float("BUSY_BAMIX_V63310_RELIEF_MIN_CREST_DB", 12.6 if bool(targets.get("v6340_contract_applied")) else 14.2, minimum=8.0, maximum=22.0):
                    y_relief, sr_relief = sf.read(str(path), always_2d=True, dtype="float32")
                    if y_relief.size > 0:
                        relief_drive = _env_float("BUSY_BAMIX_V63310_RELIEF_DENSITY_DRIVE_DB", 2.15, minimum=0.0, maximum=4.8)
                        y_relief2, relief_report = _apply_density_drive_block(y_relief.astype(np.float32, copy=False), relief_drive)
                        relief_path = Path(str(path).replace(".wav", "_v63310_peak_relief.wav"))
                        sf.write(str(relief_path), y_relief2.astype(np.float32, copy=False), int(sr_relief), subtype="FLOAT")
                        qc_relief = _premaster_qc(relief_path, sr=TARGET_SR, recipe=recipe, stem_metrics=stem_metrics or [], original_decision=original_decision, reference_db=reference_db)
                        relief_tp = float(qc_relief.get("true_peak_dbtp"))
                        relief_lufs = float(qc_relief.get("integrated_lufs"))
                        accepted = (relief_tp <= tp - _env_float("BUSY_BAMIX_V63310_RELIEF_MIN_TP_IMPROVEMENT_DB", 0.12, minimum=0.0, maximum=1.0)) and (relief_lufs >= lufs - _env_float("BUSY_BAMIX_V63310_RELIEF_MAX_LUFS_RETREAT_LU", 0.45, minimum=0.0, maximum=2.0))
                        report["v63310_peak_relief_before_makeup"] = {
                            "attempted": True, "accepted": bool(accepted), "missing_room_db": round(float(missing_room), 3),
                            "drive_db": round(float(relief_drive), 3), "density_report": relief_report,
                            "lufs_before": round(float(lufs), 3), "lufs_after": round(float(relief_lufs), 3),
                            "tp_before_dbtp": round(float(tp), 3), "tp_after_dbtp": round(float(relief_tp), 3),
                            "qc_after_hard_fail": bool(qc_relief.get("hard_fail")),
                        }
                        if accepted:
                            path = relief_path
                            qc = qc_relief
                            lufs = relief_lufs
                            tp = relief_tp
                            required_gain = float(qc.get("estimated_mastering_gain_required_lufs") or required_gain)
                            tp_room = max(0.0, tp_limit - tp)
                    else:
                        report["v63310_peak_relief_before_makeup"] = {"attempted": True, "accepted": False, "reason": "empty_audio"}
                else:
                    report["v63310_peak_relief_before_makeup"] = {"attempted": False, "reason": "not_enough_missing_room_or_crest", "missing_room_db": round(float(missing_room), 3), "crest_factor_db": round(float(crest_now), 3)}
            except Exception as relief_exc:
                report["v63310_peak_relief_before_makeup"] = {"attempted": True, "accepted": False, "reason": "exception", "error": str(relief_exc)[:240]}

        gain_db = min(
            max(0.0, desired_lufs - lufs),
            tp_room,
            _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_MAX_DB", 1.75 if bool(targets.get("v6340_contract_applied")) else 1.25, minimum=0.0, maximum=3.0),
        )
        report.update({
            "lufs_before": round(lufs, 3),
            "tp_before_dbtp": round(tp, 3),
            "desired_lufs": round(float(desired_lufs), 3),
            "tp_limit_dbtp": round(float(tp_limit), 3),
            "tp_room_db": round(float(tp_room), 3),
            "gain_db": round(float(gain_db), 3),
            "required_mastering_gain_before_lu": round(float(required_gain), 3),
            "expected_gain_max_lu": round(float(expected_gain_max), 3),
        })
        if gain_db <= _env_float("BUSY_BAMIX_V6339_HANDOFF_MAKEUP_MIN_DB", 0.25, minimum=0.0, maximum=1.0):
            report["reason"] = "insufficient_peak_room_for_safe_handoff_makeup"
            return None, report
        y, sr = sf.read(str(path), always_2d=True, dtype="float32")
        if y.size == 0:
            report["reason"] = "empty_audio"
            return None, report
        y = (y.astype(np.float32, copy=False) * np.float32(_amp(gain_db))).astype(np.float32, copy=False)
        out = Path(str(path).replace(".wav", "_v63310_handoff_makeup.wav"))
        sf.write(str(out), y, int(sr), subtype="FLOAT")
        report["applied"] = True
        report["reason"] = "peak_safe_linear_handoff_makeup_after_density_peak_relief_headroom"
        report["output_path"] = str(out)
        return out, report
    except Exception as exc:
        report["reason"] = "exception"
        report["error"] = str(exc)[:300]
        return None, report


def _salvage_premaster_after_hard_qc(
    path: Path,
    qc: dict[str, Any],
    *,
    recipe: str,
    stem_metrics: list[dict[str, Any]] | None = None,
    original_decision: dict[str, Any] | None = None,
    reference_db: dict[str, Any] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Last-ditch BAMix handoff salvage for v63.3.6.1.

    This is deliberately bus-level and deterministic: it does not create another
    candidate mix and it does not change musical balances.  It only prevents an
    otherwise useful BAMix premaster from aborting the whole job when the final
    premaster QC is slightly outside a technical guard (TP/phase).  Remaining
    density/crest warnings are handed to the mastering stage as telemetry.
    """
    report: dict[str, Any] = {
        "schema_version": _SCHEMA + ".qc_salvage_v63361",
        "enabled": _env_on("BUSY_BAMIX_V63361_QC_SALVAGE", "1"),
        "input_path": str(path),
        "actions": [],
        "applied": False,
        "reason": None,
    }
    if not report["enabled"]:
        report["reason"] = "disabled"
        return None, report
    if not isinstance(qc, dict) or not qc.get("hard_fail"):
        report["reason"] = "no_hard_fail"
        return None, report
    try:
        y, sr = sf.read(str(path), always_2d=True, dtype="float32")
        if y.size == 0:
            report["reason"] = "empty_audio"
            return None, report
        y = np.asarray(y, dtype=np.float32)
        targets = qc.get("premaster_targets") if isinstance(qc.get("premaster_targets"), dict) else _recipe_premaster_targets(recipe, stem_metrics or [], original_decision=original_decision, reference_db=reference_db)
        hard_warnings = list(qc.get("hard_warnings") or []) if isinstance(qc.get("hard_warnings"), list) else []

        # 1) True-peak/peak emergency trim.  The downstream mastering limiter can
        # restore loudness, but it should not receive a clipped/hot premaster.
        try:
            tp = float(qc.get("true_peak_dbtp"))
            tp_target = min(
                float(targets.get("tp_target") or -3.0),
                _env_float("BUSY_BAMIX_V63361_SALVAGE_TP_TARGET_DBTP", -2.2, minimum=-6.0, maximum=-0.5),
            )
            if ("true_peak_over_hard_fail" in hard_warnings) or tp > _env_float("BUSY_AUTOMIX_PREMASTER_HARD_TP_FAIL_DBTP", 0.1, minimum=-12.0, maximum=1.0):
                trim_db = min(0.0, tp_target - tp - _env_float("BUSY_BAMIX_V63361_SALVAGE_TP_MARGIN_DB", 0.20, minimum=0.0, maximum=1.5))
                if trim_db < -0.05:
                    y *= np.float32(10.0 ** (trim_db / 20.0))
                    report["actions"].append({"type": "whole_mix_true_peak_trim", "gain_db": round(float(trim_db), 3), "tp_before_db": round(float(tp), 3), "target_dbtp": round(float(tp_target), 3)})
        except Exception as exc:
            report.setdefault("action_errors", []).append({"type": "tp_trim", "error": str(exc)[:200]})

        # 2) Stereo phase salvage.  Narrow side energy only when QC already marked
        # correlation as hard-dangerous; do not collapse width for normal material.
        try:
            corr = float(qc.get("phase_correlation"))
            min_corr = _env_float("BUSY_AUTOMIX_PREMASTER_MIN_CORRELATION", 0.12, minimum=-0.5, maximum=0.9)
            if y.shape[1] >= 2 and (("phase_correlation_low" in hard_warnings) or corr < min_corr):
                width = _env_float("BUSY_BAMIX_V63361_SALVAGE_WIDTH_SCALAR", 0.68, minimum=0.2, maximum=1.0)
                mid = 0.5 * (y[:, 0] + y[:, 1])
                side = 0.5 * (y[:, 0] - y[:, 1]) * np.float32(width)
                y[:, 0] = mid + side
                y[:, 1] = mid - side
                report["actions"].append({"type": "phase_safe_width_trim", "width_scalar": round(float(width), 4), "corr_before": round(float(corr), 4), "min_corr": round(float(min_corr), 4)})
        except Exception as exc:
            report.setdefault("action_errors", []).append({"type": "width_trim", "error": str(exc)[:200]})

        # 3) High-crest/under-driven handoff density salvage.  v63.3.6.1 only
        # salvaged TP/phase, so a crest_factor_under_glued_hard_fail could still
        # pass downstream unchanged and force the final limiter to do +6 LUFS of
        # rescue gain.  This full-file, deterministic bus compaction is still not
        # a final limiter: it rounds isolated peaks, raises RMS density modestly,
        # and then applies only peak-safe makeup toward the premaster handoff
        # window.
        try:
            all_warnings = set(str(w) for w in (qc.get("warnings") or [])) | set(hard_warnings)
            targets = qc.get("premaster_targets") if isinstance(qc.get("premaster_targets"), dict) else targets
            mastering_target = _env_float("BUSY_AUTOMIX_EXPECTED_MASTERING_TARGET_LUFS", _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0), minimum=-16.0, maximum=-8.0)
            expected_window = targets.get("expected_mastering_gain_window_lu") if isinstance(targets.get("expected_mastering_gain_window_lu"), (list, tuple)) else None
            expected_gain_max = float(expected_window[1]) if expected_window and len(expected_window) >= 2 else _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_GAIN_MAX_LU", 4.6, minimum=2.0, maximum=8.0)
            required_gain = float(qc.get("estimated_mastering_gain_required_lufs") or (mastering_target - float(qc.get("integrated_lufs") or -99.0)))
            high_crest_density_need = ("crest_factor_under_glued_hard_fail" in all_warnings) or (required_gain > expected_gain_max + _env_float("BUSY_BAMIX_V6337_SALVAGE_GAIN_EXCESS_TRIGGER_LU", 0.55, minimum=0.0, maximum=3.0))
            if high_crest_density_need and _env_on("BUSY_BAMIX_V6337_UNDERDRIVEN_DENSITY_SALVAGE", "1"):
                before_peak = _db(_peak(y))
                before_rms = _db(_rms(y))
                before_crest = float(qc.get("crest_factor_db") or 0.0)
                drive_db = _env_float("BUSY_BAMIX_V6337_SALVAGE_DENSITY_DRIVE_DB", 3.2, minimum=0.0, maximum=7.5)
                wet = _env_float("BUSY_BAMIX_V6337_SALVAGE_DENSITY_WET", 0.58, minimum=0.05, maximum=1.0)
                drive = _amp(drive_db)
                # Use unity-small-signal soft clipping here, not normalized tanh.
                # The goal is to create TP/headroom by rounding isolated peaks;
                # any loudness lift must come from the explicit peak-safe makeup
                # below, not from an uncontrolled saturation gain.
                soft = np.tanh(y.astype(np.float32, copy=False) * float(drive)) / max(float(drive), 1e-6)
                y = (y.astype(np.float32, copy=False) * (1.0 - wet) + soft.astype(np.float32, copy=False) * wet).astype(np.float32, copy=False)
                post_fast = analyze_audio_fast_qc(y, int(sr))
                post_lufs = post_fast.get("integrated_lufs")
                post_tp = post_fast.get("approx_true_peak_dbfs") or post_fast.get("true_peak_dbfs") or _db(_peak(y))
                lufs_floor = max(float(targets.get("lufs_min") or -15.2), float(mastering_target) - float(expected_gain_max))
                desired_lufs = min(float(targets.get("lufs_target") or lufs_floor), lufs_floor + _env_float("BUSY_BAMIX_V6337_SALVAGE_INTO_WINDOW_LU", 0.35, minimum=0.0, maximum=1.5))
                tp_limit = float(targets.get("tp_max") or -1.5) - _env_float("BUSY_BAMIX_V6337_SALVAGE_TP_MARGIN_DB", 0.12, minimum=0.0, maximum=0.8)
                try:
                    makeup = min(
                        max(0.0, desired_lufs - float(post_lufs)),
                        max(0.0, tp_limit - float(post_tp)),
                        _env_float("BUSY_BAMIX_V6337_SALVAGE_MAX_MAKEUP_DB", 1.8, minimum=0.0, maximum=4.0),
                    )
                except Exception:
                    makeup = 0.0
                if makeup > 0.05:
                    y *= np.float32(_amp(makeup))
                # Enforce the same TP handoff ceiling after density/makeup.  This
                # keeps the salvage from fixing crest by simply creating a hotter
                # premaster.  True peak is approximated by the fast analyzer, then
                # sample peak guard below remains as a final safety net.
                final_fast = analyze_audio_fast_qc(y, int(sr))
                final_tp = final_fast.get("approx_true_peak_dbfs") or final_fast.get("true_peak_dbfs") or _db(_peak(y))
                post_density_trim_db = 0.0
                try:
                    if float(final_tp) > float(tp_limit):
                        post_density_trim_db = float(tp_limit) - float(final_tp)
                        y *= np.float32(_amp(post_density_trim_db))
                except Exception:
                    pass
                after_peak = _db(_peak(y))
                after_rms = _db(_rms(y))
                after_crest = after_peak - after_rms
                report["actions"].append({
                    "type": "underdriven_high_crest_density_handoff_salvage",
                    "drive_db": round(float(drive_db), 3),
                    "wet": round(float(wet), 3),
                    "makeup_db": round(float(makeup), 3),
                    "post_density_trim_db": round(float(post_density_trim_db), 3),
                    "required_mastering_gain_before_lu": round(float(required_gain), 3),
                    "expected_gain_max_lu": round(float(expected_gain_max), 3),
                    "peak_before_dbfs": round(float(before_peak), 3),
                    "peak_after_dbfs": round(float(after_peak), 3),
                    "crest_before_db": round(float(before_crest), 3),
                    "crest_after_proxy_db": round(float(after_crest), 3),
                    "desired_lufs": round(float(desired_lufs), 3),
                    "tp_limit_dbfs": round(float(tp_limit), 3),
                })
        except Exception as exc:
            report.setdefault("action_errors", []).append({"type": "underdriven_density_salvage", "error": str(exc)[:200]})

        if not report["actions"]:
            report["reason"] = "no_salvage_action_for_hard_warning"
            return None, report
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if math.isfinite(peak) and peak > 0.999:
            safe_gain = 0.999 / max(peak, 1e-9)
            y *= np.float32(safe_gain)
            report["actions"].append({"type": "sample_peak_guard_after_salvage", "gain_db": round(float(_db(safe_gain)), 3), "peak_before": round(float(peak), 6)})
        out = Path(str(path).replace(".wav", "_qc_salvaged.wav"))
        sf.write(str(out), y.astype(np.float32, copy=False), int(sr), subtype="FLOAT")
        report["applied"] = True
        report["output_path"] = str(out)
        report["action_count"] = len(report["actions"])
        return out, report
    except Exception as exc:
        report["reason"] = "exception"
        report["error"] = str(exc)[:500]
        return None, report


def _correction_from_qc(qc: dict[str, Any]) -> dict[str, Any]:
    gain_db = 0.0
    width = 1.0
    density_drive_db = 0.0
    reasons: list[str] = []
    targets = qc.get("premaster_targets") if isinstance(qc.get("premaster_targets"), dict) else {}
    tp = qc.get("true_peak_dbtp")
    lufs = qc.get("integrated_lufs")
    preferred_tp = float(targets.get("tp_target") or _env_float("BUSY_AUTOMIX_PREMASTER_TARGET_TP_DBTP", -3.0, minimum=-7.0, maximum=-1.0))
    target_lufs = float(targets.get("lufs_target") or _env_float("BUSY_AUTOMIX_PREMASTER_LUFS_TARGET", -16.0, minimum=-22.0, maximum=-12.0))
    hard_warnings = set(str(w) for w in (qc.get("hard_warnings") or []) if isinstance(w, str))
    soft_warnings = set(str(w) for w in (qc.get("soft_warnings") or qc.get("warnings") or []) if isinstance(w, str))
    underdriven_high_crest = bool(qc.get("underdriven_premaster")) or "crest_factor_under_glued_hard_fail" in hard_warnings
    try:
        v6350_plan = qc.get("v6350_density_limiter_workload_plan") if isinstance(qc.get("v6350_density_limiter_workload_plan"), dict) else {}
        if _env_on("BUSY_BAMIX_V6350_WORKLOAD_ROUTER", "1") and isinstance(v6350_plan, dict) and v6350_plan.get("active"):
            rec_drive = float(v6350_plan.get("recommended_density_drive_db") or 0.0)
            rec_makeup = float(v6350_plan.get("recommended_tp_safe_makeup_db") or 0.0)
            if rec_drive > float(density_drive_db or 0.0) + 0.03:
                density_drive_db = rec_drive
                reasons.append("v6350_density_limiter_workload_router_drive")
            if rec_makeup > max(0.0, float(gain_db or 0.0)) + 0.03:
                gain_db = max(float(gain_db or 0.0), rec_makeup)
                reasons.append("v6350_density_limiter_workload_router_makeup")
    except Exception:
        pass
    try:
        tp_f0 = float(tp)
        # v63.3.8: do not make a high-crest/underdriven premaster even quieter
        # merely to hit the conservative recipe tp_target.  In that case the
        # downstream limiter has to do destructive rescue gain.  Use tp_max as
        # the correction ceiling and let density compaction create headroom first.
        if underdriven_high_crest:
            correction_tp_target = float(targets.get("tp_max") or preferred_tp) - _env_float("BUSY_BAMIX_V6337_UNDERDRIVEN_TP_MAX_MARGIN_DB", 0.12, minimum=0.0, maximum=0.8)
            reason_code = "attenuate_only_to_recipe_tp_max_for_underdriven_density_handoff"
            extra_margin = _env_float("BUSY_BAMIX_V6337_UNDERDRIVEN_TP_TRIM_EXTRA_DB", 0.04, minimum=0.0, maximum=0.5)
        else:
            correction_tp_target = preferred_tp
            reason_code = "attenuate_to_recipe_true_peak_target"
            extra_margin = 0.15
        over = tp_f0 - float(correction_tp_target)
        if over > 0:
            gain_db -= over + extra_margin
            reasons.append(reason_code)
    except Exception:
        pass
    try:
        # Under-driven and plenty of TP headroom: correct inside BAMix with a single
        # deterministic gain rerender.  This specifically fixes the -23 LUFS / -8 dBTP case.
        tp_f = float(tp)
        lufs_f = float(lufs)
        lufs_min = float(targets.get("lufs_min") or (target_lufs - 2.0))
        tp_min = float(targets.get("tp_min") or (preferred_tp - 1.5))
        max_safe_mastering_gain = _env_float("BUSY_AUTOMIX_MAX_SAFE_MASTERING_GAIN_LUFS", 7.0, minimum=3.0, maximum=14.0)
        try:
            mastering_gain_required = float(qc.get("estimated_mastering_gain_required_lufs"))
        except Exception:
            mastering_gain_required = target_lufs - lufs_f
        if bool(qc.get("underdriven_premaster")) and tp_f < preferred_tp - 0.75:
            max_gain_by_tp = max(0.0, preferred_tp - tp_f)
            max_gain_by_lufs = max(0.0, target_lufs - lufs_f)
            boost = min(max_gain_by_tp, max_gain_by_lufs, _env_float("BUSY_AUTOMIX_UNDERDRIVEN_MAX_GAIN_DB", 7.0, minimum=0.5, maximum=12.0))
            if boost > 0.15:
                gain_db += boost
                reasons.append("raise_underdriven_premaster_to_recipe_window")
        elif bool(qc.get("underdriven_premaster")):
            # Peak is not low enough for a safe pure linear boost.  Apply a small
            # deterministic density drive during the one allowed correction rerender
            # and allow limited makeup gain; the renderer's normalized tanh curve
            # rounds peaks instead of turning this into a mastering limiter.
            density_drive_db = _env_float("BUSY_AUTOMIX_UNDERDRIVEN_DENSITY_DRIVE_DB", 2.35, minimum=0.0, maximum=4.8)
            # Only add makeup when there is real peak headroom.  If true peak is
            # already above/near the recipe target, the density curve itself is the
            # correction and any linear makeup would undo the peak attenuation.
            peak_headroom_to_target = max(0.0, preferred_tp - tp_f - _env_float("BUSY_AUTOMIX_DENSITY_MAKEUP_TP_MARGIN_DB", 0.35, minimum=0.0, maximum=2.0))
            makeup = min(
                max(0.0, target_lufs - lufs_f),
                peak_headroom_to_target,
                _env_float("BUSY_AUTOMIX_UNDERDRIVEN_DENSITY_MAKEUP_DB", 2.0, minimum=0.0, maximum=5.0),
                _env_float("BUSY_AUTOMIX_UNDERDRIVEN_MAX_GAIN_DB", 7.0, minimum=0.5, maximum=12.0),
            )
            if makeup > 0.1:
                gain_db += makeup
                reasons.append("underdriven_density_makeup_with_peak_headroom")
            reasons.append("underdriven_high_crest_density_drive_applied")
        elif _env_on("BUSY_BAMIX_V6313_1_SOFT_TARGET_CORRECTION", "1"):
            # v63.1.3.1: the aligned proxy estimator can avoid the old -7.5 dB clamp,
            # but a first render may still land just below the recipe window.  Do not
            # call this a hard failure; use the one deterministic correction only when
            # both loudness/TP are low or mastering would need more gain than the BAMix
            # handoff policy permits.  This keeps the first render professional without
            # forcing modules to act when evidence is low.
            soft_low_window = (lufs_f < lufs_min and tp_f < tp_min)
            safe_gain_exceeded = mastering_gain_required > (max_safe_mastering_gain + _env_float("BUSY_BAMIX_V6313_1_SAFE_GAIN_MARGIN_LU", 0.25, minimum=0.0, maximum=2.0))
            enough_tp_room = tp_f < preferred_tp - _env_float("BUSY_BAMIX_V6313_1_SOFT_TP_ROOM_DB", 0.60, minimum=0.0, maximum=3.0)
            if enough_tp_room and (soft_low_window or safe_gain_exceeded):
                desired_lufs = min(target_lufs, lufs_min + _env_float("BUSY_BAMIX_V6313_1_SOFT_LUFS_INTO_WINDOW_LU", 0.75, minimum=0.0, maximum=2.0))
                max_gain_by_tp = max(0.0, (preferred_tp - _env_float("BUSY_BAMIX_V6313_1_TP_TARGET_MARGIN_DB", 0.15, minimum=0.0, maximum=1.0)) - tp_f)
                max_gain_by_lufs = max(0.0, desired_lufs - lufs_f)
                boost = min(max_gain_by_tp, max_gain_by_lufs, _env_float("BUSY_BAMIX_V6313_1_SOFT_CORRECTION_MAX_GAIN_DB", 3.5, minimum=0.5, maximum=7.0))
                if boost > _env_float("BUSY_BAMIX_V6313_1_SOFT_CORRECTION_MIN_GAIN_DB", 0.35, minimum=0.05, maximum=2.0):
                    gain_db += boost
                    reasons.append("raise_soft_low_premaster_to_recipe_window")
                    if safe_gain_exceeded:
                        reasons.append("reduce_mastering_gain_requirement_before_handoff")
    except Exception:
        pass
    try:
        # v63.2: if the premaster is structurally healthy but still below the
        # commercial premaster window, use the single allowed correction render for
        # bus-level density completion.  This is not a final mastering limiter:
        # it combines bounded harmonic density with only the TP-safe makeup that
        # remains before the premaster true-peak target.
        if _env_on("BUSY_BAMIX_V632_DENSITY_COMPLETION_CORRECTION", "1"):
            tp_f = float(tp)
            lufs_f = float(lufs)
            crest_f = float(qc.get("crest_factor_db") or 0.0)
            corr_f = float(qc.get("phase_correlation") or 1.0)
            lufs_min = float(targets.get("lufs_min") or (target_lufs - 1.5))
            healthy_for_density = corr_f >= _env_float("BUSY_BAMIX_V632_DENSITY_MIN_CORR", 0.35, minimum=-0.5, maximum=0.95) and crest_f >= _env_float("BUSY_BAMIX_V632_DENSITY_MIN_CREST_DB", 10.2, minimum=5.0, maximum=18.0)
            density_gap = max(0.0, float(target_lufs) - float(lufs_f))
            expected_window = targets.get("expected_mastering_gain_window_lu") if isinstance(targets.get("expected_mastering_gain_window_lu"), (list, tuple)) else None
            expected_gain_max = float(expected_window[1]) if expected_window and len(expected_window) >= 2 else _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_GAIN_MAX_LU", 4.6, minimum=2.0, maximum=8.0)
            try:
                mastering_gain_required = float(qc.get("estimated_mastering_gain_required_lufs"))
            except Exception:
                mastering_gain_required = _env_float("BUSY_BAMIX_V632_EXPECTED_MASTERING_TARGET_LUFS", -9.25, minimum=-12.5, maximum=-8.0) - lufs_f
            gain_excess = max(0.0, mastering_gain_required - expected_gain_max)
            below_window = lufs_f < (lufs_min - _env_float("BUSY_BAMIX_V632_LUFS_MIN_TOLERANCE_LU", 0.15, minimum=0.0, maximum=1.5))
            gain_window_exceeded = gain_excess >= _env_float("BUSY_BAMIX_V6322_GAIN_WINDOW_TRIGGER_LU", 0.35, minimum=0.0, maximum=3.0)
            if healthy_for_density and (below_window or gain_window_exceeded or density_gap >= _env_float("BUSY_BAMIX_V632_DENSITY_GAP_TRIGGER_LU", 1.65, minimum=0.4, maximum=5.0)):
                tp_max = float(targets.get("tp_max") or -1.5)
                tp_drive_room = max(0.0, tp_max - tp_f - _env_float("BUSY_BAMIX_V6322_DRIVE_TP_MAX_MARGIN_DB", 0.12, minimum=0.0, maximum=1.0))
                drive_cap_by_tp = _env_float("BUSY_BAMIX_V6322_DENSITY_DRIVE_BASE_CAP_DB", 1.20, minimum=0.0, maximum=3.0) + _env_float("BUSY_BAMIX_V6322_DENSITY_DRIVE_TP_ROOM_SCALE", 1.70, minimum=0.0, maximum=4.0) * float(np.clip(tp_drive_room / 1.0, 0.0, 1.0))
                if bool(qc.get("underdriven_premaster")) or "crest_factor_under_glued_hard_fail" in hard_warnings:
                    drive_cap_by_tp = max(drive_cap_by_tp, _env_float("BUSY_BAMIX_V6337_HIGH_CREST_DENSITY_DRIVE_MIN_CAP_DB", 2.65, minimum=0.0, maximum=5.0))
                drive_candidate = _env_float("BUSY_BAMIX_V6322_DENSITY_DRIVE_BASE_DB", 1.15, minimum=0.0, maximum=3.0) + _env_float("BUSY_BAMIX_V6322_DENSITY_DRIVE_GAP_SCALE", 0.42, minimum=0.0, maximum=1.2) * density_gap + _env_float("BUSY_BAMIX_V6322_DENSITY_DRIVE_GAIN_EXCESS_SCALE", 0.38, minimum=0.0, maximum=1.2) * gain_excess
                extra_drive = min(
                    _env_float("BUSY_BAMIX_V632_DENSITY_COMPLETION_DRIVE_DB", 3.05, minimum=0.0, maximum=4.0),
                    max(0.0, drive_candidate),
                    max(0.0, drive_cap_by_tp),
                )
                if extra_drive > float(density_drive_db or 0.0) + 0.05:
                    density_drive_db = float(extra_drive)
                    if _env_on("BUSY_BAMIX_V6338_PEAK_EFFICIENT_DENSITY_DRIVE", "1"):
                        reasons.append("v6338_peak_efficient_density_drive_gain_window")
                    else:
                        reasons.append("v6337_commercial_premaster_density_drive_gain_window")
                    if gain_window_exceeded:
                        reasons.append("v6322_reduce_mastering_gain_excess_before_handoff")
                peak_headroom = max(0.0, preferred_tp - tp_f - _env_float("BUSY_BAMIX_V632_MAKEUP_TP_MARGIN_DB", 0.20, minimum=0.0, maximum=1.5))
                makeup = min(
                    max(0.0, target_lufs - lufs_f),
                    peak_headroom,
                    _env_float("BUSY_BAMIX_V632_DENSITY_COMPLETION_MAKEUP_DB", 1.25, minimum=0.0, maximum=3.5),
                )
                prev_gain_db = float(gain_db or 0.0)
                if makeup > max(0.1, prev_gain_db + 0.05):
                    # `makeup` is a total TP-safe linear makeup allowance toward the
                    # premaster target, not an incremental amount on top of earlier
                    # soft/underdriven gain corrections. Use it as the new floor so
                    # v63.2 density completion cannot double-count linear gain.
                    gain_db = max(prev_gain_db, float(makeup))
                    reasons.append("v632_tp_safe_premaster_density_makeup")
    except Exception:
        pass
    try:
        if float(qc.get("phase_correlation")) < _env_float("BUSY_AUTOMIX_PREMASTER_MIN_CORRELATION", 0.12):
            width = _env_float("BUSY_AUTOMIX_PHASE_CORRECTION_WIDTH_SCALAR", 0.72, minimum=0.2, maximum=1.0)
            reasons.append("reduce_side_energy_for_phase_correlation")
    except Exception:
        pass
    try:
        # v63.3.9: when peak-efficient density is active for an under-driven/high-crest
        # premaster, do not pre-trim the render by a large amount before the density
        # curve has had a chance to create peak relief.  v63.3.8 applied the trim first,
        # then preserved RMS, which made corrected premasters quieter than the input.
        if (
            _env_on("BUSY_BAMIX_V6339_DEFER_TP_TRIM_TO_PEAK_EFFICIENT_DENSITY", "1")
            and bool(underdriven_high_crest)
            and float(density_drive_db or 0.0) > 1e-6
            and float(gain_db or 0.0) < 0.0
            and _env_on("BUSY_BAMIX_V6338_PEAK_EFFICIENT_DENSITY_DRIVE", "1")
        ):
            max_pretrim = _env_float("BUSY_BAMIX_V6339_MAX_PRE_DENSITY_TRIM_DB", 0.12, minimum=0.0, maximum=1.5)
            if float(gain_db) < -float(max_pretrim):
                reasons.append("v6339_defer_excess_tp_trim_to_peak_efficient_density_and_handoff_makeup")
                gain_db = -float(max_pretrim)
    except Exception:
        pass
    return {"gain_db": round(gain_db, 3), "width_correction": round(width, 4), "density_drive_db": round(density_drive_db, 3), "reasons": reasons, "v6350_density_limiter_workload_router": qc.get("v6350_density_limiter_workload_plan") if isinstance(qc.get("v6350_density_limiter_workload_plan"), dict) else {}, "policy": "exactly one deterministic correction rerender; no additional candidates; v63.5.0 uses the premaster handoff contract to route excess limiter workload into upstream density/peak-relief/makeup before final limiting"}




def _v6313_2_module_authority_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Compact aggregate module authority handoff for v63.1.3.3/1.4.

    The previous debug brief could make a module look "inactive" because it
    surfaced the last block's decision label.  This aggregate summary separates
    last-block telemetry from the report authority label used by downstream
    readers.
    """
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}

    def _pick(d: dict[str, Any], *keys: str) -> Any:
        for k in keys:
            if isinstance(d, dict) and d.get(k) is not None:
                return d.get(k)
        return None

    def _f(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return float(default)

    def _decision_label(active: bool, applied: bool, avg_need: Any, max_need: Any, actual_max: Any) -> str:
        need = max(_f(avg_need), _f(max_need))
        amount = abs(_f(actual_max))
        if applied and amount > 0.05:
            if need >= 0.55:
                return "aggregate_need_confirmed_actual_applied"
            return "aggregate_low_need_but_safety_actual_applied"
        if active and need >= 0.55:
            return "aggregate_need_detected_but_bounded_or_bypassed"
        if active:
            return "aggregate_active_low_need_no_material_action"
        return "aggregate_inactive_or_unavailable"

    def _module(name: str, actual_avg_keys: tuple[str, ...], actual_max_keys: tuple[str, ...]) -> dict[str, Any]:
        d = runtime.get(name) if isinstance(runtime.get(name), dict) else {}
        avg_need = _pick(d, "need_score_avg", "avg_need_score")
        max_need = _pick(d, "need_score_max", "max_need_score", "need_score")
        actual_avg = _pick(d, *actual_avg_keys, "actual_amount_avg_db")
        actual_max = _pick(d, *actual_max_keys, "actual_amount_max_db")
        try:
            applied_actual = bool(d.get("applied_actual")) or abs(float(actual_max or 0.0)) > 0.05
        except Exception:
            applied_actual = bool(d.get("applied_actual") or d.get("applied"))
        active = bool(d.get("active"))
        label = _decision_label(active, applied_actual, avg_need, max_need, actual_max)
        return _jsonable({
            "active": active,
            "applied_actual": applied_actual,
            "need_score_avg": avg_need,
            "need_score_max": max_need,
            "actual_amount_avg_db": actual_avg,
            "actual_amount_max_db": actual_max,
            "module_authority_decision_label": label,
            "decision_label_authority": "aggregate_need_actual_summary_not_last_block",
            "last_block_need_score": d.get("need_score"),
            "last_block_decision": d.get("decision"),
            "last_block_decision_label_note": "debug-only; not module authority",
            "aggregate_decision": d.get("aggregate_decision"),
            "need_actual_alignment": d.get("need_actual_alignment"),
            "actual_applied_fraction": d.get("actual_applied_fraction") or d.get("active_fraction"),
        })

    summary = {
        "schema_version": _SCHEMA + ".module_authority_summary_v6313_3_label_cleanup",
        "active": bool(runtime),
        "policy": "Module authority is the aggregate need/actual label; final-block decisions are kept only as debug telemetry.",
        "glue": _module("glue", ("avg_gr_abs_db", "avg_glue_gr_db", "avg_gr_db"), ("max_gr_abs_db", "max_glue_gr_db", "max_gr_db")),
        "kick_bass": _module("kick_bass", ("avg_duck_db",), ("max_duck_db",)),
        "vocal_pocket": _module("vocal_pocket", ("avg_pocket_cut_db",), ("max_pocket_cut_db",)),
        "drum_punch": _module("drum_punch", ("avg_rounding_drive_db",), ("max_rounding_drive_db", "drive_db")),
        "harmonic_density": _module("harmonic_density", ("avg_density_drive_db",), ("max_density_drive_db", "drive_db")),
        "translation_qc": runtime.get("translation_qc") if isinstance(runtime.get("translation_qc"), dict) else {},
    }
    return _jsonable(summary)



def _v6370_body_center_vocal_support_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}
    aug = runtime.get("stem_augmentation") if isinstance(runtime.get("stem_augmentation"), dict) else {}

    def _m(name: str) -> dict[str, Any]:
        d = aug.get(name) if isinstance(aug.get(name), dict) else {}
        return d

    low_mid = _m("low_mid_body_fill")
    vocal = _m("vocal_support_body_layer")
    center = _m("center_anchor")
    bus_guard = _m("bus_peak_guard")
    modules = {
        "low_mid_body_fill": {
            "active": bool(low_mid.get("active")),
            "applied": bool(low_mid.get("applied")),
            "applied_fraction": low_mid.get("applied_fraction"),
            "assist_rms_db": low_mid.get("assist_rms_db"),
            "vocal_duck_db": low_mid.get("vocal_duck_db"),
            "vocal_fund_duck_db": low_mid.get("v6343_vocal_fund_duck_db"),
            "mud_rollback_db": low_mid.get("v6343_mud_rollback_db"),
            "low_mid_delta_db": low_mid.get("v6343_low_mid_delta_db"),
            "ms_low_mid_separation": low_mid.get("v6370_mid_side_low_mid_separation"),
            "method": low_mid.get("method"),
        },
        "vocal_support_body_layer": {
            "active": bool(vocal.get("active")),
            "applied": bool(vocal.get("applied")),
            "applied_fraction": vocal.get("applied_fraction"),
            "assist_rms_db": vocal.get("assist_rms_db"),
            "conflict_duck_db": vocal.get("conflict_duck_db"),
            "mud_rollback_db": vocal.get("mud_rollback_db"),
            "low_mid_delta_db": vocal.get("low_mid_delta_db"),
            "center_owned_mono_layer": vocal.get("center_owned_mono_layer"),
            "method": vocal.get("method"),
        },
        "center_anchor": {
            "active": bool(center.get("active")),
            "applied": bool(center.get("applied")),
            "applied_fraction": center.get("applied_fraction"),
            "anchor_rms_db": center.get("anchor_rms_db"),
            "side_lowmid_trim_db": center.get("v6370_side_lowmid_trim_db"),
            "side_lowmid_over_mid_db": center.get("v6370_side_lowmid_over_mid_db"),
            "correlation_pre": center.get("correlation_pre"),
            "correlation_post": center.get("correlation_post"),
            "method": center.get("method"),
        },
    }
    applied_any = any(bool(v.get("applied")) for v in modules.values())
    guard_rollbacks = []
    for label, d in [("low_mid_body_fill", low_mid), ("vocal_support_body_layer", vocal), ("center_anchor", center)]:
        for key in ("v6343_mud_rollback_db", "mud_rollback_db", "conflict_duck_db", "v6370_side_lowmid_trim_db", "v6370_peak_guard_db"):
            try:
                val = float(d.get(key)) if isinstance(d, dict) and d.get(key) is not None else 0.0
            except Exception:
                val = 0.0
            if val < -0.01:
                guard_rollbacks.append({"module": label, "metric": key, "value_db": round(float(val), 3)})
    return _jsonable({
        "schema_version": _SCHEMA + ".body_center_vocal_support_v6370",
        "active": bool(_env_on("BUSY_BAMIX_V6370_BODY_CENTER_VOCAL_SUPPORT", "1")),
        "applied": bool(applied_any),
        "capability_id": "body_center_vocal_support",
        "router_decision": "execute_guarded_dsp_when_role_evidence_and_need_are_present",
        "modules": modules,
        "guards": {
            "mud_rollback": True,
            "vocal_priority_guard": True,
            "mid_side_low_mid_separation": True,
            "bus_peak_guard_active": bool(bus_guard.get("active")),
            "bus_peak_guard_scale_min": bus_guard.get("scale_min"),
        },
        "rollback": {
            "guard_rollbacks": guard_rollbacks,
            "policy": "rollback/downscale body support when mud, vocal conflict, side low-mid or peak pressure increases; warning labels are preserved upstream.",
        },
        "pre_metrics": {
            "problem_summary": ((mod.get("strategy") or {}).get("stem_augmentation") or {}).get("problem_summary") if isinstance((mod.get("strategy") or {}).get("stem_augmentation"), dict) else None,
        },
        "post_metrics": {
            "low_mid_body_assist_rms_db": low_mid.get("assist_rms_db"),
            "vocal_support_assist_rms_db": vocal.get("assist_rms_db"),
            "center_anchor_rms_db": center.get("anchor_rms_db"),
        },
        "accepted": True,
        "reject_or_bypass_reasons": [],
        "policy": "low_mid_bottleneck/center_hollow/vocal body support are handled by dedicated body-center-vocal DSP, not by DML target chasing, limiter push, warning hiding or per-song exceptions.",
    })




def _v6380_bass_harmonic_translation_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}
    aug = runtime.get("stem_augmentation") if isinstance(runtime.get("stem_augmentation"), dict) else {}
    bass = aug.get("bass_harmonic_translation") if isinstance(aug.get("bass_harmonic_translation"), dict) else {}
    bus_guard = aug.get("bus_peak_guard") if isinstance(aug.get("bus_peak_guard"), dict) else {}
    harmonic_generation = bass.get("harmonic_generation") if isinstance(bass.get("harmonic_generation"), dict) else {}
    mono_anchor = bass.get("mono_anchor") if isinstance(bass.get("mono_anchor"), dict) else {}
    dc_blocker = bass.get("dc_blocker") if isinstance(bass.get("dc_blocker"), dict) else {}
    conflict = bass.get("vocal_low_mid_conflict_notch") if isinstance(bass.get("vocal_low_mid_conflict_notch"), dict) else {}
    guards = bass.get("guards") if isinstance(bass.get("guards"), dict) else {}
    guard_rollbacks = []
    for key in ("side_low_trim_db",):
        try:
            val = float(mono_anchor.get(key)) if mono_anchor.get(key) is not None else 0.0
        except Exception:
            val = 0.0
        if val < -0.01:
            guard_rollbacks.append({"module": "bass_harmonic_translation", "metric": key, "value_db": round(float(val), 3)})
    try:
        vduck = float(conflict.get("duck_db")) if conflict.get("duck_db") is not None else 0.0
    except Exception:
        vduck = 0.0
    if vduck < -0.01:
        guard_rollbacks.append({"module": "bass_harmonic_translation", "metric": "vocal_low_mid_conflict_notch.duck_db", "value_db": round(float(vduck), 3)})
    try:
        pguard = float(guards.get("peak_guard_db")) if guards.get("peak_guard_db") is not None else 0.0
    except Exception:
        pguard = 0.0
    if pguard < -0.01:
        guard_rollbacks.append({"module": "bass_harmonic_translation", "metric": "peak_guard_db", "value_db": round(float(pguard), 3)})
    return _jsonable({
        "schema_version": _SCHEMA + ".bass_harmonic_translation_v6380",
        "active": bool(_env_on("BUSY_BAMIX_V6380_BASS_HARMONIC_TRANSLATION", "1")),
        "applied": bool(bass.get("applied")),
        "capability_id": "bass_harmonic_translation",
        "router_decision": "execute_guarded_bass_harmonic_translation_when_input_relative_bass_translation_need_is_present",
        "method": bass.get("method"),
        "applied_fraction": bass.get("applied_fraction"),
        "bass_translation_amount": bass.get("bass_translation_amount"),
        "harmonic_generation": harmonic_generation,
        "second_harmonic_ratio": bass.get("second_harmonic_ratio", bass.get("second_harmonic_ratio_max")),
        "third_harmonic_ratio": bass.get("third_harmonic_ratio", bass.get("third_harmonic_ratio_max")),
        "mono_anchor": mono_anchor,
        "dc_blocker": dc_blocker,
        "vocal_low_mid_conflict_notch": conflict,
        "guards": {
            **guards,
            "bus_peak_guard_active": bool(bus_guard.get("active")),
            "bus_peak_guard_scale_min": bus_guard.get("scale_min"),
        },
        "rollback": {
            "guard_rollbacks": guard_rollbacks,
            "policy": "downscale harmonic layer or trim low side when DC, mono, vocal-conflict or peak pressure increases; warning labels are preserved upstream.",
        },
        "pre_metrics": {
            "problem_summary": ((mod.get("strategy") or {}).get("stem_augmentation") or {}).get("problem_summary") if isinstance((mod.get("strategy") or {}).get("stem_augmentation"), dict) else None,
            "fundamental_rms_db": bass.get("fundamental_rms_db"),
            "existing_harmonic_rms_db": bass.get("existing_harmonic_rms_db"),
            "harmonic_deficit_db": bass.get("harmonic_deficit_db"),
        },
        "post_metrics": {
            "assist_rms_db": bass.get("assist_rms_db"),
            "assist_peak_dbfs": bass.get("assist_peak_dbfs"),
            "side_low_trim_db": mono_anchor.get("side_low_trim_db"),
            "vocal_conflict_duck_db": conflict.get("duck_db"),
        },
        "accepted": True,
        "reject_or_bypass_reasons": [] if bool(bass.get("applied")) else [bass.get("last_bypass_reason") or bass.get("reason") or "not_applied_in_final_render"],
        "policy": "Bass translation is handled by dedicated Chebyshev 2nd/3rd harmonic DSP with DC blocker, strict mono anchor and vocal-conflict notch; no sub boost, target chasing, limiter push or DML rescue is used.",
    })


def _v6390_side_texture_stereo_cleanliness_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}
    aug = runtime.get("stem_augmentation") if isinstance(runtime.get("stem_augmentation"), dict) else {}
    side = aug.get("side_texture_control") if isinstance(aug.get("side_texture_control"), dict) else {}
    bus_guard = aug.get("bus_peak_guard") if isinstance(aug.get("bus_peak_guard"), dict) else {}
    suppressor = side.get("side_high_hash_fizz_suppressor") if isinstance(side.get("side_high_hash_fizz_suppressor"), dict) else {}
    stereo_guard = side.get("stereo_cleanliness_guard") if isinstance(side.get("stereo_cleanliness_guard"), dict) else {}
    ambience = side.get("ambience_collapse_detection") if isinstance(side.get("ambience_collapse_detection"), dict) else {}
    mono = side.get("mono_fold_down_rollback") if isinstance(side.get("mono_fold_down_rollback"), dict) else {}
    guards = side.get("guards") if isinstance(side.get("guards"), dict) else {}
    rollback_reasons = []
    for label, block in [("ambience_collapse_detection", ambience), ("mono_fold_down_rollback", mono)]:
        if isinstance(block, dict) and bool(block.get("rollback_active")):
            rollback_reasons.append({"module": "side_texture_control", "guard": label, "reason": block.get("rollback_reason") or "rollback_active"})
    if side.get("rollback_reason") not in (None, "", "none"):
        rollback_reasons.append({"module": "side_texture_control", "guard": "aggregate", "reason": side.get("rollback_reason")})
    if side.get("peak_trim_db") is not None:
        try:
            if float(side.get("peak_trim_db") or 0.0) < -0.01:
                rollback_reasons.append({"module": "side_texture_control", "guard": "peak_guard", "value_db": round(float(side.get("peak_trim_db")), 3)})
        except Exception:
            pass
    applied = bool(side.get("applied"))
    out = {
        "schema_version": _SCHEMA + ".side_texture_stereo_cleanliness_v6390",
        "active": bool(_env_on("BUSY_BAMIX_V6390_SIDE_TEXTURE_STEREO_CLEANLINESS", "1")),
        "applied": applied,
        "capability_id": "side_texture_stereo_cleanliness",
        "router_decision": "execute_side_only_band_limited_hash_fizz_suppression_when_side_relative_roughness_is_present",
        "side_texture_control": {
            "active": bool(side.get("active")),
            "applied": bool(side.get("applied")),
            "method": side.get("method"),
            "applied_fraction": side.get("applied_fraction"),
            "need_score": side.get("need_score"),
            "suppression_db": side.get("suppression_db", side.get("duck_db")),
            "affected_band_hz": side.get("detected_band_hz"),
            "rollback_reason": side.get("rollback_reason"),
        },
        "side_high_hash_fizz_suppressor": suppressor or {
            "active": bool(side.get("active")),
            "applied": bool(side.get("applied")),
            "detected_band_hz": side.get("detected_band_hz"),
            "hash_risk": side.get("hash_risk", side.get("need_score")),
            "suppression_db": side.get("suppression_db", side.get("duck_db")),
        },
        "stereo_cleanliness_guard": stereo_guard or {
            "active": bool(side.get("active")),
            "side_incoherence_risk": None,
            "center_protection_active": None,
            "width_change_db_or_ratio": {"total_side_change_db": side.get("total_side_change_db"), "side_width_ratio": side.get("width_change_ratio")},
        },
        "ambience_collapse_detection": ambience or {"active": bool(side.get("active")), "risk": None, "rollback_active": False, "rollback_reason": "not_measured"},
        "mono_fold_down_rollback": mono or {"active": bool(side.get("active")), "compatibility_risk": None, "rollback_active": False, "rollback_reason": "not_measured"},
        "guards": {
            **guards,
            "bus_peak_guard_active": bool(bus_guard.get("active")),
            "bus_peak_guard_scale_min": bus_guard.get("scale_min"),
        },
        "rollback": {
            "guard_rollbacks": rollback_reasons,
            "rollback_reason": side.get("rollback_reason") or (rollback_reasons[-1].get("reason") if rollback_reasons else "none"),
            "policy": "side cleanup is rolled back or downscaled when center/vocal brightness, ambience depth, mono fold-down or peak/crest safety is at risk; warning labels are preserved upstream.",
        },
        "pre_metrics": {
            "problem_summary": ((mod.get("strategy") or {}).get("stem_augmentation") or {}).get("problem_summary") if isinstance((mod.get("strategy") or {}).get("stem_augmentation"), dict) else None,
            "side_high_over_mid_high_db": side.get("side_high_over_mid_high_db"),
            "selected_band_side_mid_excess_db": side.get("selected_band_side_mid_excess_db"),
            "selected_band_spectral_flatness": side.get("selected_band_spectral_flatness"),
            "correlation_pre": side.get("correlation_pre"),
        },
        "post_metrics": {
            "suppression_db": side.get("suppression_db", side.get("duck_db")),
            "high_side_change_db": side.get("high_side_change_db"),
            "total_side_change_db": side.get("total_side_change_db"),
            "correlation_post": side.get("correlation_post"),
            "mono_delta_db": (mono or {}).get("mono_delta_db") if isinstance(mono, dict) else None,
        },
        "accepted": True,
        "reject_or_bypass_reasons": [] if applied else [side.get("last_bypass_reason") or side.get("reason") or "not_applied_in_final_render"],
        "policy": "Side-high hash/fizz and stereo cleanliness are handled by dedicated side-only dynamic EQ with center/vocal protection, ambience collapse detection and mono fold-down rollback; no DML rescue, target chase, broad high shelf, warning hiding or simple width collapse is used.",
    }
    # Convenience aliases requested by downstream report readers.
    out["side_texture_control.active"] = out["side_texture_control"].get("active")
    out["side_texture_control.applied"] = out["side_texture_control"].get("applied")
    out["side_texture_control.method"] = out["side_texture_control"].get("method")
    return _jsonable(out)


def _v645_deterministic_quality_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}
    aug = runtime.get("stem_augmentation") if isinstance(runtime.get("stem_augmentation"), dict) else {}
    residue_map = final.get("v645_stem_neural_codec_residue_map") if isinstance(final.get("v645_stem_neural_codec_residue_map"), dict) else runtime.get("v645_stem_neural_codec_residue_map", {})
    residue_pressure = final.get("v645_residue_pressure") if isinstance(final.get("v645_residue_pressure"), dict) else runtime.get("v645_residue_pressure", {})
    erb = aug.get("v645_erb_ms_resonance_suppressor") if isinstance(aug.get("v645_erb_ms_resonance_suppressor"), dict) else {}
    body = aug.get("v645_upward_body_density_polynomial") if isinstance(aug.get("v645_upward_body_density_polynomial"), dict) else {}
    side = aug.get("side_texture_control") if isinstance(aug.get("side_texture_control"), dict) else {}
    bus = aug.get("bus_peak_guard") if isinstance(aug.get("bus_peak_guard"), dict) else {}
    coord = bus.get("v645_body_density_peak_guard_coordinator") if isinstance(bus.get("v645_body_density_peak_guard_coordinator"), dict) else {}
    return _jsonable({
        "schema_version": _SCHEMA + ".deterministic_quality_summary_v645",
        "active": bool(_env_on("BUSY_BAMIX_V645_STEM_RESIDUE_MAP", "1") or _env_on("BUSY_BAMIX_V645_ERB_MS_RESONANCE_SUPPRESSOR", "1")),
        "actual_full_premaster_render_policy": {
            "single_full_premaster_render": True,
            "fallback_render_allowed": False,
            "virtual_or_proxy_selection_only": True,
        },
        "stem_neural_codec_residue_map": residue_map,
        "residue_pressure": residue_pressure,
        "erb_ms_dynamic_resonance_suppressor": {
            "active": bool(erb.get("active")),
            "applied": bool(erb.get("applied")),
            "applied_fraction": erb.get("applied_fraction"),
            "rms_delta_db": erb.get("rms_delta_db"),
            "peak_delta_db": erb.get("peak_delta_db"),
            "crest_delta_db": erb.get("crest_delta_db"),
            "flux_ratio": erb.get("flux_ratio"),
            "rollback_reasons": erb.get("rollback_reasons"),
            "method": erb.get("method"),
        },
        "side_hf_residue_application": {
            "active": bool(side.get("active")),
            "applied": bool(side.get("applied")),
            "hash_risk": side.get("hash_risk", side.get("need_score")),
            "v645_residue_witness_floor": side.get("v645_residue_witness_floor"),
            "vocal_protection_active": residue_pressure.get("vocal_protection_active") if isinstance(residue_pressure, dict) else None,
            "effective_side_hf_hash_pressure": residue_pressure.get("effective_side_hf_hash_pressure") if isinstance(residue_pressure, dict) else None,
            "suppression_db": side.get("suppression_db", side.get("duck_db")),
            "method": side.get("method"),
        },
        "upward_body_density_polynomial": {
            "active": bool(body.get("active")),
            "applied": bool(body.get("applied")),
            "applied_fraction": body.get("applied_fraction"),
            "blend_db": body.get("blend_db"),
            "peak_trim_db": body.get("peak_trim_db"),
            "crest_delta_db": body.get("crest_delta_db"),
            "sub_delta_db": body.get("sub_delta_db"),
            "body_delta_db": body.get("body_delta_db"),
            "validation_rollback_reasons": body.get("validation_rollback_reasons"),
            "method": body.get("method"),
        },
        "body_density_peak_guard_coordinator": {
            **coord,
            "bus_peak_guard_scale_min": bus.get("scale_min"),
            "bus_peak_guard_delta_preservation_mode": bus.get("delta_preservation_mode"),
            "whole_mix_trim_db": bus.get("whole_mix_trim_db"),
        },
        "forbidden_operations": [
            "phase_inversion_cancellation",
            "master_bus_spectral_subtraction",
            "full_mix_diffusion_restoration",
            "blind_audio_super_resolution_or_bwe",
            "vocal_generation_or_vocoder_repair",
        ],
        "policy": "v64.5 improves clean/full/dense/polished quality through deterministic M/S dynamic EQ and source-derived density only; residue handling is suppression/plausible repair, not true source restoration.",
    })


def _v6464_transient_hf_ownership_summary(render_attempts: list[dict[str, Any]] | None) -> dict[str, Any]:
    attempts = render_attempts if isinstance(render_attempts, list) else []
    final = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    mod = final.get("v631_professional_module_render") if isinstance(final.get("v631_professional_module_render"), dict) else {}
    runtime = mod.get("module_runtime") if isinstance(mod.get("module_runtime"), dict) else {}
    aug = runtime.get("stem_augmentation") if isinstance(runtime.get("stem_augmentation"), dict) else {}
    ownership = aug.get("v6464_transient_hf_ownership_lock") if isinstance(aug.get("v6464_transient_hf_ownership_lock"), dict) else {}
    consolidator = aug.get("v6464_assist_delta_consolidator") if isinstance(aug.get("v6464_assist_delta_consolidator"), dict) else {}
    seam = aug.get("v6463_output_seam_smoother") if isinstance(aug.get("v6463_output_seam_smoother"), dict) else {}
    block = runtime.get("v6463_adaptive_block_sizing") if isinstance(runtime.get("v6463_adaptive_block_sizing"), dict) else final.get("v6463_adaptive_block_sizing", {})
    smr = runtime.get("source_morphology_repair") if isinstance(runtime.get("source_morphology_repair"), dict) else {}
    cache_reports = smr.get("cache_reports") if isinstance(smr.get("cache_reports"), list) else []
    smr_seams = []
    for row in cache_reports[:12]:
        if isinstance(row, dict) and isinstance(row.get("v6463_smr_stem_cache_seam_smoother"), dict):
            smr_seams.append(row.get("v6463_smr_stem_cache_seam_smoother"))
    return _jsonable({
        "schema_version": _SCHEMA + ".transient_hf_ownership_summary_v6464",
        "active": bool(_env_on("BUSY_BAMIX_V6464_TRANSIENT_HF_OWNERSHIP_LOCK", "1")),
        "adaptive_block_sizing": block,
        "transient_hf_ownership_lock": ownership,
        "assist_delta_consolidator": consolidator,
        "output_seam_smoother": seam,
        "smr_stem_cache_seam_smoother_examples": smr_seams,
        "single_render_policy": {
            "full_premaster_render_attempt_count": len(attempts),
            "extra_audio_render_added_by_patch": False,
        },
        "policy": "v64.6.4 consolidates overlapping transient/HF assist layers and smooths block seams inside the existing single BAMix render path.",
    })


def _v6313_3_handoff_damage_budget(qc: dict[str, Any] | None, recipe: str | None = None) -> dict[str, Any]:
    """Report-only BAMix -> mastering safe-push budget hint."""
    q = qc if isinstance(qc, dict) else {}
    def _num(key: str) -> float | None:
        try:
            v = float(q.get(key))
            if math.isfinite(v):
                return v
        except Exception:
            pass
        return None
    lufs = _num("integrated_lufs")
    crest = _num("crest_factor_db")
    lra = _num("lra_lu")
    recipe_s = str(recipe or "").strip().lower()
    punch = recipe_s == "punch_preserved"
    base_target = _env_float("BUSY_AUTOMIX_MASTERING_TARGET_LUFS", -11.5, minimum=-14.0, maximum=-9.5)
    base_max_gain = _env_float("BUSY_AUTOMIX_MASTERING_MAX_TOTAL_GAIN_LUFS", 7.0, minimum=3.0, maximum=12.0)
    max_gain = float(base_max_gain)
    max_finish_push = _env_float("BUSY_AUTOMIX_MASTERING_FINISH_MAX_PUSH_DB", 2.0, minimum=0.0, maximum=4.0)
    max_crest_loss = _env_float("BUSY_AUTOMIX_MASTERING_MAX_CREST_LOSS_DB", 4.0, minimum=1.0, maximum=8.0)
    reasons: list[str] = []
    if punch:
        max_crest_loss = min(max_crest_loss, _env_float("BUSY_BAMIX_V6314_PUNCH_MAX_CREST_LOSS_DB", 2.85, minimum=1.0, maximum=4.0))
        max_finish_push = min(max_finish_push, _env_float("BUSY_BAMIX_V6314_PUNCH_FINISH_PUSH_CAP_DB", 1.35, minimum=0.2, maximum=3.0))
        reasons.append("punch_preserved_soft_chase_budget")
    if lra is not None and lra < _env_float("BUSY_BAMIX_V6314_FRAGILE_LRA_LU", 3.0, minimum=0.5, maximum=6.0):
        max_gain = min(max_gain, _env_float("BUSY_BAMIX_V6314_FRAGILE_LRA_MAX_GAIN_LU", 4.5, minimum=1.0, maximum=9.0))
        max_finish_push = min(max_finish_push, _env_float("BUSY_BAMIX_V6314_FRAGILE_LRA_FINISH_PUSH_DB", 1.15, minimum=0.0, maximum=3.5))
        reasons.append("premaster_lra_fragile_push_cap_hint")
    if crest is not None and crest < _env_float("BUSY_BAMIX_V6314_FRAGILE_CREST_DB", 10.0, minimum=4.0, maximum=14.0):
        max_gain = min(max_gain, _env_float("BUSY_BAMIX_V6314_FRAGILE_CREST_MAX_GAIN_LU", 4.8, minimum=1.0, maximum=9.0))
        max_finish_push = min(max_finish_push, _env_float("BUSY_BAMIX_V6314_FRAGILE_CREST_FINISH_PUSH_DB", 1.25, minimum=0.0, maximum=3.5))
        reasons.append("premaster_crest_fragile_push_cap_hint")
    recommended_target = min(float(base_target), float(lufs) + float(max_gain)) if lufs is not None else float(base_target)
    return _jsonable({
        "schema_version": _SCHEMA + ".mastering_handoff_budget_v6313_3_report_marker",
        "active": bool(_env_on("BUSY_BAMIX_V6313_3_HANDOFF_BUDGET_REPORT", "1")),
        "recipe": recipe_s or None,
        "punch_preserved": bool(punch),
        "premaster_lufs": round(float(lufs), 3) if lufs is not None else None,
        "premaster_crest_factor_db": round(float(crest), 3) if crest is not None else None,
        "premaster_lra_lu": round(float(lra), 3) if lra is not None else None,
        "base_target_lufs": round(float(base_target), 3),
        "recommended_final_target_lufs": round(float(recommended_target), 3),
        "recommended_max_total_gain_lufs": round(float(max_gain), 3),
        "recommended_max_finish_push_db": round(float(max_finish_push), 3),
        "recommended_max_crest_loss_db": round(float(max_crest_loss), 3),
        "reasons": reasons,
        "policy": "Report marker only; worker handoff policy may consume the same premaster QC to cap final push.",
    })


def _v6313_estimator_feedback(render_attempts: list[dict[str, Any]] | None, qc_attempts: list[dict[str, Any]] | None, correction: dict[str, Any] | None) -> dict[str, Any]:
    """Report-only feedback for the v63.1.3 pre-render estimator."""
    attempts = render_attempts if isinstance(render_attempts, list) else []
    qcs = qc_attempts if isinstance(qc_attempts, list) else []
    first = attempts[0] if attempts and isinstance(attempts[0], dict) else {}
    second = attempts[1] if len(attempts) > 1 and isinstance(attempts[1], dict) else {}
    q1 = qcs[0] if qcs and isinstance(qcs[0], dict) else {}
    qf = qcs[-1] if qcs and isinstance(qcs[-1], dict) else {}
    est = first.get("prerender_gain_estimator") if isinstance(first.get("prerender_gain_estimator"), dict) else {}
    gain = correction.get("gain_db") if isinstance(correction, dict) else None
    try:
        gain_f = float(gain)
    except Exception:
        gain_f = None
    reasons = correction.get("reasons") if isinstance(correction, dict) else []
    reasons = reasons if isinstance(reasons, list) else []
    soft_target_correction = "raise_soft_low_premaster_to_recipe_window" in [str(r) for r in reasons]
    repeated_underdrive = (bool(q1.get("underdriven_premaster")) or soft_target_correction) and gain_f is not None and gain_f > _env_float("BUSY_BAMIX_V6313_FEEDBACK_LARGE_CORRECTION_DB", 2.0, minimum=0.5, maximum=6.0)
    first_lufs = q1.get("integrated_lufs")
    final_lufs = qf.get("integrated_lufs")
    try:
        lufs_delta = float(final_lufs) - float(first_lufs)
    except Exception:
        lufs_delta = None
    return _jsonable({
        "schema_version": _SCHEMA + ".prerender_estimator_feedback_v6313_2",
        "active": True,
        "first_render_underdriven": bool(q1.get("underdriven_premaster")),
        "first_render_soft_below_recipe_window": ("integrated_lufs_below_recipe_target_window" in (q1.get("soft_warnings") or []) or "true_peak_below_recipe_target_window" in (q1.get("soft_warnings") or [])),
        "soft_target_correction_applied": bool(soft_target_correction),
        "correction_gain_db": round(float(gain_f), 3) if gain_f is not None else None,
        "lufs_delta_after_correction": round(float(lufs_delta), 3) if lufs_delta is not None else None,
        "repeated_correction_dependency_detected": repeated_underdrive,
        "initial_premix_gain_db": first.get("initial_premix_gain_db"),
        "corrected_total_global_gain_db": second.get("total_global_gain_db"),
        "estimator_authority": est.get("authority"),
        "aligned_proxy_selected_window": (est.get("aligned_proxy") or {}).get("selected_window") if isinstance(est.get("aligned_proxy"), dict) else None,
        "recommendation": "calibrated_proxy_safety_still_overconservative" if repeated_underdrive else ("soft_target_correction_used_to_enter_recipe_window" if soft_target_correction else "estimator_within_expected_feedback_range"),
        "policy": "report-only feedback; no cross-job learning state or additional render is introduced",
    })

def build_busy_auto_mixing_premaster(
    *,
    stem_zip_path: str | Path,
    original_stereo_path: str | Path,
    work_dir: str | Path,
    original_features: dict[str, Any] | None = None,
    original_decision: dict[str, Any] | None = None,
    log_callback: Any | None = None,
    private_docs_zip_path: str | Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Build a single Busy Auto Mixing premaster from uploaded stems.

    v63.0 foundation rules:
    - opt-in only; caller decides whether the user selected Busy Auto Mixing
    - stems are the intended source; original stereo is only a reference anchor
    - one AI planner call max; rule fallback if unavailable
    - no full-length multi-candidate render
    - at most one deterministic QC correction re-render
    """
    def log(event: str, **fields: Any) -> None:
        if callable(log_callback):
            try:
                log_callback(event, **_jsonable(fields))
            except Exception:
                pass

    work = Path(work_dir)
    stem_zip = Path(stem_zip_path)
    original = Path(original_stereo_path)
    report: dict[str, Any] = {
        "schema_version": _SCHEMA,
        "requested": True,
        "enabled": _env_on("BUSY_AUTO_MIXING", os.environ.get("BUSY_AUTOMIX", "1")),
        "mode": "single_recipe_premaster_then_master",
        "original_stereo_used_as": "reference_anchor",
        "mastering_input_source": "busy_auto_mixing_premaster",
        "candidate_render_policy": {
            "multi_candidate_full_render_forbidden": True,
            "full_length_candidate_count_max": 1,
            "full_premaster_render_attempt_count_max": 1,
            "max_correction_rerenders": 0,
            "single_premaster_render_lock": _env_on("BUSY_AUTOMIX_SINGLE_PREMASTER_RENDER", "1"),
            "policy_note": "one selected mix recipe; BAMix writes one full premaster render by default. QC correction is reported as a virtual handoff plan, not executed as another full stem render.",
            "v643_gemini_sum_l_r_mid_side_rough_mix_advisory": _env_on("BUSY_BAMIX_GEMINI_MIX_ADVISORY", os.environ.get("BUSY_GEMINI_AUDIO_JUDGE", "1")),
            "v643_gemini_advisory_role": "weak producer prior before GPT-5.5 planner; no direct DSP authority",
            "v632_commercial_premaster_density": _env_on("BUSY_BAMIX_V632_COMMERCIAL_PREMASTER_DENSITY", "1"),
            "v633_gpt_guided_stem_augmentation": _env_on("BUSY_BAMIX_V633_STEM_AUGMENTATION", "1"),
                "v6380_bass_harmonic_translation": _env_on("BUSY_BAMIX_V6380_BASS_HARMONIC_TRANSLATION", "1"),
                "v6390_side_texture_stereo_cleanliness": _env_on("BUSY_BAMIX_V6390_SIDE_TEXTURE_STEREO_CLEANLINESS", "1"),
        },
        "ai_policy": {
            "planner_model": os.environ.get("BUSY_AUTOMIX_AI_MODEL", "gpt-5.5"),
            "single_call": True,
            "max_ai_calls": 1,
            "fallback": "rule_based_recipe_selector",
            "v633_existing_call_extended_for_stem_augmentation": True,
        },
    }
    if not report["enabled"]:
        report.update({"available": False, "status": "disabled", "reason": "BUSY_AUTO_MIXING_disabled"})
        return None, report

    reference_db, reference_db_asset, reference_db_zip = _v6341_load_private_reference_db(private_docs_zip_path)
    report["v6341_reference_db_handoff_source"] = {
        "enabled": _env_on("BUSY_BAMIX_V6341_REFERENCE_DB_CONTRACT", "1"),
        "loaded": isinstance(reference_db, dict),
        "asset": reference_db_asset,
        "zip_path_present": bool(reference_db_zip),
        "policy": "use pre-BAMix reference_v7/decision profile plus private genre_mastering_reference DB before recipe bucket fallback",
    }

    try:
        log("busy_auto_mixing_preflight_start", stem_zip=str(stem_zip))
        stems, preflight = _preflight_zip(stem_zip, work)
        report["technical_preflight"] = preflight
        report["duration_authority"] = _duration_authority(original, stems, original_features or {})
        if preflight.get("technical_failure"):
            report.update({"available": False, "status": "technical_failure", "reason": "technical_preflight_failed", "failure_reasons": preflight.get("failure_reasons", [])})
            log("busy_auto_mixing_preflight_failed", reasons=preflight.get("failure_reasons"))
            return None, report
        log("busy_auto_mixing_preflight_done", readable_stems=len(stems), total_uncompressed_mb=preflight.get("total_uncompressed_mb"))

        stem_metrics: list[dict[str, Any]] = []
        for s in stems:
            try:
                stem_metrics.append(_analyze_stem_proxy(Path(str(s.get("local_path"))), str(s.get("filename") or "stem")))
            except Exception as exc:
                stem_metrics.append({"filename": s.get("filename"), "role": "unknown", "role_confidence": 0.0, "artifact_risk": 1.0, "analysis_error": str(exc)[:300]})
        report["stem_count"] = len(stems)
        report["stem_metrics"] = stem_metrics
        report["role_map"] = {str(m.get("filename")): str(m.get("role") or "unknown") for m in stem_metrics}
        report["role_summary"] = _build_role_summary(stem_metrics)

        reference = _reference_summary(original_features or {}, original)
        reference["duration_authority"] = report.get("duration_authority")
        report["reference_anchor"] = reference

        fallback = _rule_select_recipe(stem_metrics, reference)
        gemini_mix_advisory = _build_and_run_bamix_gemini_mix_advisory(
            stems,
            stem_metrics,
            str(fallback.get("selected_mix_recipe") or "clean_balanced_professional"),
            original_features=original_features if isinstance(original_features, dict) else {},
            reference=reference,
            fallback_recipe=fallback,
            work_dir=work,
            log_callback=log,
        )
        report["gemini_stem_mix_observation_advisory"] = gemini_mix_advisory
        compact_payload = {
            "stem_count": len(stem_metrics),
            "role_summary": report["role_summary"],
            "reference_anchor": reference,
            "rule_recommendation": fallback,
            "gemini_stem_mix_observation_advisory": {
                "available": gemini_mix_advisory.get("available"),
                "enabled": gemini_mix_advisory.get("enabled"),
                "reason": gemini_mix_advisory.get("reason"),
                "advisory_role": gemini_mix_advisory.get("advisory_role"),
                "direct_dsp_authority": False,
                "producer_prior": gemini_mix_advisory.get("parsed", {}) if isinstance(gemini_mix_advisory.get("parsed"), dict) else {},
                "observation_summary": {
                    k: v for k, v in (gemini_mix_advisory.get("observation_summary") or {}).items()
                    if k not in {"views"} or str(os.environ.get("BUSY_BAMIX_GEMINI_PAYLOAD_INCLUDE_VIEW_DETAIL", "0")).lower() in {"1", "true", "yes", "on"}
                } if isinstance(gemini_mix_advisory.get("observation_summary"), dict) else {},
                "fusion_policy": gemini_mix_advisory.get("fusion_policy", {}),
            },
            "mastering_context": {
                "selected_profile": (original_decision or {}).get("selected_profile") if isinstance(original_decision, dict) else None,
                "mode": (original_decision or {}).get("mode") if isinstance(original_decision, dict) else None,
                "processing_flags_preview": ((original_decision or {}).get("processing_flags") or [])[:24] if isinstance((original_decision or {}).get("processing_flags"), list) else [],
            },
            "stem_augmentation_problem_summary": _stem_augmentation_problem_summary(stem_metrics, reference),
            "stem_augmentation_registry": {
                "policy": "derive assist layers only from provided stems using deterministic DSP; no external generation or new musical composition",
                "effective_v6380_executable_modules": ["bass_harmonic_translation", "low_mid_body_fill", "vocal_support_body_layer", "center_anchor", "drum_parallel_density", "side_texture_control", "transient_ghost"],
                "effective_v6370_executable_modules": ["bass_harmonic_translation", "low_mid_body_fill", "vocal_support_body_layer", "center_anchor", "drum_parallel_density", "side_texture_control", "transient_ghost"],
                "effective_v6336_executable_modules": ["bass_harmonic_translation", "low_mid_body_fill", "center_anchor", "drum_parallel_density", "side_texture_control", "transient_ghost"],
                "effective_v645_deterministic_quality_modules": ["stem_neural_codec_residue_map", "erb_ms_dynamic_resonance_suppressor", "side_hf_residue_witness_floor", "upward_body_density_polynomial", "body_density_peak_guard_coordinator"],
                "deferred_modules": ["short_room_early_reflection"],
            },
        }
        ai = _call_gpt_planner(compact_payload)
        final_plan = _clamp_recipe(ai, fallback, stem_metrics, reference=reference)
        recipe = str(final_plan.get("selected_mix_recipe") or "clean_balanced_professional")
        report["mix_strategy"] = final_plan
        report["v631_mix_strategy"] = final_plan.get("v631_mix_strategy") if isinstance(final_plan, dict) else {}
        if isinstance(report["v631_mix_strategy"], dict):
            report["v631_mix_strategy"]["gemini_stem_mix_observation_advisory"] = {
                "available": gemini_mix_advisory.get("available"),
                "enabled": gemini_mix_advisory.get("enabled"),
                "reason": gemini_mix_advisory.get("reason"),
                "used_by_gpt55_planner": bool(((final_plan.get("ai_planner") or {}).get("planner") or {}).get("gemini_advisory_use") if isinstance(final_plan.get("ai_planner"), dict) else False),
                "policy": "weak Gemini SUM/L/R/MID/SIDE rough-stem listener prior; deterministic module clamps remain final authority",
            }
        report["selected_mix_recipe"] = recipe
        report["premaster_target_matrix"] = _recipe_premaster_targets(recipe, stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
        log("busy_auto_mixing_recipe_selected", recipe=recipe, source=final_plan.get("planner_source"), ai_available=ai.get("available"), ai_model=ai.get("model"))

        premaster = work / "busy_auto_mixing_premaster_float32.wav"
        smr_cache_dir = work / "bamix_smr_stem_cache"
        report["source_morphology_repair_stem_cache"] = {
            "schema_version": _SCHEMA + ".source_morphology_repair_stem_cache_v6444_1",
            "enabled": bool(_env_on("BUSY_BAMIX_SMR_STEM_CACHE", "1")),
            "cache_dir": str(smr_cache_dir),
            "cache_domain": "post_role_gain_width_pre_global_gain",
            "policy": "needed stems are source-conditioned once in the same post-role-gain/width domain as the original inline SMR path; the v64.4.4.2 single-premaster lock normally makes correction rerender reuse unnecessary",
        }
        render1 = _render_single_premaster(stems, stem_metrics, recipe, premaster, target_sr=TARGET_SR, mix_strategy=report.get("v631_mix_strategy"), original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db, source_morphology_cache_dir=smr_cache_dir)
        qc1 = _premaster_qc(premaster, sr=TARGET_SR, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
        report["render_attempts"] = [render1]
        report["premaster_qc_attempts"] = [qc1]
        correction = _correction_from_qc(qc1)
        single_premaster_render_lock = _env_on("BUSY_AUTOMIX_SINGLE_PREMASTER_RENDER", "1")
        requested_correction_rerenders = _env_int("BUSY_AUTOMIX_MAX_CORRECTION_RERENDERS", 0, minimum=0, maximum=3)
        max_correction_rerenders = 0 if single_premaster_render_lock else requested_correction_rerenders
        report["virtual_premaster_correction_plan"] = {
            "schema_version": _SCHEMA + ".virtual_premaster_correction_plan_v6444_2",
            "active": bool(correction.get("reasons")),
            "applied_as_actual_rerender": False,
            "first_correction": correction,
            "policy": "QC correction remains telemetry/handoff guidance unless BUSY_AUTOMIX_SINGLE_PREMASTER_RENDER=0 explicitly unlocks legacy full-premaster correction rerenders.",
        }
        report["correction_policy"] = {
            "single_premaster_render_lock": bool(single_premaster_render_lock),
            "requested_max_correction_rerenders": requested_correction_rerenders,
            "max_correction_rerenders": max_correction_rerenders,
            "first_correction": correction,
            "policy": "single actual BAMix premaster render by default; correction rerenders are disabled to preserve virtual-plan-then-one-render behavior and reduce runtime",
        }
        qc_final = qc1
        active_correction = correction
        for corr_idx in range(int(max_correction_rerenders)):
            hard_or_correctable = bool(qc_final.get("hard_fail") or active_correction.get("reasons"))
            if not (hard_or_correctable and _env_on("BUSY_AUTOMIX_CORRECTION_RERENDER", "1") and active_correction.get("reasons")):
                break
            corrected = work / f"busy_auto_mixing_premaster_float32_corrected_{corr_idx + 1}.wav"
            render2 = _render_single_premaster(
                stems,
                stem_metrics,
                recipe,
                corrected,
                target_sr=TARGET_SR,
                correction_gain_db=float(active_correction.get("gain_db") or 0.0),
                width_correction=float(active_correction.get("width_correction") or 1.0),
                density_drive_db=float(active_correction.get("density_drive_db") or 0.0),
                mix_strategy=report.get("v631_mix_strategy"),
                original_decision=original_decision if isinstance(original_decision, dict) else {},
                reference_db=reference_db,
                source_morphology_cache_dir=smr_cache_dir,
            )
            qc2 = _premaster_qc(corrected, sr=TARGET_SR, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
            report["render_attempts"].append(render2)
            report["premaster_qc_attempts"].append(qc2)
            if isinstance(report.get("virtual_premaster_correction_plan"), dict):
                report["virtual_premaster_correction_plan"]["applied_as_actual_rerender"] = True
            premaster = corrected
            qc_final = qc2
            active_correction = _correction_from_qc(qc_final)
            report["correction_policy"]["last_correction"] = active_correction
            if not active_correction.get("reasons"):
                break
        report["premaster_qc"] = qc_final
        report["correction_loops_executed"] = max(0, len(report.get("render_attempts", [])) - 1)
        report["full_premaster_render_attempt_count"] = len(report.get("render_attempts", []))
        report["correction_rerender_count"] = report["correction_loops_executed"]
        if report.get("virtual_premaster_correction_plan", {}).get("active") and not report.get("correction_loops_executed"):
            report["virtual_premaster_correction_plan"]["not_applied_reason"] = "single_premaster_render_lock"
        # v63.1.1 report compatibility aliases.  Some downstream/debug readers
        # look for these compact keys; keep them synchronized with the canonical
        # selected_mix_recipe/correction_loops_executed fields.
        report["recipe"] = recipe
        report["selected_recipe"] = recipe
        report["correction_loops"] = report["correction_loops_executed"]
        report["render_attempt_count"] = report["full_premaster_render_attempt_count"]
        try:
            cache_rows = []
            for _rr in (report.get("render_attempts") or []):
                _smr = (((_rr.get("v631_professional_module_render") or {}).get("module_runtime") or {}).get("source_morphology_repair") or {}) if isinstance(_rr, dict) else {}
                _cache = _smr.get("stem_cache") if isinstance(_smr, dict) and isinstance(_smr.get("stem_cache"), dict) else {}
                if _cache:
                    cache_rows.append(_cache)
            if cache_rows:
                cache_attempted_count = int(sum(int(r.get("cache_attempted_count") or r.get("stem_cache_count") or 0) for r in cache_rows))
                cache_used_count = int(sum(int(r.get("cache_used_count") or 0) for r in cache_rows))
                cache_hit_count = int(sum(int(r.get("cache_hit_count") or 0) for r in cache_rows))
                cache_miss_build_count = int(sum(int(r.get("cache_miss_build_count") or 0) for r in cache_rows))
                built_block_count = int(sum(int(r.get("built_block_count") or 0) for r in cache_rows))
                reused_block_count = int(sum(int(r.get("reused_block_count") or 0) for r in cache_rows))
                report["source_morphology_repair_stem_cache"].update({
                    "attempt_count": int(len(cache_rows)),
                    "enabled": bool(any(r.get("enabled") for r in cache_rows)),
                    "cache_active": bool(cache_used_count > 0),
                    "cache_attempted_count": cache_attempted_count,
                    "cache_used_count": cache_used_count,
                    "cache_hit_count": cache_hit_count,
                    "cache_miss_build_count": cache_miss_build_count,
                    "built_block_count": built_block_count,
                    "reused_block_count": reused_block_count,
                    "summary": "SMR cache active for legacy correction rerender reuse" if cache_hit_count > 0 else "SMR cache built once for the single BAMix premaster render; no correction rerender reuse was needed",
                })
        except Exception:
            pass
        # v63.1.3.2.1: expose estimator authority at the BAMix top level.
        # The estimator was already present inside render_attempts[0], but
        # downstream debug/handoff readers often look only at the compact BAMix
        # root object.  Keep canonical render_attempt telemetry intact and add
        # aliases only; no DSP behavior is changed here.
        _first_render = report.get("render_attempts", [None])[0] if isinstance(report.get("render_attempts"), list) and report.get("render_attempts") else {}
        if isinstance(_first_render, dict):
            report["initial_premix_gain_db"] = _first_render.get("initial_premix_gain_db")
            report["prerender_gain_estimator"] = _first_render.get("prerender_gain_estimator") if isinstance(_first_render.get("prerender_gain_estimator"), dict) else {}
        _last_render = report.get("render_attempts", [None])[-1] if isinstance(report.get("render_attempts"), list) and report.get("render_attempts") else {}
        if isinstance(_last_render, dict):
            report["final_total_global_gain_db"] = _last_render.get("total_global_gain_db")
        # v63.3.9.1: expose density telemetry at the BAMix root as an alias to
        # the last active render attempt.  The canonical per-attempt reports stay
        # inside render_attempts, but root aliases make stage_state/debug_brief
        # checks unambiguous and avoid reading a fade-out/last-block placeholder.
        try:
            _density_reports = []
            for _rr in (report.get("render_attempts") or []):
                if isinstance(_rr, dict) and isinstance(_rr.get("density_drive_report"), dict) and _rr.get("density_drive_report", {}).get("active"):
                    _density_reports.append(_rr.get("density_drive_report"))
            if _density_reports:
                report["density_drive_report"] = _density_reports[-1]
                report["density_drive_reports_by_attempt"] = _density_reports
        except Exception:
            pass
        _est_feedback = _v6313_estimator_feedback(report.get("render_attempts"), report.get("premaster_qc_attempts"), correction)
        report["v6313_prerender_estimator_feedback"] = _est_feedback
        report["v6313_2_prerender_estimator_feedback"] = _est_feedback
        report["v6313_2_module_authority_summary"] = _v6313_2_module_authority_summary(report.get("render_attempts"))
        report["v6313_3_module_authority_summary"] = report["v6313_2_module_authority_summary"]
        report["v6370_body_center_vocal_support"] = _v6370_body_center_vocal_support_summary(report.get("render_attempts"))
        report["body_center_vocal_support"] = report["v6370_body_center_vocal_support"]
        report["v6380_bass_harmonic_translation"] = _v6380_bass_harmonic_translation_summary(report.get("render_attempts"))
        report["bass_harmonic_translation"] = report["v6380_bass_harmonic_translation"]
        report["v6390_side_texture_stereo_cleanliness"] = _v6390_side_texture_stereo_cleanliness_summary(report.get("render_attempts"))
        report["side_texture_stereo_cleanliness"] = report["v6390_side_texture_stereo_cleanliness"]
        report["side_texture_control"] = report["v6390_side_texture_stereo_cleanliness"].get("side_texture_control", {})
        report["side_high_hash_fizz_suppressor"] = report["v6390_side_texture_stereo_cleanliness"].get("side_high_hash_fizz_suppressor", {})
        report["stereo_cleanliness_guard"] = report["v6390_side_texture_stereo_cleanliness"].get("stereo_cleanliness_guard", {})
        report["ambience_collapse_detection"] = report["v6390_side_texture_stereo_cleanliness"].get("ambience_collapse_detection", {})
        report["mono_fold_down_rollback"] = report["v6390_side_texture_stereo_cleanliness"].get("mono_fold_down_rollback", {})
        report["v645_deterministic_quality_patch"] = _v645_deterministic_quality_summary(report.get("render_attempts"))
        report["stem_neural_codec_residue_map"] = report["v645_deterministic_quality_patch"].get("stem_neural_codec_residue_map", {})
        report["erb_ms_dynamic_resonance_suppressor"] = report["v645_deterministic_quality_patch"].get("erb_ms_dynamic_resonance_suppressor", {})
        report["upward_body_density_polynomial"] = report["v645_deterministic_quality_patch"].get("upward_body_density_polynomial", {})
        report["v6464_transient_hf_ownership_lock"] = _v6464_transient_hf_ownership_summary(report.get("render_attempts"))
        report["v6463_adaptive_block_sizing"] = report["v6464_transient_hf_ownership_lock"].get("adaptive_block_sizing", {})
        report["v6464_assist_delta_consolidator"] = report["v6464_transient_hf_ownership_lock"].get("assist_delta_consolidator", {})
        report["v6463_output_seam_smoother"] = report["v6464_transient_hf_ownership_lock"].get("output_seam_smoother", {})
        report["v6313_3_handoff_damage_budget"] = _v6313_3_handoff_damage_budget(qc_final, recipe)
        if qc_final.get("hard_fail") and _env_on("BUSY_BAMIX_V63361_QC_SALVAGE", "1"):
            salvaged_path, salvage = _salvage_premaster_after_hard_qc(premaster, qc_final, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
            report["v63361_qc_salvage"] = salvage
            if salvaged_path is not None:
                qc_salvaged = _premaster_qc(salvaged_path, sr=TARGET_SR, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
                report.setdefault("premaster_qc_attempts", []).append(qc_salvaged)
                report.setdefault("qc_salvage_attempts", []).append({"path": str(salvaged_path), "salvage": salvage, "qc_after": qc_salvaged})
                premaster = salvaged_path
                qc_final = qc_salvaged
                report["premaster_qc"] = qc_final
                log("busy_auto_mixing_qc_salvage_applied", warnings=qc_final.get("warnings"), hard_fail=qc_final.get("hard_fail"), salvage=salvage)
            else:
                log("busy_auto_mixing_qc_salvage_skipped", salvage=salvage, warnings=qc_final.get("warnings"))
        # v63.3.9.1: allow peak-safe handoff makeup even when the current QC
        # still carries an under-driven/crest hard warning.  The makeup helper
        # itself checks LUFS need and true-peak room; if there is no safe room it
        # returns a skipped report.  This prevents the exact v63.3.8 regression
        # pattern: density creates headroom but QC remains hard-fail because LUFS
        # is still under the premaster handoff window.
        if _env_on("BUSY_BAMIX_V6339_PEAK_SAFE_HANDOFF_MAKEUP", "1"):
            makeup_path, makeup_report = _apply_v6339_peak_safe_handoff_makeup(premaster, qc_final, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
            report["v6339_peak_safe_handoff_makeup"] = makeup_report
            if makeup_path is not None:
                qc_makeup = _premaster_qc(makeup_path, sr=TARGET_SR, recipe=recipe, stem_metrics=stem_metrics, original_decision=original_decision if isinstance(original_decision, dict) else {}, reference_db=reference_db)
                report.setdefault("premaster_qc_attempts", []).append(qc_makeup)
                report.setdefault("handoff_makeup_attempts", []).append({"path": str(makeup_path), "makeup": makeup_report, "qc_after": qc_makeup})
                premaster = makeup_path
                qc_final = qc_makeup
                report["premaster_qc"] = qc_final
                log("busy_auto_mixing_v6339_handoff_makeup_applied", makeup=makeup_report, qc=qc_final)
            else:
                log("busy_auto_mixing_v6339_handoff_makeup_skipped", makeup=makeup_report)
        if qc_final.get("hard_fail"):
            report["v63361_final_qc_hard_fail_after_salvage"] = True
            report["v63361_final_qc_downgrade_allowed"] = not _env_on("BUSY_AUTOMIX_FAIL_ON_FINAL_QC_HARD_FAIL", "0")
        if qc_final.get("hard_fail") and _env_on("BUSY_AUTOMIX_FAIL_ON_FINAL_QC_HARD_FAIL", "0"):
            report.update({"available": False, "status": "qc_failed", "reason": "BUSY_MIX_FAILURE_final_qc_hard_fail", "premaster_used_for_mastering": False})
            log("busy_auto_mixing_qc_failed", warnings=qc_final.get("warnings"), qc=qc_final, salvage=report.get("v63361_qc_salvage"))
            return None, report
        if qc_final.get("hard_fail"):
            report["v63361_hard_qc_downgraded_to_mastering_handoff_warning"] = True
            log("busy_auto_mixing_qc_hard_fail_downgraded", warnings=qc_final.get("warnings"), qc=qc_final, salvage=report.get("v63361_qc_salvage"))
        report.update({
            "available": True,
            "status": "premaster_ready",
            "premaster_path": str(premaster),
            "premaster_used_for_mastering": True,
            "candidate_render_count": 1,
            "full_length_candidate_count": 1,
            "full_premaster_render_attempt_count": len(report.get("render_attempts", [])),
            "parallel_candidate_buffer_count": 0,
        })
        log("busy_auto_mixing_done", premaster=str(premaster), recipe=recipe, qc_warnings=qc_final.get("warnings"), correction_loops=report.get("correction_loops_executed"))
        gc.collect()
        return premaster, _jsonable(report)
    except Exception as exc:
        report.update({"available": False, "status": "exception", "reason": "busy_auto_mixing_exception", "error": str(exc)[:800], "premaster_used_for_mastering": False})
        log("busy_auto_mixing_exception", error=str(exc)[:500])
        return None, _jsonable(report)
