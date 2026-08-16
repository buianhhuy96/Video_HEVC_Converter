"""FFmpeg transcode using Intel QSV (hevc_qsv) with libx265 fallback.

Encoder ladder tried in order:
  1. Full-HW QSV       — HW decode + HW encode (fastest, coolest)
  2. QSV encode-only   — SW decode, HW encode (rescues sources iGPU can't decode)
  3. libx265           — pure CPU fallback

A progress-based watchdog kills stalled ffmpeg jobs.
`atomic_replace` preserves mtime & mode and cleans up on cross-device fallbacks.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from config import Config
from probe import HEVC_SAFE_CONTAINERS, VideoInfo, has_qsv_device
import state

log = logging.getLogger(__name__)

# Containers where "-movflags +faststart" is meaningful (moov atom relocation).
FASTSTART_CONTAINERS = {".mp4", ".mov", ".m4v"}


def _choose_container(src: Path, cfg: Config) -> str:
    ext = src.suffix.lower()
    return ext if ext in HEVC_SAFE_CONTAINERS else cfg.output.fallback_container


def _stream_map_args(cfg: Config) -> list[str]:
    """Map only real video (excluding cover art), plus untouched audio/subs/fonts.

    Using 0:V? instead of 0:v ensures attached_pic thumbnails aren't fed to the
    HEVC encoder. Audio, subtitles, and attachments (e.g. ASS fonts) are copied
    stream-for-stream; data streams are dropped because most containers reject them.
    """
    args = ["-map", "0:V?"]
    if cfg.output.copy_audio:
        args += ["-map", "0:a?"]
    if cfg.output.copy_subs:
        args += ["-map", "0:s?"]
    args += ["-map", "0:t?"]
    return args


def _common_output_args(out_ext: str, cfg: Config) -> list[str]:
    """Codec + container flags for streams other than the main video."""
    args: list[str] = []
    if cfg.output.copy_audio:
        args += ["-c:a", "copy"]
    if cfg.output.copy_subs:
        # Strict bit-for-bit passthrough of subtitle streams.
        args += ["-c:s", "copy"]
    else:
        args += ["-sn"]
    args += ["-map_metadata", "0", "-map_chapters", "0"]
    if out_ext in FASTSTART_CONTAINERS:
        args += ["-movflags", "+faststart"]
    return args


def _build_qsv_cmd(
    src: Path, dst: Path, info: VideoInfo, cfg: Config, *, hw_decode: bool
) -> list[str]:
    enc = cfg.encoder
    ten_bit = enc.allow_10bit and info.bit_depth >= 10
    out_ext = dst.suffix.lower()

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
        "-init_hw_device", "qsv=hw:/dev/dri/renderD128",
        "-filter_hw_device", "hw",
    ]
    if hw_decode:
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]

    cmd += ["-i", str(src)]
    cmd += _stream_map_args(cfg)

    if not hw_decode:
        # SW-decoded frames must be uploaded to the iGPU before hevc_qsv accepts them.
        sw_fmt = "p010le" if ten_bit else "nv12"
        cmd += ["-vf", f"format={sw_fmt},hwupload=extra_hw_frames=64"]

    cmd += [
        "-c:v", "hevc_qsv",
        "-preset", enc.preset,
        "-global_quality", str(enc.global_quality),
    ]
    if not hw_decode:
        # In the HW-decode path the encoder infers pix_fmt from QSV surfaces.
        cmd += ["-pix_fmt", "p010le" if ten_bit else "nv12"]

    if enc.look_ahead:
        cmd += ["-look_ahead", "1", "-look_ahead_depth", str(enc.look_ahead_depth)]

    if enc.max_bitrate_kbps > 0:
        cmd += [
            "-maxrate", f"{enc.max_bitrate_kbps}k",
            "-bufsize", f"{enc.max_bitrate_kbps * 2}k",
        ]

    cmd += _common_output_args(out_ext, cfg)
    cmd += [str(dst)]
    return cmd


def _build_x265_cmd(src: Path, dst: Path, info: VideoInfo, cfg: Config) -> list[str]:
    enc = cfg.encoder
    ten_bit = enc.allow_10bit and info.bit_depth >= 10
    out_ext = dst.suffix.lower()

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
        "-i", str(src),
    ]
    cmd += _stream_map_args(cfg)
    cmd += [
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", str(enc.fallback_crf),
        "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p",
        "-x265-params", "log-level=error",
    ]
    cmd += _common_output_args(out_ext, cfg)
    cmd += [str(dst)]
    return cmd


def transcode(info: VideoInfo, cfg: Config) -> Path:
    """Encode `info.path` into a temp file and return its path."""
    src = info.path
    work_dir = Path(cfg.runtime.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_ext = _choose_container(src, cfg)
    dst = work_dir / f".{src.stem}.converting{out_ext}"

    if dst.exists():
        dst.unlink()

    attempts: list[tuple[str, list[str]]] = []
    if cfg.encoder.codec == "hevc_qsv" and has_qsv_device():
        attempts.append(("QSV full-HW", _build_qsv_cmd(src, dst, info, cfg, hw_decode=True)))
        attempts.append(("QSV encode-only", _build_qsv_cmd(src, dst, info, cfg, hw_decode=False)))
    attempts.append(("libx265", _build_x265_cmd(src, dst, info, cfg)))

    last_err: str | None = None
    for label, cmd in attempts:
        if state.stop_requested():
            raise RuntimeError("stopped by user")
        if dst.exists():
            dst.unlink()
        log.info("%s encode: %s", label, src.name)
        state.set_current(encoder=label)
        rc = _run_ffmpeg(cmd, cfg)
        if rc == 0 and dst.exists() and dst.stat().st_size > 0:
            return dst
        if state.stop_requested():
            raise RuntimeError("stopped by user")
        last_err = f"{label} rc={rc}"
        log.warning("%s failed for %s (%s)", label, src.name, last_err)

    if dst.exists():
        dst.unlink()
    raise RuntimeError(f"all encoders failed for {src}: {last_err}")


def _run_ffmpeg(cmd: list[str], cfg: Config) -> int:
    """Run ffmpeg with a `-progress` pipe watchdog. Returns exit code (-1 if killed)."""
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
    log.debug("cmd: %s", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    last_activity = time.monotonic()
    progress: dict[str, str] = {}
    lock = threading.Lock()

    def _touch() -> None:
        nonlocal last_activity
        with lock:
            last_activity = time.monotonic()

    def _read_progress(stream) -> None:
        for line in stream:
            _touch()
            line = line.rstrip()
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            progress[k] = v
            if k == "progress":
                state.set_progress(progress)

    def _read_stderr(stream) -> None:
        for line in stream:
            _touch()
            line = line.rstrip()
            if line:
                log.debug("ffmpeg | %s", line)

    _spawn(proc.stdout, _read_progress)
    _spawn(proc.stderr, _read_stderr)

    stall = cfg.runtime.stall_timeout_seconds
    heartbeat_ts = time.monotonic()
    killed = False
    while proc.poll() is None:
        time.sleep(1)
        now = time.monotonic()
        if state.stop_requested():
            log.warning("stop requested — killing ffmpeg PID %d", proc.pid)
            proc.kill()
            killed = True
            break
        with lock:
            idle = now - last_activity
        if stall > 0 and idle > stall:
            log.error("ffmpeg stalled >%ds — killing PID %d", stall, proc.pid)
            proc.kill()
            killed = True
            break
        if now - heartbeat_ts > 60 and progress:
            log.info(
                "progress: speed=%s time=%s bitrate=%s",
                progress.get("speed", "?"),
                progress.get("out_time", "?"),
                progress.get("bitrate", "?"),
            )
            heartbeat_ts = now

    rc = proc.wait()
    if killed:
        return -1
    if progress:
        log.info(
            "encode done: speed=%s time=%s size=%s",
            progress.get("speed", "?"),
            progress.get("out_time", "?"),
            progress.get("total_size", "?"),
        )
    return rc


def _spawn(stream, target: Callable) -> threading.Thread:
    t = threading.Thread(target=target, args=(stream,), daemon=True)
    t.start()
    return t


def _move_or_copy(src: Path, dst: Path) -> None:
    """Move src → dst even across filesystems; src is guaranteed gone after."""
    try:
        src.rename(dst)
    except OSError:
        shutil.copy2(src, dst)
        src.unlink()


def atomic_replace(src: Path, new_file: Path, cfg: Config) -> Path:
    """Replace `src` with `new_file`, preserving mtime and permissions.

    Same base name is kept. Extension follows `new_file` (may differ from src
    when we remuxed a legacy container, e.g. .avi → .mkv).
    """
    orig_stat = src.stat()
    final_path = src.with_suffix(new_file.suffix)

    if cfg.runtime.delete_original:
        backup = src.with_suffix(src.suffix + ".converting.bak")
        _move_or_copy(src, backup)
        try:
            shutil.move(str(new_file), str(final_path))
        except Exception:
            if backup.exists() and not src.exists():
                _move_or_copy(backup, src)
            raise
        backup.unlink(missing_ok=True)
    else:
        keep = src.with_name(f"{src.stem}.orig{src.suffix}")
        _move_or_copy(src, keep)
        shutil.move(str(new_file), str(final_path))

    try:
        os.utime(final_path, (orig_stat.st_atime, orig_stat.st_mtime))
        os.chmod(final_path, orig_stat.st_mode)
    except OSError as e:
        log.warning("could not preserve mtime/mode on %s: %s", final_path, e)

    return final_path
