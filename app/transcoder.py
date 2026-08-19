"""FFmpeg transcode using Intel QSV (hevc_qsv, full-HW only) or libx265.

When `cfg.encoder.codec == "hevc_qsv"` only the full-HW pipeline is tried.
Files that aren't eligible (software filters requested, QSV device missing)
or fail at runtime raise `NotSupported` and are recorded as *skipped* by
`convert.py` — never silently fall back to a slower software path.

When `cfg.encoder.codec == "libx265"` the pure-CPU encoder is used.

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


class NotSupported(Exception):
    """Encoding pipeline is unavailable for this file — record as skipped."""


# Containers where "-movflags +faststart" is meaningful (moov atom relocation).
FASTSTART_CONTAINERS = {".mp4", ".mov", ".m4v"}


def _choose_container(src: Path, cfg: Config) -> str:
    ext = src.suffix.lower()
    return ext if ext in HEVC_SAFE_CONTAINERS else cfg.output.fallback_container


# Level → unsharp `luma_amount`. Chroma left at 0 to avoid colour fringing.
_SHARPEN_STRENGTHS = {1: 0.3, 2: 0.5, 3: 0.8, 4: 1.2, 5: 1.6}


def _pre_filters(cfg: Config) -> str:
    """Software-domain filter chain applied before the encoder."""
    filters = []
    if cfg.encoder.deband:
        filters.append("gradfun=1.5:8")
    amt = _SHARPEN_STRENGTHS.get(int(cfg.encoder.sharpen or 0))
    if amt is not None:
        filters.append(f"unsharp=5:5:{amt:.2f}:5:5:0.0")
    return ",".join(filters)


def _needs_bt709_bsf(info: VideoInfo) -> bool:
    """True when the source has no colour tagging — retag output as BT.709 SDR."""
    return not (info.color_primaries or info.color_trc or info.color_space)


def _stream_map_args(cfg: Config, info: VideoInfo) -> list[str]:
    """Map main video for encoding + cover art / audio / subs / attachments to copy.

    `0:V?` selects real video streams (excluding attached_pic) — those get
    encoded. Cover art is mapped separately so it can be bit-copied via a
    per-stream codec override (see `_common_output_args`).
    """
    args = ["-map", "0:V?"]
    if info.attached_pic_streams > 0:
        args += ["-map", "0:v:m:attached_pic?"]
    if cfg.output.copy_audio:
        args += ["-map", "0:a?"]
    if cfg.output.copy_subs:
        args += ["-map", "0:s?"]
    args += ["-map", "0:t?"]
    return args


def _common_output_args(out_ext: str, cfg: Config, info: VideoInfo) -> list[str]:
    """Codec + container flags for streams other than the main video."""
    args: list[str] = []
    if info.attached_pic_streams > 0:
        # The main video encoder is set by the per-encoder builder; per-stream
        # override keeps cover art (mapped second) as a bit-copy.
        args += ["-c:v:1", "copy"]
    if cfg.output.copy_audio:
        args += ["-c:a", "copy"]
    if cfg.output.copy_subs:
        # Strict bit-for-bit passthrough of subtitle streams.
        args += ["-c:s", "copy"]
    else:
        args += ["-sn"]
    if cfg.encoder.preserve_color_metadata:
        if info.color_primaries:
            args += ["-color_primaries", info.color_primaries]
        if info.color_trc:
            args += ["-color_trc", info.color_trc]
        if info.color_space:
            args += ["-colorspace", info.color_space]
        if info.color_range:
            args += ["-color_range", info.color_range]
    # For untagged sources, retag the HEVC bitstream directly (post-encode) so
    # players don't guess sRGB gamma. Bitstream filter runs after the encoder,
    # so it doesn't confuse hevc_qsv's parameter validation.
    if _needs_bt709_bsf(info):
        args += ["-bsf:v", "hevc_metadata=colour_primaries=1:"
                            "transfer_characteristics=1:"
                            "matrix_coefficients=1:"
                            "video_full_range_flag=0"]
    args += ["-map_metadata", "0", "-map_chapters", "0"]
    if out_ext in FASTSTART_CONTAINERS:
        args += ["-movflags", "+faststart"]
    return args


def _build_qsv_cmd(
    src: Path, dst: Path, info: VideoInfo, cfg: Config, *, hw_decode: bool
) -> list[str]:
    enc = cfg.encoder
    # Always emit 10-bit main10 when allowed: matches libx265 fallback and
    # avoids encoder-introduced banding on smooth 8-bit sources.
    ten_bit = enc.allow_10bit
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
    cmd += _stream_map_args(cfg, info)

    if hw_decode:
        if ten_bit and info.bit_depth < 10:
            # Convert 8-bit HW-decoded surfaces to 10-bit on the iGPU.
            cmd += ["-filter:v:0", "scale_qsv=format=p010le"]
    else:
        # SW-decoded frames must be uploaded to the iGPU before hevc_qsv accepts them.
        # Scope to output video stream 0 so mapped cover art (stream 1) is untouched.
        sw_fmt = "p010le" if ten_bit else "nv12"
        pre = _pre_filters(cfg)
        chain = f"format={sw_fmt},hwupload=extra_hw_frames=64"
        if pre:
            chain = f"{pre},{chain}"
        cmd += ["-filter:v:0", chain]

    cmd += [
        "-c:v", "hevc_qsv",
        "-preset", enc.preset,
        "-global_quality", str(enc.global_quality),
        # ICQ tuning knobs — scene-cut aware I-frames, smarter B-frame
        # placement, pyramid B-frames, more references per GOP. Small quality
        # bump (~5%) at negligible speed cost on Xe-LP.
        "-adaptive_i", "1",
        "-adaptive_b", "1",
        "-b_strategy", "1",
        "-bf", "4",
        "-refs", "4",
    ]
    if not hw_decode:
        cmd += ["-pix_fmt", "p010le" if ten_bit else "nv12"]

    if enc.look_ahead:
        cmd += ["-look_ahead", "1", "-look_ahead_depth", str(enc.look_ahead_depth)]

    if enc.max_bitrate_kbps > 0:
        cmd += [
            "-maxrate", f"{enc.max_bitrate_kbps}k",
            "-bufsize", f"{enc.max_bitrate_kbps * 2}k",
        ]

    cmd += _common_output_args(out_ext, cfg, info)
    cmd += [str(dst)]
    return cmd


def _build_x265_cmd(src: Path, dst: Path, info: VideoInfo, cfg: Config) -> list[str]:
    enc = cfg.encoder
    # Always emit 10-bit main10 when allowed: matches QSV path.
    ten_bit = enc.allow_10bit
    out_ext = dst.suffix.lower()

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
        "-i", str(src),
    ]
    cmd += _stream_map_args(cfg, info)
    pre = _pre_filters(cfg)
    if pre:
        cmd += ["-filter:v:0", pre]
    cmd += [
        "-c:v", "libx265",
        "-preset", enc.preset,
        # `-tune grain` + aq-mode=3 + no-sao is the classic grain-preserving
        # recipe: keeps film-grain intact and avoids the loop-filter smoothing
        # that eats micro-contrast at higher CRF.
        "-tune", "grain",
        "-crf", str(enc.global_quality),
        "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p",
        "-x265-params", "log-level=error:aq-mode=3:no-sao=1",
    ]
    if enc.max_bitrate_kbps > 0:
        cmd += [
            "-maxrate", f"{enc.max_bitrate_kbps}k",
            "-bufsize", f"{enc.max_bitrate_kbps * 2}k",
        ]
    cmd += _common_output_args(out_ext, cfg, info)
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
    if cfg.encoder.codec == "hevc_qsv":
        if not has_qsv_device():
            raise NotSupported("QSV device (/dev/dri/renderD128) not available")
        # Sharpen and deband run in software — they force encode-only mode,
        # which the user has opted out of. Skip the file instead of degrading.
        if cfg.encoder.deband or cfg.encoder.sharpen:
            raise NotSupported(
                "software filter enabled (deband/sharpen) is incompatible "
                "with QSV full-HW mode"
            )
        attempts.append(("QSV full-HW", _build_qsv_cmd(src, dst, info, cfg, hw_decode=True)))
    elif cfg.encoder.codec == "libx265":
        attempts.append(("libx265", _build_x265_cmd(src, dst, info, cfg)))
    else:
        raise NotSupported(f"unknown encoder codec: {cfg.encoder.codec!r}")

    last_err: str | None = None
    enc_cfg = cfg.encoder
    params = (
        f"codec={enc_cfg.codec} preset={enc_cfg.preset} "
        f"crf/global_quality={enc_cfg.global_quality} "
        f"10bit={enc_cfg.allow_10bit} "
        f"look_ahead={enc_cfg.look_ahead}({enc_cfg.look_ahead_depth}) "
        f"sharpen={enc_cfg.sharpen} deband={enc_cfg.deband}"
    )
    for label, cmd in attempts:
        if state.stop_requested():
            raise RuntimeError("stopped by user")
        if dst.exists():
            dst.unlink()
        log.info("%s encode: %s  [%s]", label, src.name, params)
        state.set_current(encoder=label, enc_params=params)
        rc = _run_ffmpeg(cmd, cfg)
        if rc == 0 and dst.exists() and dst.stat().st_size > 0:
            return dst
        if state.stop_requested():
            raise RuntimeError("stopped by user")
        last_err = f"{label} rc={rc}"
        log.warning("%s failed for %s (%s)", label, src.name, last_err)

    if dst.exists():
        dst.unlink()
    # Only one attempt is ever queued now; treat its failure as "not supported"
    # so the file is recorded as skipped, not retried on the next sweep.
    raise NotSupported(f"encoder failed: {last_err}")


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

    stderr_tail: list[str] = []

    def _read_stderr(stream) -> None:
        for line in stream:
            _touch()
            line = line.rstrip()
            if line:
                log.debug("ffmpeg | %s", line)
                stderr_tail.append(line)
                if len(stderr_tail) > 40:
                    del stderr_tail[0]

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
    if rc != 0 and stderr_tail:
        log.warning("ffmpeg stderr tail (rc=%d):\n  %s", rc, "\n  ".join(stderr_tail[-15:]))
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

    Refuses to clobber an unrelated file that already lives at the target path,
    and stages the encoded output next to the source so the final swap is a
    same-filesystem rename — no partial file can remain at the final path if
    a cross-filesystem copy fails.
    """
    orig_stat = src.stat()
    final_path = src.with_suffix(new_file.suffix)

    if final_path.exists() and final_path != src:
        raise RuntimeError(
            f"refusing to overwrite existing file at target: {final_path}"
        )

    # Copy the encoded file into the source directory first. This turns the
    # subsequent rename into a same-filesystem atomic operation.
    staged = src.with_name(f".{src.stem}.converting.staged{new_file.suffix}")
    if staged.exists():
        staged.unlink()
    try:
        _move_or_copy(new_file, staged)
    except Exception:
        staged.unlink(missing_ok=True)
        raise

    if cfg.runtime.delete_original:
        backup = src.with_suffix(src.suffix + ".converting.bak")
        if backup.exists():
            backup.unlink()
        os.rename(src, backup)
        try:
            os.rename(staged, final_path)
        except OSError:
            try:
                os.rename(backup, src)
            except OSError:
                log.exception(
                    "CRITICAL: failed to restore %s after replacement error", src
                )
            staged.unlink(missing_ok=True)
            raise
        backup.unlink(missing_ok=True)
    else:
        keep = src.with_name(f"{src.stem}.orig{src.suffix}")
        if keep.exists():
            keep.unlink()
        os.rename(src, keep)
        try:
            os.rename(staged, final_path)
        except OSError:
            try:
                os.rename(keep, src)
            except OSError:
                log.exception(
                    "CRITICAL: failed to restore %s after replacement error", src
                )
            staged.unlink(missing_ok=True)
            raise

    try:
        os.utime(final_path, (orig_stat.st_atime, orig_stat.st_mtime))
        os.chmod(final_path, orig_stat.st_mode)
    except OSError as e:
        log.warning("could not preserve mtime/mode on %s: %s", final_path, e)

    return final_path
