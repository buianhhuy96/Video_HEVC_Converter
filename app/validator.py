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
        # rc alone is not enough: ffmpeg's stream demuxer will happily hit
        # EOF on a truncated file with rc=0 even if only 3 minutes of a
        # 2-hour source made it into the container. So we also demand the
        # decoded out_time reach at least (source duration - tolerance).
        base = ["ffmpeg", "-nostdin", "-v", "error", "-xerror"]
        tail = ["-i", str(new_path), "-progress", "pipe:1", "-f", "null", "-"]
        last_progress: dict[str, str] = {}
        for accel in (["-hwaccel", "qsv"], []):
            cmd = base + accel + tail
            rc, stderr, last_progress = _run_full_decode(cmd, progress_cb)
            if rc == 0:
                break
            if accel:
                log.info("full-decode: QSV path failed (rc=%d), retrying in software", rc)
                continue
            raise ValidationError(
                f"full-decode failed: rc={rc} stderr={stderr.strip()[:400]}"
            )
        # Truncation check: how much of the file actually decoded?
        decoded_s = _parse_out_time(last_progress.get("out_time"))
        if decoded_s is not None and original.duration > 0:
            gap = original.duration - decoded_s
            if gap > cfg.duration_tolerance_seconds:
                raise ValidationError(
                    f"decoded output is truncated: source={original.duration:.1f}s "
                    f"but decoded only {decoded_s:.1f}s "
                    f"(missing {gap:.1f}s of content)"
                )


def _parse_out_time(s: str | None) -> float | None:
    """Parse ffmpeg's HH:MM:SS.uuuuuu progress timestamp into seconds."""
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) != 3:
            return None
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        return None


def _run_full_decode(
    cmd: list[str],
    progress_cb: Callable[[dict[str, str]], None] | None,
) -> tuple[int, str, dict[str, str]]:
    """Run ffmpeg with -progress pipe:1 and push each sample to progress_cb.

    Returns (returncode, stderr_text, final_progress_dict).
    """
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
    return proc.returncode, "".join(stderr_lines), progress


def precheck_source(src: Path, info: VideoInfo, sample_seconds: int = 30) -> None:
    """Sample-decode the source to catch damage before we start encoding.

    Encoding a source with broken PTS or corrupted packets produces an
    unplayable output that also destroys the original. We do a fast software
    decode of a window near the middle of the file (where corruption is
    typically most visible) and fail loudly if ffmpeg reports any error.

    Raises ValidationError if the source is not safely encodable.
    """
    if info.duration <= 0:
        # No duration to seek into; probe already worked, so trust it.
        return

    # Middle of file — start/end are usually intact even in damaged rips.
    start = max(0.0, info.duration / 2 - sample_seconds / 2)
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-xerror",
        "-ss", f"{start:.2f}",
        "-i", str(src),
        "-t", str(sample_seconds),
        "-map", "0:v:0",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(60, sample_seconds * 4),
        )
    except subprocess.TimeoutExpired as e:
        raise ValidationError(
            f"source precheck timed out (ffmpeg hung reading {src.name})"
        ) from e

    if proc.returncode != 0 or proc.stderr.strip():
        msg = proc.stderr.strip().splitlines()
        first_error = msg[0] if msg else f"rc={proc.returncode}"
        raise ValidationError(f"source appears damaged: {first_error[:300]}")

