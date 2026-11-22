"""SQLite store for events, analysed shots and settings (schema from #1)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path, PurePosixPath

from karsija.analysis import Shot

CACHE_VERSION = 4
SCHEMA_VERSION = 1

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
    state        TEXT NOT NULL DEFAULT 'present' CHECK (state IN ('present', 'moved'))
);
CREATE INDEX IF NOT EXISTS shots_event ON shots(event_id);
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL                  -- JSON
);
"""

_METRICS = ("taken", "sharp", "sharp_global", "clip_hi", "clip_lo", "dhash", "hist", "error")


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
        """Return the cached analysis of a shot if its files have not changed."""
        row = self.db.execute(
            f"SELECT files, {', '.join(_METRICS)} FROM shots WHERE key = ? AND sig = ? AND error = ''",
            (key, sig),
        ).fetchone()
        if row is None:
            return None
        files, taken, sharp, sharp_global, clip_hi, clip_lo, dh, hist, error = row
        return Shot(key=key, files=json.loads(files), taken=taken, sharp=sharp, sharp_global=sharp_global,
                    clip_hi=clip_hi, clip_lo=clip_lo, dhash=dh, hist=json.loads(hist), error=error)

    def put(self, event_id: int, shot: Shot, sig: str) -> None:
        """Store the metrics of an analysed shot. User decisions on an existing row are kept."""
        self.db.execute(
            f"""INSERT INTO shots (event_id, key, files, sig, {', '.join(_METRICS)})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    event_id = excluded.event_id, files = excluded.files, sig = excluded.sig,
                    {', '.join(f'{m} = excluded.{m}' for m in _METRICS)}""",
            (event_id, shot.key, json.dumps(shot.files), sig, shot.taken, shot.sharp, shot.sharp_global,
             shot.clip_hi, shot.clip_lo, shot.dhash, json.dumps(shot.hist), shot.error),
        )

    def save_verdicts(self, shots: list[Shot]) -> None:
        """Store group and automatic verdict of judged shots."""
        self.db.executemany(
            "UPDATE shots SET group_id = ?, auto_verdict = ?, reason = ? WHERE key = ?",
            [(s.group, s.verdict, s.reason, s.key) for s in shots],
        )

    def get_settings(self) -> dict[str, float]:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM settings")}

    def set_settings(self, values: dict[str, float]) -> None:
        self.db.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
