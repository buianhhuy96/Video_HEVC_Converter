"""Entrypoint — scan directories, transcode non-HEVC videos to HEVC using Intel QSV."""
from __future__ import annotations

import logging
import os
import signal
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
from transcoder import atomic_replace, transcode
from validator import ValidationError, validate

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


def _encode_and_replace(path: Path, info, cfg: Config, store: Store) -> None:
    """Transcode → validate → atomically replace `path`. All errors captured to store."""
    log.info("PLAN  %s  codec=%s %dx%d %.1fs %d-bit",
             path, info.codec, info.width, info.height, info.duration, info.bit_depth)

    if cfg.runtime.dry_run:
        store.record(path, "skipped", reason="dry_run", orig_codec=info.codec)
        return

    if not _is_stable(path, cfg.runtime.stability_check_seconds):
        log.info("SKIP  %s  (file changing on disk — will retry next scan)", path)
        return

    orig_size = path.stat().st_size
    tmp_out: Path | None = None
    state.set_current(path=str(path), stage="encoding",
                      started_at=time.time(), progress={},
                      duration=info.duration)
    try:
        tmp_out = transcode(info, cfg)
        state.set_current(stage="validating")
        validate(info, tmp_out, cfg.validation)

        new_size = tmp_out.stat().st_size
        ratio = new_size / max(orig_size, 1)
        if ratio > cfg.output.max_size_ratio:
            log.info(
                "DISCARD %s: new file %.1f%% of original (> %.0f%% cap) — keeping original",
                path, ratio * 100, cfg.output.max_size_ratio * 100,
            )
            tmp_out.unlink(missing_ok=True)
            store.record(
                path, "skipped",
                reason=f"no size gain ({ratio:.2f})",
                orig_codec=info.codec, orig_size=orig_size, new_size=new_size,
            )
            return

        state.set_current(stage="replacing")
        final = atomic_replace(path, tmp_out, cfg)
        log.info(
            "DONE  %s -> %s  %.1f MiB -> %.1f MiB (%.0f%%)",
            path.name, final.name,
            orig_size / 1024 / 1024, new_size / 1024 / 1024,
            ratio * 100,
        )
        store.record(
            final, "ok",
            orig_codec=info.codec, new_codec="hevc",
            orig_size=orig_size, new_size=new_size,
        )
    except ValidationError as e:
        log.error("VALIDATION-FAIL %s  (%s)", path, e)
        if tmp_out and tmp_out.exists():
            tmp_out.unlink()
        store.record(path, "failed", reason=f"validate: {e}", orig_codec=info.codec)
    except Exception as e:  # noqa: BLE001
        log.error("ENCODE-FAIL %s  (%s)", path, e)
        if tmp_out and tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        store.record(path, "failed", reason=f"encode: {e}", orig_codec=info.codec)
    finally:
        state.clear_current()


def discover(cfg: Config, store: Store) -> int:
    """Walk scan_paths, populate state.pending with files needing conversion.

    Returns the number of files examined (not the pending count).
    """
    log.info("discover starting — paths: %s", cfg.scan_paths)
    state.scan_started()
    state.set_current(stage="scanning")
    pending: list[dict] = []
    n = 0
    try:
        for path in _iter_videos(cfg):
            if _shutdown:
                break
            n += 1
            info = _classify(path, cfg, store)
            if info is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            pending.append({
                "path": str(path),
                "codec": info.codec,
                "width": info.width,
                "height": info.height,
                "duration": info.duration,
                "size": size,
                "bit_depth": info.bit_depth,
            })
    finally:
        state.set_pending(pending)
        state.scan_ended(n)
        state.clear_current()
    log.info("discover complete — %d file(s) examined, %d pending", n, len(pending))
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

    log.info("=== Video HEVC Converter — Ugreen DXP4800 Plus (Intel 8505 QSV) ===")
    if has_qsv_device():
        log.info("Intel /dev/dri/renderD128 present — QSV path available")
    else:
        log.warning("No /dev/dri/renderD128 — will fall back to CPU libx265")

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

    # Kick off with an immediate discovery so users see the pending list
    # populated as soon as the container starts.
    state.request_scan_now()

    while not _shutdown:
        try:
            cfg = load_config()
            interval_secs = max(1.0, cfg.runtime.scan_interval_hours * 3600)
            action = state.wait_for_action(interval_secs) or "scan"
            if _shutdown:
                break
            if action == "scan":
                discover(cfg, store)
                if cfg.runtime.auto_convert and state.pending_count() > 0:
                    log.info("auto_convert enabled — starting conversion")
                    convert_pending(cfg, store)
            elif action == "convert":
                convert_pending(cfg, store)
            # 'wake' actions (shutdown) fall through the loop and re-check _shutdown
        except Exception:  # noqa: BLE001
            log.exception("cycle aborted with unexpected error")

        if _shutdown:
            break
        if cfg.runtime.scan_interval_hours <= 0:
            log.info("scan_interval_hours=0 → one-shot mode, exiting")
            break

    log.info("exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
