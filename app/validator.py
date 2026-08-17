"""Validation of transcoded output against the original."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from config import ValidationCfg
from probe import VideoInfo, probe_video

log = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


def validate(
    original: VideoInfo,
    new_path: Path,
    cfg: ValidationCfg,
    *,
    expect_subtitles: bool = True,
) -> None:
    """Raises ValidationError if the new file is bad."""
    if not new_path.exists() or new_path.stat().st_size == 0:
        raise ValidationError("output file missing or empty")

    try:
        new_info = probe_video(new_path)
    except Exception as e:  # noqa: BLE001
        raise ValidationError(f"ffprobe on output failed: {e}") from e

    if new_info.video_streams < 1:
        raise ValidationError("output has no video stream")

    # Encoder must have actually produced HEVC — silent codec passthrough
    # would otherwise leave the library in its original codec.
    if new_info.codec not in ("hevc", "h265"):
        raise ValidationError(
            f"unexpected output codec {new_info.codec!r} (expected hevc)"
        )

    # Guard against quality regressions we can detect from the probe alone.
    if new_info.bit_depth < original.bit_depth:
        raise ValidationError(
            f"bit-depth downgrade: {original.bit_depth}-bit → {new_info.bit_depth}-bit"
        )
    if (new_info.width, new_info.height) != (original.width, original.height):
        raise ValidationError(
            f"resolution changed: {original.width}x{original.height} → "
            f"{new_info.width}x{new_info.height}"
        )

    if cfg.check_stream_counts:
        if new_info.video_streams != original.video_streams:
            raise ValidationError(
                f"video stream count mismatch "
                f"(orig={original.video_streams}, new={new_info.video_streams})"
            )
        if new_info.audio_streams != original.audio_streams:
            raise ValidationError(
                f"audio stream count mismatch "
                f"(orig={original.audio_streams}, new={new_info.audio_streams})"
            )
        if expect_subtitles and new_info.subtitle_streams != original.subtitle_streams:
            raise ValidationError(
                f"subtitle stream count mismatch "
                f"(orig={original.subtitle_streams}, new={new_info.subtitle_streams})"
            )

    if original.duration > 0:
        diff = abs(new_info.duration - original.duration)
        if diff > cfg.duration_tolerance_seconds:
            raise ValidationError(
                f"duration mismatch: orig={original.duration:.2f}s "
                f"new={new_info.duration:.2f}s (diff {diff:.2f}s)"
            )

    if cfg.full_decode:
        # Decode every frame to /dev/null. Any decode error → non-zero exit.
        cmd = [
            "ffmpeg", "-v", "error",
            "-xerror",
            "-i", str(new_path),
            "-f", "null", "-",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or res.stderr.strip():
            raise ValidationError(
                f"full-decode failed: rc={res.returncode} stderr={res.stderr.strip()[:400]}"
            )
