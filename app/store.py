"""SQLite state store for processed files."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    status      TEXT    NOT NULL,   -- 'ok' | 'skipped' | 'failed'
    reason      TEXT,
    orig_codec  TEXT,
    new_codec   TEXT,
    orig_size   INTEGER,
    new_size    INTEGER,
    ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON processed(status);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def already_done(self, path: Path) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        with self._conn() as c:
            row = c.execute(
                "SELECT size, mtime, status FROM processed WHERE path=?",
                (str(path),),
            ).fetchone()
        if not row:
            return False
        size, mtime, status = row
        # Reprocess if file changed on disk.
        if size != st.st_size or abs(mtime - st.st_mtime) > 1.0:
            return False
        # Failed rows stay "done" so the same broken file isn't retried
        # every scan. Users can retry via the "Retry failed" UI button.
        return status in ("ok", "skipped", "failed")

    def record(self, path: Path, status: str, **fields) -> None:
        try:
            st = path.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        with self._conn() as c:
            c.execute(
                """INSERT INTO processed
                   (path, size, mtime, status, reason, orig_codec, new_codec,
                    orig_size, new_size, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     status=excluded.status, reason=excluded.reason,
                     orig_codec=excluded.orig_codec, new_codec=excluded.new_codec,
                     orig_size=excluded.orig_size, new_size=excluded.new_size,
                     ts=excluded.ts""",
                (
                    str(path), size, mtime, status,
                    fields.get("reason"),
                    fields.get("orig_codec"),
                    fields.get("new_codec"),
                    fields.get("orig_size"),
                    fields.get("new_size"),
                    time.time(),
                ),
            )
