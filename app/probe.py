"""ffprobe helpers and file classification."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import Config

log = logging.getLogger(__name__)

# Containers that natively support HEVC — safe to keep the same extension.
# WebM is intentionally excluded: it standardises on VP8/VP9/AV1, not HEVC.
HEVC_SAFE_CONTAINERS = {".mp4", ".mkv", ".mov", ".m4v", ".ts", ".mts", ".m2ts"}


@dataclass
class VideoInfo:
    path: Path
    codec: str
    pix_fmt: str
    width: int
    height: int
    duration: float
    bit_depth: int
    chroma: str
    video_streams: int
    audio_streams: int
    subtitle_streams: int
    attached_pic_streams: int
    color_primaries: str
    color_trc: str
    color_space: str
    color_range: str


class Skip(Exception):
    """Raised when a file must be skipped (with reason)."""


def ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {res.stderr.strip()}")
    return json.loads(res.stdout)


def _parse_pix_fmt(pix_fmt: str) -> tuple[int, str]:
    """Return (bit_depth, chroma_subsampling) parsed from an ffprobe pix_fmt."""
    p = (pix_fmt or "").lower()
    if "444" in p:
        chroma = "444"
    elif "422" in p:
        chroma = "422"
    else:
        chroma = "420"
    if "12le" in p or "12be" in p or "p012" in p:
        depth = 12
    elif "10le" in p or "10be" in p or "p010" in p:
        depth = 10
    elif "16le" in p or "16be" in p:
        depth = 16
    else:
        depth = 8
    return depth, chroma


def probe_video(path: Path) -> VideoInfo:
    data = ffprobe(path)
    streams = data.get("streams", [])
    all_v = [s for s in streams if s.get("codec_type") == "video"]
    attached_pic = [
        s for s in all_v
        if s.get("disposition", {}).get("attached_pic", 0) == 1
    ]
    v_streams = [
        s for s in all_v
        if s.get("disposition", {}).get("attached_pic", 0) == 0
    ]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not v_streams:
        raise Skip("no video stream")

    v = v_streams[0]

    codec = (v.get("codec_name") or "").lower()
    pix_fmt = (v.get("pix_fmt") or "").lower()
    bit_depth, chroma = _parse_pix_fmt(pix_fmt)

    def _clean(tag: str) -> str:
        val = (v.get(tag) or "").lower()
        return "" if val in ("", "unknown", "reserved") else val

    color_primaries = _clean("color_primaries")
    color_trc = _clean("color_transfer")
    color_space = _clean("color_space")
    color_range = _clean("color_range")

    duration = 0.0
    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except ValueError:
            pass
    if duration == 0.0 and v.get("duration"):
        try:
            duration = float(v["duration"])
        except ValueError:
            pass

    return VideoInfo(
        path=path,
        codec=codec,
        pix_fmt=pix_fmt,
        width=int(v.get("width", 0) or 0),
        height=int(v.get("height", 0) or 0),
        duration=duration,
        bit_depth=bit_depth,
        chroma=chroma,
        video_streams=len(v_streams),
        audio_streams=len(a_streams),
        subtitle_streams=len(s_streams),
        attached_pic_streams=len(attached_pic),
        color_primaries=color_primaries,
        color_trc=color_trc,
        color_space=color_space,
        color_range=color_range,
    )


def classify(path: Path, cfg: Config) -> VideoInfo:
    """Probe & decide whether the file should be processed. Raises Skip if not."""
    ext = path.suffix.lower()
    if ext not in cfg.video_extensions:
        raise Skip(f"extension {ext} not in video_extensions")

    if ext in cfg.raw_extensions:
        raise Skip(f"raw container extension {ext}")

    name_lower = path.name.lower()
    for marker in cfg.raw_filename_markers:
        if marker in name_lower:
            raise Skip(f"filename marker '{marker}' suggests raw/log source")

    try:
        size = path.stat().st_size
    except OSError as e:
        raise Skip(f"stat failed: {e}") from e
    if size < cfg.min_size_bytes:
        raise Skip(f"file smaller than min_size_bytes ({size} < {cfg.min_size_bytes})")

    info = probe_video(path)

    if info.codec in cfg.raw_codecs:
        raise Skip(f"raw/log codec: {info.codec}")

    if info.codec in cfg.skip_codecs:
        raise Skip(f"already efficient codec: {info.codec}")

    if info.chroma != "420":
        raise Skip(f"chroma {info.chroma} — skipped to avoid downsample to 4:2:0")

    if info.bit_depth > 10:
        raise Skip(f"{info.bit_depth}-bit source — skipped (no lossless HEVC path here)")

    return info


def has_qsv_device() -> bool:
    return _QSV_AVAILABLE


def has_nvenc() -> bool:
    """True if ffmpeg on PATH advertises the hevc_nvenc encoder."""
    return _NVENC_AVAILABLE


_QSV_AVAILABLE: bool = (
    Path("/dev/dri/renderD128").exists() and shutil.which("ffmpeg") is not None
)


def _detect_nvenc() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return "hevc_nvenc" in res.stdout


_NVENC_AVAILABLE: bool = _detect_nvenc()
