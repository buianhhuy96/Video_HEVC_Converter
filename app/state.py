"""Shared runtime state between the scanner, the transcoder, and the web UI.

Everything here is process-local — the whole app runs in one container.
Locks guard the mutable dicts; a queue.Queue lets the UI push actions
(scan / convert) into the main loop, waking it from its interval sleep.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

# Actions the UI can request. The main loop consumes these instead of just
# sleeping for the scan interval.
_action_q: "queue.Queue[str]" = queue.Queue()

# Signals a running convert cycle to abort as soon as it can. The transcoder
# kills ffmpeg; the outer loop breaks out of the queue.
_stop_convert = threading.Event()

_lock = threading.Lock()

_current: dict[str, Any] = {
    "path": None,
    "stage": "idle",        # idle | scanning | probing | encoding | validating | replacing
    "started_at": None,
    "encoder": None,        # 'QSV full-HW' | 'QSV encode-only' | 'libx265'
    "duration": 0.0,        # seconds — source duration (used for progress bar)
    "progress": {},         # ffmpeg -progress key=value dict
}

_scan: dict[str, Any] = {
    "last_start": None,
    "last_end": None,
    "files_examined": 0,   # total candidates found by the walk
    "files_probed": 0,     # how many of those we've already classified
    "phase": "idle",        # idle | enumerating | probing
    "scanning": False,
}

_pending_lock = threading.Lock()
# Each entry: {path, codec, width, height, duration, size, bit_depth}
_pending: list[dict] = []


# ---------------------------------------------------------------------------
# Actions (scan / convert / shutdown wake)
# ---------------------------------------------------------------------------
def request_scan_now() -> None:
    _action_q.put("scan")


def request_convert_now() -> None:
    _action_q.put("convert")


def request_sweep_now() -> None:
    """Trigger a full sweep (discover + convert) via the action queue."""
    _action_q.put("sweep")


def request_wake() -> None:
    """Wake the main loop without triggering an action (used by shutdown)."""
    _action_q.put("wake")


def wait_for_action(timeout: float) -> str | None:
    """Block until an action arrives or `timeout` seconds pass.

    Returns the action name, or None on timeout.
    """
    try:
        return _action_q.get(timeout=timeout)
    except queue.Empty:
        return None


# ---------------------------------------------------------------------------
# Stop-conversion signal
# ---------------------------------------------------------------------------
def request_stop() -> None:
    _stop_convert.set()


def stop_requested() -> bool:
    return _stop_convert.is_set()


def clear_stop() -> None:
    _stop_convert.clear()


# ---------------------------------------------------------------------------
# Current job
# ---------------------------------------------------------------------------
def set_current(**kwargs) -> None:
    with _lock:
        _current.update(kwargs)


def clear_current() -> None:
    with _lock:
        _current.update(path=None, stage="idle", started_at=None,
                        encoder=None, duration=0.0, progress={})


def get_current() -> dict[str, Any]:
    with _lock:
        return dict(_current)


def set_progress(progress: dict[str, str]) -> None:
    with _lock:
        _current["progress"] = dict(progress)


# ---------------------------------------------------------------------------
# Scan stats
# ---------------------------------------------------------------------------
def scan_started() -> None:
    """Enter the enumeration phase — walking the tree, no total yet."""
    with _lock:
        _scan["last_start"] = time.time()
        _scan["files_examined"] = 0
        _scan["files_probed"] = 0
        _scan["phase"] = "enumerating"
        _scan["scanning"] = True


def scan_probing(total: int) -> None:
    """Enumeration done; now probing `total` candidates one by one."""
    with _lock:
        _scan["files_examined"] = total
        _scan["files_probed"] = 0
        _scan["phase"] = "probing"


def scan_probe_tick() -> None:
    with _lock:
        _scan["files_probed"] += 1


def scan_ended(files_examined: int) -> None:
    with _lock:
        _scan["last_end"] = time.time()
        _scan["files_examined"] = files_examined
        _scan["files_probed"] = files_examined
        _scan["phase"] = "idle"
        _scan["scanning"] = False


def get_scan_stats() -> dict[str, Any]:
    with _lock:
        return dict(_scan)


# ---------------------------------------------------------------------------
# Pending queue (populated by discovery, drained by conversion)
# ---------------------------------------------------------------------------
def set_pending(items: list[dict]) -> None:
    with _pending_lock:
        _pending.clear()
        _pending.extend(items)


def get_pending() -> list[dict]:
    with _pending_lock:
        return [dict(x) for x in _pending]


def pending_count() -> int:
    with _pending_lock:
        return len(_pending)


def clear_pending() -> None:
    with _pending_lock:
        _pending.clear()


def remove_pending(path: str) -> None:
    with _pending_lock:
        _pending[:] = [x for x in _pending if x.get("path") != path]
