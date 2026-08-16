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
HEVC_SAFE_CONTAINERS = {".mp4", ".mkv", ".mov", ".m4v", ".ts", ".mts", ".m2ts", ".webm"}


@dataclass
class VideoInfo:
    path: Path
    codec: str
    pix_fmt: str
    width: int
    height: int
    duration: float
    bit_depth: int
    video_streams: int
    audio_streams: int
    subtitle_streams: int


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


def probe_video(path: Path) -> VideoInfo:
    data = ffprobe(path)
    streams = data.get("streams", [])
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not v_streams:
        raise Skip("no video stream")

    # Pick the first non-cover-art video stream.
    v = next(
        (s for s in v_streams
         if s.get("disposition", {}).get("attached_pic", 0) == 0),
        v_streams[0],
    )

    codec = (v.get("codec_name") or "").lower()
    pix_fmt = (v.get("pix_fmt") or "").lower()
    bit_depth = 10 if ("10le" in pix_fmt or "10be" in pix_fmt or "p010" in pix_fmt) else 8

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
        video_streams=len(v_streams),
        audio_streams=len(a_streams),
        subtitle_streams=len(s_streams),
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

    return info


def has_qsv_device() -> bool:
    return _QSV_AVAILABLE


_QSV_AVAILABLE: bool = (
    Path("/dev/dri/renderD128").exists() and shutil.which("ffmpeg") is not None
)
