"""SQLite store for events, analysed shots and settings.

The store doubles as the analysis cache: a shot is re-analysed only when its
``signature`` (path + size + mtime of every file) changes. The user-decision
columns of ``shots`` (``user_verdict``, ``rating``, ``decided_at``) are written
by the review server (``karsija serve``), every change also goes into
``decisions`` so it can be undone. ``jobs`` holds the progress of background
analyses. The ``scan`` command writes neither. ``settings`` overrides
``analysis.DEFAULT_SETTINGS`` for the server; nothing writes it yet.

``shots.state``: ``present`` (in its event folder), ``moved`` (moved into the
reject folder by ``scan --move``) or ``missing`` (gone from the folder when the
server last analysed the event; it keeps its decisions in case it comes back).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import fields
from pathlib import Path, PurePosixPath
from typing import Any

from karsija.analysis import DEFAULT_SETTINGS, Shot
from karsija.library import EVENT_DIR_RE, read_exif

CACHE_VERSION = 4
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    rel_path     TEXT NOT NULL UNIQUE,
    year         INTEGER NOT NULL,
    name         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES events(id),
    key          TEXT NOT NULL UNIQUE,
    files        TEXT NOT NULL,                 -- JSON list of paths
    sig          TEXT NOT NULL,
    taken        REAL NOT NULL DEFAULT 0,
    sharp        REAL NOT NULL DEFAULT 0,
    sharp_global REAL NOT NULL DEFAULT 0,
    clip_hi      REAL NOT NULL DEFAULT 0,
    clip_lo      REAL NOT NULL DEFAULT 0,
    dhash        TEXT NOT NULL DEFAULT '',
    hist         TEXT NOT NULL DEFAULT '[]',    -- JSON list
    exif         TEXT,                          -- JSON object
    error        TEXT NOT NULL DEFAULT '',
    group_id     INTEGER,
    auto_verdict TEXT,
    reason       TEXT,
    user_verdict TEXT CHECK (user_verdict IN ('keep', 'reject')),
    rating       INTEGER CHECK (rating BETWEEN 0 AND 5),
    decided_at   REAL,
    state        TEXT NOT NULL DEFAULT 'present' CHECK (state IN ('present', 'moved', 'missing'))
);
CREATE INDEX IF NOT EXISTS shots_event ON shots(event_id);
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL                  -- JSON
);
CREATE TABLE IF NOT EXISTS decisions (         -- undo stack of user decisions
    id                INTEGER PRIMARY KEY,
    shot_id           INTEGER NOT NULL REFERENCES shots(id),
    prev_user_verdict TEXT,
    prev_rating       INTEGER,
    prev_decided_at   REAL,
    new_user_verdict  TEXT,
    new_rating        INTEGER,
    batch             INTEGER NOT NULL,         -- the writes of one action (e.g. a group accept)
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_batch ON decisions(batch);
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,                 -- 'analyse'
    scope        TEXT NOT NULL,                 -- 'all' or an event id
    total        INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error        TEXT NOT NULL DEFAULT '',
    started      REAL,
    finished     REAL
);
"""

_METRICS = ("taken", "sharp", "sharp_global", "clip_hi", "clip_lo", "dhash", "hist", "error")
_JSON = {"hist", "exif"}
UNSET: Any = object()          # decide(): leave the field as it is


def default_db_path(root: Path) -> Path:
    """State database of ``karsija serve``: outside the library, one file per library root.

    ``$XDG_DATA_HOME/karsija/<first 16 hex of sha256(resolved root)>.sqlite``, by default under ``~/.local/share``.
    """
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return base / "karsija" / f"{digest}.sqlite"


class Store:
    def __init__(self, path: Path | str):
        # the server opens one connection per request or job thread; a connection is never shared concurrently
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise RuntimeError(f"{path}: tietokannan versio {version} on uudempi kuin tämä karsija ({SCHEMA_VERSION})")
        if version == 1:
            self._upgrade_from_1()
        self.db.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _upgrade_from_1(self) -> None:
        """Schema 2 allows ``state = 'missing'``. SQLite cannot change a CHECK constraint, so the table is rebuilt."""
        start = SCHEMA.index("CREATE TABLE IF NOT EXISTS shots")
        create = SCHEMA[start:SCHEMA.index("CREATE INDEX IF NOT EXISTS shots_event")]
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys = OFF")
        self.db.executescript(f"""
            BEGIN;
            ALTER TABLE shots RENAME TO shots_v1;
            {create}
            INSERT INTO shots SELECT * FROM shots_v1;
            DROP TABLE shots_v1;
            COMMIT;""")
        self.db.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def signature(files: list[str]) -> str:
        """Cache key: path + size + mtime of every file of the shot."""
        parts = []
        for f in files:
            st = os.stat(f)
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        return f"v{CACHE_VERSION}|" + "|".join(sorted(parts))

    def event_id(self, rel_path: str) -> int:
        """Return the id of an event folder (``YYYY/YYYY-MM-DD...`` relative to the root), creating it if needed."""
        p = PurePosixPath(rel_path)
        self.db.execute(
            "INSERT INTO events (rel_path, year, name) VALUES (?, ?, ?) ON CONFLICT (rel_path) DO NOTHING",
            (rel_path, int(p.parts[0]), p.name),
        )
        row = self.db.execute("SELECT id FROM events WHERE rel_path = ?", (rel_path,)).fetchone()
        return int(row[0])

    def get(self, key: str, sig: str) -> Shot | None:
        """Return the cached analysis of a shot if its files have not changed.

        Rows stored before EXIF metadata was kept get it read from the file here (header only, no analysis).
        """
        row = self.db.execute(
            f"SELECT {', '.join(_METRICS)}, exif, files FROM shots WHERE key = ? AND sig = ? AND error = ''",
            (key, sig),
        ).fetchone()
        if row is None:
            return None
        shot = self._shot(key, row)
        if row[-2] is None:
            shot.exif = read_exif(key, shot.files)
            self.db.execute("UPDATE shots SET exif = ? WHERE key = ?", (json.dumps(shot.exif), key))
        return shot

    @staticmethod
    def _shot(key: str, row: tuple[Any, ...]) -> Shot:
        """Shot from a row of ``_METRICS + (exif, files)``."""
        values = dict(zip((*_METRICS, "exif", "files"), row, strict=True))
        for k in _JSON | {"files"}:
            values[k] = json.loads(values[k]) if values[k] is not None else {}
        return Shot(key=key, **values)

    def put(self, event_id: int, shot: Shot, sig: str) -> None:
        """Store the metrics of an analysed shot. User decisions on an existing row are kept."""
        cols = (*_METRICS, "exif")
        self.db.execute(
            f"""INSERT INTO shots (event_id, key, files, sig, {', '.join(cols)})
                VALUES (?, ?, ?, ?, {', '.join('?' * len(cols))})
                ON CONFLICT (key) DO UPDATE SET
                    event_id = excluded.event_id, files = excluded.files, sig = excluded.sig, state = 'present',
                    {', '.join(f'{m} = excluded.{m}' for m in cols)}""",
            (event_id, shot.key, json.dumps(shot.files), sig,
             *(json.dumps(getattr(shot, c)) if c in _JSON else getattr(shot, c) for c in cols)),
        )

    def event_shots(self, event_id: int, keys: set[str] | None = None) -> list[Shot]:
        """Analysed shots of an event that are still in place (not moved), optionally only the given keys."""
        rows = self.db.execute(
            f"SELECT key, {', '.join(_METRICS)}, exif, files FROM shots WHERE event_id = ? AND state = 'present'",
            (event_id,),
        ).fetchall()
        return [self._shot(r[0], r[1:]) for r in rows if keys is None or r[0] in keys]

    def mark_missing(self, event_id: int, on_disk: set[str]) -> None:
        """After scanning an event folder: shots not found are ``missing`` and lose their group and suggestion,
        missing shots that are back are ``present`` again. Moved shots are left alone."""
        rows = self.db.execute(
            "SELECT key, state FROM shots WHERE event_id = ? AND state IN ('present', 'missing')", (event_id,)
        ).fetchall()
        self.db.executemany(
            "UPDATE shots SET state = 'missing', group_id = NULL, auto_verdict = NULL, reason = NULL WHERE key = ?",
            [(k,) for k, st in rows if st == "present" and k not in on_disk],
        )
        self.db.executemany(
            "UPDATE shots SET state = 'present' WHERE key = ?",
            [(k,) for k, st in rows if st == "missing" and k in on_disk],
        )

    def save_verdicts(self, shots: list[Shot]) -> None:
        """Store group and automatic verdict of judged shots. Shots in no group (errors) get ``group_id`` NULL."""
        self.db.executemany(
            "UPDATE shots SET group_id = ?, auto_verdict = ?, reason = ? WHERE key = ?",
            [(s.group if s.group >= 0 else None, s.verdict, s.reason, s.key) for s in shots],
        )

    def mark_moved(self, keys: list[str]) -> None:
        """Record that the files of these shots were moved into the reject folder."""
        self.db.executemany("UPDATE shots SET state = 'moved' WHERE key = ?", [(k,) for k in keys])

    def import_legacy_cache(self, path: Path, root: Path) -> int:
        """Copy analyses from the old ``cache.sqlite`` of karsija.py. Returns the number of shots imported.

        Its signatures use the same format, so the imported rows are cache hits as long as the files are unchanged.
        Rows outside ``root`` or outside a ``YYYY/YYYY-MM-DD...`` event folder are skipped.
        """
        names = {f.name for f in fields(Shot)}
        legacy = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = legacy.execute("SELECT key, sig, data FROM shots").fetchall()
        finally:
            legacy.close()
        imported = 0
        for key, sig, data in rows:
            try:
                rel = Path(key).parent.relative_to(root)
            except ValueError:
                continue
            if len(rel.parts) != 2 or not re.fullmatch(r"\d{4}", rel.parts[0]) or not EVENT_DIR_RE.match(rel.parts[1]):
                continue
            shot = Shot(**{k: v for k, v in json.loads(data).items() if k in names})
            if self.db.execute("SELECT 1 FROM shots WHERE key = ?", (key,)).fetchone():
                continue
            self.put(self.event_id(rel.as_posix()), shot, sig)
            imported += 1
        return imported

    def get_settings(self) -> dict[str, float]:
        """Stored settings only; see ``settings()`` for the values in effect."""
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM settings")}

    def settings(self) -> dict[str, float]:
        """Grouping and judging thresholds in effect: the defaults overridden by the stored values."""
        stored = self.get_settings()
        return {k: stored.get(k, v) for k, v in DEFAULT_SETTINGS.items()}

    def set_settings(self, values: dict[str, float]) -> None:
        self.db.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )

    # ----------------------------------------------------------------------- #
    # Decisions
    # ----------------------------------------------------------------------- #
    def decide(self, changes: list[tuple[int, Any, Any]]) -> int:
        """Record user decisions ``(shot_id, user_verdict, rating)`` as one undoable batch; ``UNSET`` keeps a field.

        Returns the batch number.
        """
        self._begin_write()
        now = time.time()
        batch = int(self.db.execute("SELECT COALESCE(MAX(batch), 0) + 1 FROM decisions").fetchone()[0])
        for shot_id, verdict, rating in changes:
            prev_verdict, prev_rating, prev_at = self.db.execute(
                "SELECT user_verdict, rating, decided_at FROM shots WHERE id = ?", (shot_id,)
            ).fetchone()
            new_verdict = prev_verdict if verdict is UNSET else verdict
            new_rating = prev_rating if rating is UNSET else rating
            self.db.execute(
                "UPDATE shots SET user_verdict = ?, rating = ?, decided_at = ? WHERE id = ?",
                (new_verdict, new_rating, now, shot_id),
            )
            self.db.execute(
                """INSERT INTO decisions (shot_id, prev_user_verdict, prev_rating, prev_decided_at,
                                          new_user_verdict, new_rating, batch, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (shot_id, prev_verdict, prev_rating, prev_at, new_verdict, new_rating, batch, now),
            )
        return batch

    def _begin_write(self) -> None:
        """Take the write lock now, so concurrent requests cannot read the same last batch."""
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")

    def undo(self) -> tuple[int | None, list[int]]:
        """Undo the last decision batch. Returns the batch number and the ids of the shots restored."""
        self._begin_write()
        row = self.db.execute("SELECT MAX(batch) FROM decisions").fetchone()
        if row[0] is None:
            return None, []
        batch = int(row[0])
        rows = self.db.execute(
            "SELECT id, shot_id, prev_user_verdict, prev_rating, prev_decided_at FROM decisions "
            "WHERE batch = ? ORDER BY id DESC",
            (batch,),
        ).fetchall()
        for _id, shot_id, verdict, rating, decided_at in rows:   # newest first, so the oldest state wins
            self.db.execute(
                "UPDATE shots SET user_verdict = ?, rating = ?, decided_at = ? WHERE id = ?",
                (verdict, rating, decided_at, shot_id),
            )
        self.db.execute("DELETE FROM decisions WHERE batch = ?", (batch,))
        return batch, sorted({r[1] for r in rows})

    # ----------------------------------------------------------------------- #
    # Jobs
    # ----------------------------------------------------------------------- #
    def create_job(self, kind: str, scope: str) -> int:
        cur = self.db.execute("INSERT INTO jobs (kind, scope) VALUES (?, ?)", (kind, scope))
        return int(cur.lastrowid or 0)

    def update_job(self, job_id: int, **values: Any) -> None:
        cols = [c for c in values if c in ("total", "done", "status", "error", "started", "finished")]
        self.db.execute(
            f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
            (*(values[c] for c in cols), job_id),
        )

    def job(self, job_id: int) -> dict[str, Any] | None:
        cur = self.db.execute(
            "SELECT id, kind, scope, total, done, status, error, started, finished FROM jobs WHERE id = ?", (job_id,)
        )
        row = cur.fetchone()
        return dict(zip((d[0] for d in cur.description), row, strict=True)) if row else None

    def fail_unfinished_jobs(self) -> None:
        """Mark jobs of a previous server run that never finished."""
        self.db.execute(
            "UPDATE jobs SET status = 'failed', error = 'keskeytyi', finished = ? "
            "WHERE status IN ('queued', 'running')",
            (time.time(),),
        )

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
