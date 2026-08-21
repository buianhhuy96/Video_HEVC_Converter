"""Shared runtime state between the scanner, the transcoder, and the web UI.

Everything here is process-local — the whole app runs in one container.
Locks guard the mutable dicts; a queue.Queue lets the UI push actions
(scan / convert) into the main loop, waking it from its interval sleep.
"""
from __future__ import annotations

import queue
import threading
import time
from os.path import sep as _sep
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
    "encoder": None,        # 'QSV full-HW'
    "enc_params": None,     # compact human summary of the active encoder settings
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

_all_media_lock = threading.Lock()
# Superset of _pending: every video file the scanner found, including
# HEVC / raw / off-spec ones. Each entry adds `needs_convert: bool` and
# `skip_reason: str | None` on top of the pending fields. Used by the
# Rename tab and library-overview widgets.
_all_media: list[dict] = []

_rename_lock = threading.Lock()
# Rename tab: nested tree of folder/file nodes generated from _all_media.
# Empty dict means "no batch". Mutated per-row as the user edits proposed
# names in the UI; consumed by "Apply".
_rename_tree: dict = {}


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


# Windows wait primitives cap at ~49 days worth of milliseconds. Anything
# larger throws OverflowError. Cap here so the caller never has to worry.
_MAX_WAIT_SECONDS = 24 * 3600  # 1 day — loop back if the timeout runs out.


def wait_for_action(timeout: float) -> str | None:
    """Block until an action arrives or `timeout` seconds pass.

    Returns the action name, or None on timeout.
    """
    try:
        return _action_q.get(timeout=min(timeout, _MAX_WAIT_SECONDS))
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
                        encoder=None, enc_params=None, duration=0.0, progress={})


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


def append_pending(item: dict) -> None:
    """Add a single item to the pending queue if the path isn't already there."""
    p = item.get("path")
    with _pending_lock:
        if any(x.get("path") == p for x in _pending):
            return
        _pending.append(item)


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


def remap_paths(folder_map: list[tuple[str, str]],
                file_map: list[tuple[str, str]],
                *, folders_first: bool = True) -> None:
    """Rewrite in-memory pending/all_media paths after on-disk renames.

    Folder renames apply as a path prefix; file renames as an exact match.
    Must be called with the same src/dst mappings that were just committed
    to disk. For an Apply pass (folder → new-folder → file), use
    folders_first=True; for Undo, pass reversed mappings and False.
    """
    folders = sorted(folder_map, key=lambda pair: -len(pair[0]))

    def _apply_folder(path: str) -> str:
        for src, dst in folders:
            prefix = src.rstrip(_sep) + _sep
            if path.startswith(prefix):
                return dst.rstrip(_sep) + _sep + path[len(prefix):]
            if path == src:
                return dst
        return path

    def _apply_file(path: str) -> str:
        for src, dst in file_map:
            if path == src:
                return dst
        return path

    def _remap(path: str) -> str:
        if folders_first:
            return _apply_file(_apply_folder(path))
        return _apply_folder(_apply_file(path))

    with _pending_lock:
        for entry in _pending:
            entry["path"] = _remap(entry.get("path", ""))
    with _all_media_lock:
        for entry in _all_media:
            entry["path"] = _remap(entry.get("path", ""))


# ---------------------------------------------------------------------------
# All media list (populated by discovery, consumed by Rename / library widgets)
# ---------------------------------------------------------------------------
def set_all_media(items: list[dict]) -> None:
    with _all_media_lock:
        _all_media.clear()
        _all_media.extend(items)


def get_all_media() -> list[dict]:
    with _all_media_lock:
        return [dict(x) for x in _all_media]


def all_media_count() -> int:
    with _all_media_lock:
        return len(_all_media)


def clear_all_media() -> None:
    with _all_media_lock:
        _all_media.clear()


# ---------------------------------------------------------------------------
# Rename tab preview tree
# ---------------------------------------------------------------------------
def set_rename_tree(tree: dict) -> None:
    with _rename_lock:
        _rename_tree.clear()
        _rename_tree.update(tree)


def get_rename_tree() -> dict:
    with _rename_lock:
        # Shallow copy — callers who mutate `children` will still affect
        # the stored tree, so callers that need isolation should deep-copy.
        return dict(_rename_tree)


def clear_rename_tree() -> None:
    with _rename_lock:
        _rename_tree.clear()


def update_rename_node(node_id: str, **fields) -> bool:
    """Merge `fields` into the tree node with the given id. Returns True on hit."""
    from rename import find_node  # local import to avoid cycle at module load
    with _rename_lock:
        if not _rename_tree:
            return False
        node = find_node(_rename_tree, node_id)
        if node is None:
            return False
        node.update(fields)
        return True
