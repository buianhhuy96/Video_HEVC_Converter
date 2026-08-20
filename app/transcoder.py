"""FFmpeg transcode using Intel QSV (full-HW) or NVIDIA NVENC.

Encoder is chosen at startup by the `VHC_ENCODER` env var:
  auto  — QSV if the iGPU device is present, else NVENC if available.
  qsv   — force Intel QSV full-HW (NAS default).
  nvenc — force NVIDIA NVENC (Windows PC path).

QSV path stays in the full-HW pipeline; sharpen/denoise use `vpp_qsv`; deband
is unsupported. NVENC path uses software filters (unsharp, hqdn3d, gradfun)
before the encoder — negligible speed cost while NVENC does the encode.

Files that can't be encoded raise `NotSupported` and get recorded as skipped.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Callable

from config import Config
from probe import HEVC_SAFE_CONTAINERS, VideoInfo, has_nvenc, has_qsv_device
import state

log = logging.getLogger(__name__)


class NotSupported(Exception):
    """Encoding pipeline is unavailable for this file — record as skipped."""


# Ladder for the "auto CRF by source size" option. Sorted descending: the
# first threshold whose (min_size_gb) < actual size wins. Sources below the
# smallest threshold fall back to `_CRF_MIN`.
_CRF_LADDER: list[tuple[float, int]] = [
    (8.0, 23),
    (7.0, 22),
    (6.0, 20),
    (4.0, 18),
    (2.0, 16),
]
_CRF_MIN = 15  # applied to sources smaller than the smallest ladder threshold


def _effective_crf(size_bytes: int, base_crf: int) -> int:
    gb = size_bytes / (1024 ** 3)
    for min_gb, crf in _CRF_LADDER:
        if gb > min_gb:
            return crf
    return _CRF_MIN


# Containers where "-movflags +faststart" is meaningful (moov atom relocation).
FASTSTART_CONTAINERS = {".mp4", ".mov", ".m4v"}


def _choose_container(src: Path, cfg: Config) -> str:
    ext = src.suffix.lower()
    return ext if ext in HEVC_SAFE_CONTAINERS else cfg.output.fallback_container


# Level → vpp_qsv `detail` (HW detail enhancer, 0..100).
_VPP_DETAIL_STRENGTHS = {1: 20, 2: 40, 3: 60, 4: 80, 5: 100}
# Level → vpp_qsv `denoise` (HW noise reducer, 0..100). Aggressive values
# also erase film grain, so keep the curve gentler than detail.
_VPP_DENOISE_STRENGTHS = {1: 5, 2: 15, 3: 30, 4: 50, 5: 80}

# Level → software `unsharp` luma_amount for the NVENC path.
_SW_SHARPEN_STRENGTHS = {1: 0.3, 2: 0.5, 3: 0.8, 4: 1.2, 5: 1.6}
# Level → software `hqdn3d` params (luma_spatial, chroma_spatial, luma_temp,
# chroma_temp). Curve mirrors vpp_qsv=denoise for consistent slider feel.
_SW_DENOISE_PARAMS = {
    1: "1:0.5:3:3",
    2: "2:1:4:4",
    3: "3:1.5:6:6",
    4: "4:2:8:8",
    5: "6:3:9:9",
}


def _vpp_qsv_filter(cfg: Config, out_fmt: str | None) -> str:
    """HW-side filter chain (vpp_qsv). Kept in the full-HW pipeline.

    `out_fmt` is p010le / nv12 to convert to, or None to skip format change.
    Returns an empty string when nothing needs to happen.
    """
    parts: list[str] = []
    detail = _VPP_DETAIL_STRENGTHS.get(int(cfg.encoder.sharpen or 0))
    if detail is not None:
        parts.append(f"detail={detail}")
    denoise = _VPP_DENOISE_STRENGTHS.get(int(cfg.encoder.denoise or 0))
    if denoise is not None:
        parts.append(f"denoise={denoise}")
    if out_fmt:
        parts.append(f"format={out_fmt}")
    if not parts:
        return ""
    return "vpp_qsv=" + ":".join(parts)


def _sw_filter_chain(cfg: Config, out_fmt: str) -> str:
    """Software filter chain for the NVENC path: gradfun + hqdn3d + unsharp."""
    parts: list[str] = []
    if cfg.encoder.deband:
        parts.append("gradfun=1.5:8")
    denoise = _SW_DENOISE_PARAMS.get(int(cfg.encoder.denoise or 0))
    if denoise is not None:
        parts.append(f"hqdn3d={denoise}")
    sharpen = _SW_SHARPEN_STRENGTHS.get(int(cfg.encoder.sharpen or 0))
    if sharpen is not None:
        parts.append(f"unsharp=5:5:{sharpen:.2f}:5:5:0.0")
    parts.append(f"format={out_fmt}")
    return ",".join(parts)


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
    # Prevent silent packet drops when the HW pipeline pushes bursts faster
    # than the muxer normally accepts; and keep container timestamps
    # monotonic even if the source has negative or reordered PTS.
    args += [
        "-max_muxing_queue_size", "9999",
        "-avoid_negative_ts", "make_zero",
    ]
    if out_ext in FASTSTART_CONTAINERS:
        args += ["-movflags", "+faststart"]
    return args


def _build_qsv_cmd(
    src: Path, dst: Path, info: VideoInfo, cfg: Config,
) -> list[str]:
    enc = cfg.encoder
    # Always emit 10-bit main10 when allowed: avoids encoder-introduced
    # banding on smooth 8-bit sources.
    ten_bit = enc.allow_10bit
    out_ext = dst.suffix.lower()

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
        "-init_hw_device", "qsv=hw:/dev/dri/renderD128",
        "-filter_hw_device", "hw",
        "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
        "-i", str(src),
        # cfr regenerates output PTS at a constant rate -- immune to broken
        # source timing; passthrough copies source PTS verbatim.
        "-fps_mode", "cfr" if enc.fixed_frame_rate else "passthrough",
    ]
    cmd += _stream_map_args(cfg, info)

    needs_10bit = ten_bit and info.bit_depth < 10
    out_fmt = "p010le" if needs_10bit else None
    vpp = _vpp_qsv_filter(cfg, out_fmt)
    if vpp:
        cmd += ["-filter:v:0", vpp]
    elif needs_10bit:
        cmd += ["-filter:v:0", "scale_qsv=format=p010le"]

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

    if enc.look_ahead and enc.look_ahead_depth > 0:
        cmd += ["-look_ahead", "1", "-look_ahead_depth", str(enc.look_ahead_depth)]

    if enc.max_bitrate_kbps > 0:
        cmd += [
            "-maxrate", f"{enc.max_bitrate_kbps}k",
            "-bufsize", f"{enc.max_bitrate_kbps * 2}k",
        ]

    cmd += _common_output_args(out_ext, cfg, info)
    cmd += [str(dst)]
    return cmd


def _build_nvenc_cmd(
    src: Path, dst: Path, info: VideoInfo, cfg: Config,
) -> list[str]:
    """Software decode + software filters + NVIDIA NVENC encode."""
    enc = cfg.encoder
    ten_bit = enc.allow_10bit
    out_ext = dst.suffix.lower()
    pix_fmt = "p010le" if ten_bit else "yuv420p"

    cmd: list[str] = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning",
        "-i", str(src),
        "-fps_mode", "cfr" if enc.fixed_frame_rate else "passthrough",
    ]
    cmd += _stream_map_args(cfg, info)
    cmd += ["-filter:v:0", _sw_filter_chain(cfg, pix_fmt)]
    cmd += [
        "-c:v", "hevc_nvenc",
        # p1..p7 = fastest..slowest; p7 is NVENC's veryslow-equivalent.
        "-preset", "p7",
        "-tune", "hq",
        "-rc", "vbr",
        "-cq", str(enc.global_quality),
        "-b:v", "0",  # cq mode ignores b:v but ffmpeg needs it set to 0.
        "-pix_fmt", pix_fmt,
        # Better motion prediction and reference reuse (mirrors QSV knobs).
        "-bf", "4",
        "-b_ref_mode", "middle",
        "-refs", "4",
        "-multipass", "fullres",
        "-spatial-aq", "1",
        "-temporal-aq", "1",
        "-aq-strength", "8",
    ]
    if enc.look_ahead and enc.look_ahead_depth > 0:
        cmd += ["-rc-lookahead", str(enc.look_ahead_depth)]
    if enc.max_bitrate_kbps > 0:
        cmd += [
            "-maxrate", f"{enc.max_bitrate_kbps}k",
            "-bufsize", f"{enc.max_bitrate_kbps * 2}k",
        ]

    cmd += _common_output_args(out_ext, cfg, info)
    cmd += [str(dst)]
    return cmd


# Which encoder the process will use. Read once at import so the choice is
# stable for the lifetime of the container / process.
def _select_encoder() -> str:
    forced = os.environ.get("VHC_ENCODER", "auto").lower().strip()
    if forced == "qsv":
        return "qsv"
    if forced == "nvenc":
        return "nvenc"
    if forced not in ("auto", ""):
        log.warning("VHC_ENCODER=%r not recognised; using auto", forced)
    if has_qsv_device():
        return "qsv"
    if has_nvenc():
        return "nvenc"
    return "qsv"  # will fail at transcode time with a clear message


_ENCODER = _select_encoder()


def transcode(info: VideoInfo, cfg: Config) -> Path:
    """Encode `info.path` into a temp file and return its path."""
    src = info.path
    work_dir = Path(cfg.runtime.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_ext = _choose_container(src, cfg)
    dst = work_dir / f".{src.stem}.converting{out_ext}"

    if dst.exists():
        dst.unlink()

    if _ENCODER == "qsv":
        if not has_qsv_device():
            raise NotSupported("QSV device (/dev/dri/renderD128) not available")
        # QSV path stays in full-HW: deband would need software gradfun.
        if cfg.encoder.deband:
            raise NotSupported(
                "Deband requires software filters; incompatible with QSV full-HW"
            )
    elif _ENCODER == "nvenc":
        if not has_nvenc():
            raise NotSupported("hevc_nvenc not available in this ffmpeg build")
    else:
        raise NotSupported(f"unknown encoder backend: {_ENCODER!r}")

    # Dynamic CRF: override the base value with a per-size ladder for this
    # one encode so bigger sources (usually less-efficiently pre-encoded) get
    # compressed harder.
    if cfg.encoder.dynamic_crf:
        try:
            size_bytes = src.stat().st_size
        except OSError:
            size_bytes = 0
        base_crf = cfg.encoder.global_quality
        eff_crf = _effective_crf(size_bytes, base_crf)
        if eff_crf != base_crf:
            log.info(
                "dynamic CRF: source %.1f GB \u2192 CRF %d (base %d)",
                size_bytes / (1024 ** 3), eff_crf, base_crf,
            )
            cfg = dc_replace(
                cfg, encoder=dc_replace(cfg.encoder, global_quality=eff_crf)
            )

    if _ENCODER == "qsv":
        cmd = _build_qsv_cmd(src, dst, info, cfg)
        label = "QSV full-HW"
    else:
        cmd = _build_nvenc_cmd(src, dst, info, cfg)
        label = "NVENC"

    enc_cfg = cfg.encoder
    params = (
        f"preset={enc_cfg.preset} "
        f"crf/global_quality={enc_cfg.global_quality} "
        f"10bit={enc_cfg.allow_10bit} "
        f"look_ahead={enc_cfg.look_ahead}({enc_cfg.look_ahead_depth}) "
        f"sharpen={enc_cfg.sharpen} denoise={enc_cfg.denoise}"
        f" deband={enc_cfg.deband}"
        + (" auto_crf=on" if cfg.encoder.dynamic_crf else "")
    )

    if state.stop_requested():
        raise RuntimeError("stopped by user")
    log.info("%s encode: %s  [%s]", label, src.name, params)
    state.set_current(encoder=label, enc_params=params)
    rc = _run_ffmpeg(cmd, cfg)
    if rc == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    if state.stop_requested():
        raise RuntimeError("stopped by user")
    log.warning("%s failed for %s (rc=%d)", label, src.name, rc)
    if dst.exists():
        dst.unlink()
    raise NotSupported(f"encoder failed: {label} rc={rc}")


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
