"""Entrypoint — scan directories, transcode non-HEVC videos to HEVC using Intel QSV."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import state
import webui
from config import Config, load_config
from probe import Skip, classify, has_qsv_device
from store import Store
import transcoder
from transcoder import NotSupported, atomic_replace, transcode
from validator import ValidationError, precheck_source, validate

log = logging.getLogger("converter")

_shutdown = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _shutdown
        log.warning("Signal %s received — will exit after current file.", signum)
        _shutdown = True
        state.request_wake()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _setup_logging(log_file: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fileh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    fileh.setFormatter(fmt)
    root.addHandler(fileh)


def _iter_videos(cfg: Config):
    exts = cfg.video_extensions
    for root_str in cfg.scan_paths:
        root = Path(root_str)
        if not root.exists():
            log.warning("scan path missing: %s", root)
            continue
        for path in root.rglob("*"):
            if _shutdown:
                return
            if not path.is_file():
                continue
            if path.suffix.lower() not in exts:
                continue
            # Skip our own in-progress artefacts.
            if path.name.startswith(".") and ".converting" in path.name:
                continue
            yield path


def _is_stable(path: Path, wait: float) -> bool:
    """Return False if size/mtime changes over `wait` seconds (file still being written)."""
    if wait <= 0:
        return True
    try:
        s1 = path.stat()
    except OSError:
        return False
    time.sleep(wait)
    try:
        s2 = path.stat()
    except OSError:
        return False
    return s1.st_size == s2.st_size and s1.st_mtime == s2.st_mtime


def _seconds_until(hhmm: str) -> float | None:
    """Seconds from now until the next local HH:MM. Returns None if invalid/empty."""
    if not hhmm:
        return None
    try:
        hour, minute = (int(part) for part in hhmm.split(":", 1))
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    from datetime import datetime, timedelta
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text",
                    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
_SIDECAR_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}


def _find_sidecar_subs(video: Path) -> list[Path]:
    """Sibling files with the video's stem and any subtitle extension.
    Matches names like `Movie.en.srt`, `Movie.forced.en.srt`, etc.
    Returns [] if the parent directory is unreadable or has no matches.
    """
    if not video.parent.exists():
        return []
    stem_lower = video.stem.lower() + "."
    subs: list[Path] = []
    try:
        for sibling in video.parent.iterdir():
            if not sibling.is_file():
                continue
            if sibling.suffix.lower() not in _SIDECAR_SUB_EXTS:
                continue
            if sibling.name.lower().startswith(stem_lower):
                subs.append(sibling)
    except OSError:
        return []
    return sorted(subs)


def _sidecar_language(sidecar: Path, video_stem: str) -> str | None:
    """Guess an ISO language tag from the tail of a sidecar name.
    `Movie.eng.forced.srt` with video stem `Movie` returns `eng`. When the
    first tail token isn't 2- or 3-letter alphabetic, returns None so we
    let the muxer default the language field."""
    lead = video_stem.lower() + "."
    tail = sidecar.name[len(lead):] if sidecar.name.lower().startswith(lead) else ""
    if not tail:
        return None
    first = tail.split(".", 1)[0]
    if 2 <= len(first) <= 3 and first.isalpha():
        return first.lower()
    return None


def _remerge_source_subs(
    source: Path, encoded: Path, cfg: Config,
) -> Path:
    """If `source` has embedded subtitle streams or has sidecar sub files
    next to it (only when cfg.output.merge_external_subs), merge them
    into `encoded` via a stream-copy remux and return the merged path.
    On no subs or failure, return `encoded` unchanged.

    Doing the subtitle mux as a second pass avoids the matroska-muxer
    stall that occurs when subtitle streams are interleaved with a live
    HEVC encode + look-ahead.
    """
    source_subs: list[str] = []
    if cfg.output.copy_subs:
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "s",
                    "-show_entries", "stream=index,codec_name",
                    "-of", "csv=p=0", str(source),
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("sub-remerge: ffprobe failed on %s (%s)", source, e)
            return encoded
        if probe.returncode == 0:
            source_subs = [
                line.strip() for line in probe.stdout.splitlines()
                if line.strip() and line.split(",")[-1].lower() in _TEXT_SUB_CODECS
            ]

    sidecars: list[Path] = []
    if cfg.output.merge_external_subs:
        sidecars = _find_sidecar_subs(source)
        log.info(
            "sub-remerge: %d embedded text-sub stream(s), %d sidecar file(s) found",
            len(source_subs), len(sidecars),
        )
    else:
        log.info(
            "sub-remerge: %d embedded text-sub stream(s) to merge",
            len(source_subs),
        )

    if not source_subs and not sidecars:
        log.info("sub-remerge: nothing to merge — encoded file passes through")
        return encoded

    merged = encoded.with_name(f"{encoded.stem}.submerged{encoded.suffix}")
    if merged.exists():
        merged.unlink()

    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning",
           "-nostdin",
           "-i", str(encoded), "-i", str(source)]
    for sidecar in sidecars:
        cmd += ["-i", str(sidecar)]
    cmd += ["-map", "0"]
    if cfg.output.copy_subs:
        cmd += ["-map", "1:s?"]
    for i, sidecar in enumerate(sidecars, start=2):
        cmd += ["-map", f"{i}:s?"]
    cmd += ["-c", "copy", "-max_interleave_delta", "0"]
    for i, sidecar in enumerate(sidecars):
        lang = _sidecar_language(sidecar, source.stem)
        if lang:
            # First sidecar's subtitle stream lands after all source-mapped
            # subs — number them sequentially past that. We can't index by
            # source count without probing more, so tag against the sidecar's
            # own stream ordinal using the ':m:filename' shortcut.
            cmd += [f"-metadata:s:s:{len(source_subs) + i}", f"language={lang}"]
    cmd += [str(merged)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("sub-remerge: ffmpeg failed (%s)", e)
        merged.unlink(missing_ok=True)
        return encoded
    if proc.returncode != 0 or not merged.exists() or merged.stat().st_size == 0:
        log.warning(
            "sub-remerge: rc=%d %s", proc.returncode,
            proc.stderr.strip()[:200],
        )
        merged.unlink(missing_ok=True)
        return encoded
    encoded.unlink()
    log.info(
        "sub-remerge: %d embedded + %d sidecar(s) merged into %s",
        len(source_subs), len(sidecars), encoded.name,
    )
    if sidecars and cfg.output.delete_external_subs_after_merge:
        for sidecar in sidecars:
            try:
                sidecar.unlink()
                log.info("sub-remerge: removed sidecar %s", sidecar.name)
            except OSError as e:
                log.warning("sub-remerge: could not delete sidecar %s (%s)",
                            sidecar, e)
    return merged


def _classify(path: Path, cfg: Config, store: Store):
    """Probe and decide whether `path` needs conversion.

    Records skip/probe-fail rows in `store`. Returns the VideoInfo when the
    file should be encoded, otherwise None.
    """
    if store.already_done(path):
        log.debug("cached OK: %s", path)
        return None
    try:
        info = classify(path, cfg)
    except Skip as s:
        log.info("SKIP  %s  (%s)", path, s)
        store.record(path, "skipped", reason=str(s))
        return None
    except Exception as e:  # noqa: BLE001
        log.error("PROBE-FAIL %s  (%s)", path, e)
        store.record(path, "failed", reason=f"probe: {e}")
        return None
    return info


def _quick_filter(path: Path, cfg: Config) -> bool:
    """Cheap gate that runs before ffprobe. True = worth probing."""
    ext = path.suffix.lower()
    if ext not in cfg.video_extensions:
        return False
    if ext in cfg.raw_extensions:
        return False
    name_lower = path.name.lower()
    if any(m in name_lower for m in cfg.raw_filename_markers):
        return False
    try:
        if path.stat().st_size < cfg.min_size_bytes:
            return False
    except OSError:
        return False
    return True


def _probe_for_library(path: Path, cfg: Config) -> dict:
    """Probe `path` and return a rich item dict for the all-media list.

    Raises `Skip` for raw/log codecs (they don't belong in the library view
    at all). Files that are valid media but don't need conversion (already
    HEVC, off-spec chroma, high bit-depth) come back with
    `needs_convert=False` and a human-readable `skip_reason`.
    """
    from probe import probe_video  # local import to avoid cycles

    info = probe_video(path)
    if info.codec in cfg.raw_codecs:
        raise Skip(f"raw/log codec: {info.codec}")

    reason: str | None = None
    if info.codec in cfg.skip_codecs:
        reason = f"already {info.codec}"
    elif info.chroma != "420":
        reason = f"chroma {info.chroma} \u2014 skipped to avoid downsample"
    elif info.bit_depth > 10:
        reason = f"{info.bit_depth}-bit source \u2014 no lossless HEVC path"

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    return {
        "path": str(path),
        "codec": info.codec,
        "width": info.width,
        "height": info.height,
        "duration": info.duration,
        "size": size,
        "bit_depth": info.bit_depth,
        "needs_convert": reason is None,
        "skip_reason": reason,
    }


def _encode_and_replace(path: Path, info, cfg: Config, store: Store) -> None:
    """Transcode → validate → atomically replace `path`. All errors captured to store."""
    log.info("PLAN  %s  codec=%s %dx%d %.1fs %d-bit",
             path, info.codec, info.width, info.height, info.duration, info.bit_depth)

    if cfg.runtime.dry_run:
        log.info("DRY-RUN would convert %s (codec=%s)", path, info.codec)
        return

    if not _is_stable(path, cfg.runtime.stability_check_seconds):
        log.info("SKIP  %s  (file changing on disk — will retry next scan)", path)
        return

    try:
        orig_stat = path.stat()
    except OSError as e:
        log.error("ENCODE-FAIL %s  (stat failed: %s)", path, e)
        return
    orig_size = orig_stat.st_size
    orig_mtime = orig_stat.st_mtime
    tmp_out: Path | None = None
    t_start = time.time()
    state.set_current(path=str(path), stage="encoding",
                      started_at=t_start, progress={},
                      duration=info.duration, size=orig_size)
    try:
        # Pre-flight: if the source is already damaged (bad PTS, corrupt
        # frames, unreadable middle), skip it. Encoding a broken source
        # produces a broken output AND destroys the original.
        log.info("STEP  precheck: %s", path.name)
        try:
            precheck_source(path, info)
        except ValidationError as e:
            log.warning("PRECHECK-FAIL %s  (%s)", path, e)
            store.record(path, "failed", reason=f"source damaged: {e}",
                         orig_codec=info.codec,
                         duration_seconds=time.time() - t_start)
            return

        log.info("STEP  transcode start: %s", path.name)
        tmp_out = transcode(info, cfg)
        log.info(
            "STEP  transcode done: %s (%.1f MiB, %.1fs elapsed)",
            path.name,
            tmp_out.stat().st_size / (1024 * 1024),
            time.time() - t_start,
        )

        # Sub-remerge BEFORE validate so validate inspects the final file
        # that will replace the source — full stream layout, real byte
        # content, catches remerge regressions too.
        if cfg.output.copy_subs or cfg.output.merge_external_subs:
            log.info("STEP  sub-remerge start: %s", path.name)
            tmp_out = _remerge_source_subs(path, tmp_out, cfg)
            log.info(
                "STEP  sub-remerge done: %s (%.1f MiB)",
                path.name, tmp_out.stat().st_size / (1024 * 1024),
            )

        state.set_current(stage="validating", progress={})
        log.info("STEP  validate start: %s", tmp_out.name)
        # Subtitle count check is disabled: bitmap subs (PGS/DVBSUB) and
        # any non-text streams get dropped by the text-only remerge, so a
        # strict match against the source count would fail on legit files.
        validate(info, tmp_out, cfg.validation,
                 expect_subtitles=False,
                 progress_cb=state.set_progress)
        log.info("STEP  validate ok: %s", tmp_out.name)

        new_size = tmp_out.stat().st_size

        # Guard against source-file modification during the (potentially long)
        # encode: replacing a file the user just re-downloaded would lose data.
        try:
            current_stat = path.stat()
        except OSError as e:
            log.error("REPLACE-FAIL %s  (source vanished during encode: %s)", path, e)
            tmp_out.unlink(missing_ok=True)
            store.record(path, "failed", reason=f"source vanished: {e}",
                         orig_codec=info.codec,
                         duration_seconds=time.time() - t_start,
                         command=transcoder.last_ffmpeg_cmd)
            return
        if (current_stat.st_size != orig_size
                or abs(current_stat.st_mtime - orig_mtime) > 1.0):
            log.warning("REPLACE-FAIL %s  (source modified during encode)", path)
            tmp_out.unlink(missing_ok=True)
            store.record(path, "failed", reason="source modified during encode",
                         orig_codec=info.codec,
                         duration_seconds=time.time() - t_start,
                         command=transcoder.last_ffmpeg_cmd)
            return

        state.set_current(stage="replacing")
        log.info("STEP  atomic_replace start: %s -> %s", path.name, tmp_out.name)
        final = atomic_replace(path, tmp_out, cfg)
        log.info("STEP  atomic_replace done: %s", final.name)
        log.info(
            "DONE  %s -> %s  %.1f MiB -> %.1f MiB (%.0f%%)",
            path.name, final.name,
            orig_size / 1024 / 1024, new_size / 1024 / 1024,
            new_size / max(orig_size, 1) * 100,
        )
        store.record(
            final, "ok",
            orig_codec=info.codec, new_codec="hevc",
            orig_size=orig_size, new_size=new_size,
            duration_seconds=time.time() - t_start,
        )
    except ValidationError as e:
        log.error("VALIDATION-FAIL %s  (%s)", path, e)
        if tmp_out and tmp_out.exists():
            tmp_out.unlink()
        if state.stop_requested():
            # Validation was interrupted by user stop; keep for retry.
            log.info("VALIDATE-CANCELLED %s", path)
        else:
            store.record(path, "failed", reason=f"validate: {e}",
                         orig_codec=info.codec,
                         duration_seconds=time.time() - t_start,
                         command=transcoder.last_ffmpeg_cmd)
    except NotSupported as e:
        log.info("SKIP  %s  (%s)", path, e)
        if tmp_out and tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        store.record(path, "skipped", reason=f"encoder: {e}",
                     orig_codec=info.codec,
                     duration_seconds=time.time() - t_start)
    except Exception as e:  # noqa: BLE001
        if tmp_out and tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        if state.stop_requested():
            # User pressed Stop; leave the entry in pending and skip the DB
            # failed record so a later Convert click retries cleanly.
            log.info("ENCODE-CANCELLED %s", path)
        else:
            log.error("ENCODE-FAIL %s  (%s)", path, e)
            store.record(path, "failed", reason=f"encode: {e}",
                         orig_codec=info.codec,
                         duration_seconds=time.time() - t_start,
                         command=transcoder.last_ffmpeg_cmd)
    finally:
        state.clear_current()


def discover(cfg: Config, store: Store) -> int:
    """Walk scan_paths; populate state.pending and state.all_media.

    Two-pass so the UI can render a real progress bar: enumerate all
    candidates fast, then probe them one by one (the expensive step).
    Populates two lists: `all_media` (every valid video the walker saw,
    including already-HEVC ones — used by Rename tab) and `pending`
    (the subset that still needs conversion). Returns the number of files
    examined (not the pending count).
    """
    log.info("discover starting — paths: %s", cfg.scan_paths)
    state.scan_started()
    state.set_current(stage="scanning")
    all_media: list[dict] = []
    pending: list[dict] = []
    n = 0
    try:
        candidates: list[Path] = []
        for path in _iter_videos(cfg):
            if _shutdown:
                break
            candidates.append(path)
        state.scan_probing(len(candidates))
        log.info("discover: enumerated %d candidate(s); probing", len(candidates))

        for path in candidates:
            if _shutdown:
                break
            state.scan_probe_tick()
            n += 1
            if not _quick_filter(path, cfg):
                continue
            try:
                item = _probe_for_library(path, cfg)
            except Skip as s:
                # raw/log codec — record so we don't re-probe next scan.
                store.record(path, "skipped", reason=str(s))
                continue
            except Exception as e:  # noqa: BLE001
                log.error("PROBE-FAIL %s  (%s)", path, e)
                store.record(path, "failed", reason=f"probe: {e}")
                continue

            all_media.append(item)

            if item["needs_convert"]:
                # Cached OK/skip from a prior successful pass — don't re-queue,
                # and reflect that in the library so the row's Status matches
                # the fact that it isn't being queued.
                if store.already_done(path):
                    item["needs_convert"] = False
                    item["skip_reason"] = "already processed (use Clear cache to re-probe)"
                    continue
                pending.append({
                    k: item[k] for k in
                    ("path", "codec", "width", "height",
                     "duration", "size", "bit_depth")
                })
            elif item["skip_reason"]:
                # Off-spec but valid media — memoise so future scans skip fast.
                store.record(path, "skipped", reason=item["skip_reason"])
    finally:
        state.set_pending(pending)
        state.set_all_media(all_media)
        state.scan_ended(n)
        state.clear_current()
    log.info(
        "discover complete — %d file(s) examined, %d in library, %d pending",
        n, len(all_media), len(pending),
    )
    return n


def convert_pending(cfg: Config, store: Store) -> int:
    """Drain the pending queue, encoding each file. Returns number processed.

    Aborts as soon as `state.stop_requested()` becomes true — the currently
    running ffmpeg is killed, its temp file is cleaned up, and the remaining
    pending items are left in the queue for a later Convert click.
    """
    state.clear_stop()
    items = state.get_pending()
    if not items:
        log.info("convert requested but pending queue is empty")
        return 0
    log.info("convert starting — %d file(s) queued", len(items))
    processed = 0
    for item in items:
        if _shutdown or state.stop_requested():
            break
        path = Path(item["path"])
        if not path.exists():
            log.warning("queued file no longer exists: %s", path)
            state.remove_pending(str(path))
            continue
        info = _classify(path, cfg, store)
        if info is None:
            state.remove_pending(str(path))
            continue
        _encode_and_replace(path, info, cfg, store)
        # If a stop was requested mid-encode, don't remove from pending —
        # the user probably wants to retry this file later.
        if not state.stop_requested():
            state.remove_pending(str(path))
            processed += 1
    if state.stop_requested():
        log.warning("convert aborted by user — %d processed, %d still queued",
                    processed, state.pending_count())
    else:
        log.info("convert complete — %d file(s) processed", processed)
    state.clear_stop()
    return processed


def main() -> int:
    cfg = load_config()
    _setup_logging(cfg.runtime.log_file)
    _install_signal_handlers()

    log.info("=== Video HEVC Converter ===")
    if has_qsv_device():
        log.info("Intel /dev/dri/renderD128 present — QSV path available")
    else:
        log.warning("No /dev/dri/renderD128 — QSV encodes will be skipped")

    store = Store(cfg.runtime.state_db)

    ui_port = int(os.environ.get("UI_PORT", "8080"))
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    ui_thread = threading.Thread(
        target=webui.serve, args=(config_path, "0.0.0.0", ui_port),
        daemon=True, name="webui",
    )
    ui_thread.start()
    log.info("web UI on http://0.0.0.0:%d (auth: %s)",
             ui_port,
             "password required" if os.environ.get("UI_PASSWORD") else "OPEN — no password")

    # Idle at startup: wait for the scheduled sweep time or a manual UI action.
    # (No automatic kickoff — otherwise a restart during the day would drain the
    # pending queue immediately, which the user does not want.)

    while not _shutdown:
        try:
            cfg = load_config()
            delay = _seconds_until(cfg.runtime.sweep_at_time)

            if delay is not None:
                action = state.wait_for_action(delay) or "sweep"
            else:
                action = state.wait_for_action(24 * 3600) or "wake"

            if _shutdown:
                break

            if action == "sweep":
                log.info("scheduled sweep — discover + convert")
                discover(cfg, store)
                if state.pending_count() > 0 and not _shutdown:
                    convert_pending(cfg, store)
            elif action == "scan":
                discover(cfg, store)
            elif action == "convert":
                convert_pending(cfg, store)
            # 'wake' actions (shutdown) fall through to re-check _shutdown
        except Exception:  # noqa: BLE001
            log.exception("cycle aborted with unexpected error")

        if _shutdown:
            break

    log.info("exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
