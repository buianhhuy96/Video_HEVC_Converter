"""Validation of transcoded output against the original."""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable

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
    progress_cb: Callable[[dict[str, str]], None] | None = None,
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
        # Full decode: every packet must decode without error. Try QSV first
        # (uses the iGPU's fixed-function decoder — an order of magnitude
        # faster than software), then fall back to software if QSV isn't
        # usable for this file.
        base = ["ffmpeg", "-nostdin", "-v", "error", "-xerror"]
        tail = ["-i", str(new_path), "-progress", "pipe:1", "-f", "null", "-"]
        for accel in (["-hwaccel", "qsv"], []):
            cmd = base + accel + tail
            rc, stderr = _run_full_decode(cmd, progress_cb)
            if rc == 0 and not stderr.strip():
                break
            if accel and rc != 0:
                log.info("full-decode: QSV path failed, retrying in software")
                continue
            raise ValidationError(
                f"full-decode failed: rc={rc} stderr={stderr.strip()[:400]}"
            )


def _run_full_decode(
    cmd: list[str],
    progress_cb: Callable[[dict[str, str]], None] | None,
) -> tuple[int, str]:
    """Run ffmpeg with -progress pipe:1 and push each sample to progress_cb."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    progress: dict[str, str] = {}
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        progress[k] = v
        if k == "progress" and progress_cb is not None:
            try:
                progress_cb(dict(progress))
            except Exception:  # noqa: BLE001
                log.exception("validation progress_cb raised")
    proc.wait()
    t.join(timeout=1)
    return proc.returncode, "".join(stderr_lines)
