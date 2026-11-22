from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from karsija.analysis import Shot
from karsija.store import SCHEMA_VERSION, Store


def make_shot(path: Path) -> Shot:
    path.write_bytes(b"jpeg")
    return Shot(key=str(path), files=[str(path)], taken=1.5, sharp=42.0, sharp_global=7.0,
                clip_hi=0.01, clip_lo=0.02, dhash="ab" * 32, hist=[0.5, 0.25])


def test_cache_hit_and_miss_on_mtime_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    shot = make_shot(tmp_path / "a.jpg")
    sig = Store.signature(shot.files)
    event = store.event_id("2025/2025-06-01-test")
    store.put(event, shot, sig)
    store.commit()

    hit = store.get(shot.key, Store.signature(shot.files))
    assert hit == shot

    st = os.stat(shot.key)
    os.utime(shot.key, (st.st_atime, st.st_mtime + 10))
    new_sig = Store.signature(shot.files)
    assert new_sig != sig
    assert store.get(shot.key, new_sig) is None


def test_signature_covers_every_file(tmp_path: Path) -> None:
    raw, jpg = tmp_path / "a.nef", tmp_path / "a.jpg"
    raw.write_bytes(b"raw")
    jpg.write_bytes(b"jpeg")
    sig = Store.signature([str(raw), str(jpg)])
    assert f"{raw}:3:" in sig and f"{jpg}:4:" in sig
    jpg.write_bytes(b"longer jpeg")
    assert Store.signature([str(raw), str(jpg)]) != sig


def test_put_keeps_user_decisions(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    shot = make_shot(tmp_path / "a.jpg")
    event = store.event_id("2025/2025-06-01-test")
    store.put(event, shot, "sig1")
    store.db.execute("UPDATE shots SET user_verdict = 'reject', rating = 2 WHERE key = ?", (shot.key,))

    shot.sharp = 50.0
    store.put(event, shot, "sig2")
    row = store.db.execute("SELECT sharp, sig, user_verdict, rating FROM shots WHERE key = ?", (shot.key,)).fetchone()
    assert row == (50.0, "sig2", "reject", 2)


def test_save_verdicts(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    shot = make_shot(tmp_path / "a.jpg")
    store.put(store.event_id("2025/2025-06-01-test"), shot, "sig")
    shot.group, shot.verdict, shot.reason = 3, "best", "2 samankaltaista"
    store.save_verdicts([shot])
    row = store.db.execute("SELECT group_id, auto_verdict, reason FROM shots").fetchone()
    assert row == (3, "best", "2 samankaltaista")


def test_events(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    a = store.event_id("2025/2025-06-01-test")
    assert store.event_id("2025/2025-06-01-test") == a
    b = store.event_id("2024/2024-12-24")
    assert b != a
    rows = store.db.execute("SELECT rel_path, year, name FROM events ORDER BY id").fetchall()
    assert rows == [("2025/2025-06-01-test", 2025, "2025-06-01-test"), ("2024/2024-12-24", 2024, "2024-12-24")]


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "karsija.sqlite"
    store = Store(path)
    assert store.get_settings() == {}
    store.set_settings({"abs_blur": 10.0, "hamming": 40})
    store.set_settings({"abs_blur": 12.5})
    store.commit()
    store.close()
    assert Store(path).get_settings() == {"abs_blur": 12.5, "hamming": 40}


def test_mark_moved_and_back(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    shot = make_shot(tmp_path / "a.jpg")
    event = store.event_id("2025/2025-06-01-test")
    store.put(event, shot, "sig")
    store.mark_moved([shot.key])
    assert store.db.execute("SELECT state FROM shots").fetchone() == ("moved",)
    store.put(event, shot, "sig2")         # moved back and analysed again
    assert store.db.execute("SELECT state FROM shots").fetchone() == ("present",)


def test_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "karsija.sqlite"
    Store(path).close()
    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    store.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    store.close()
    with pytest.raises(RuntimeError, match="uudempi"):
        Store(path)


def test_import_legacy_cache(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    event = root / "2025" / "2025-06-01-test"
    event.mkdir(parents=True)
    inside = make_shot(event / "a.jpg")
    outside = make_shot(tmp_path / "b.jpg")
    not_event = make_shot(root / "c.jpg")
    legacy = sqlite3.connect(tmp_path / "cache.sqlite")
    legacy.execute("CREATE TABLE shots (key TEXT PRIMARY KEY, sig TEXT, data TEXT)")
    for s in (inside, outside, not_event):
        d = asdict(s)
        for k in ("group", "verdict", "reason"):
            d.pop(k)
        legacy.execute("INSERT INTO shots VALUES (?, ?, ?)", (s.key, Store.signature(s.files), json.dumps(d)))
    legacy.commit()
    legacy.close()

    store = Store(tmp_path / "karsija.sqlite")
    assert store.import_legacy_cache(tmp_path / "cache.sqlite", root) == 1
    assert store.get(inside.key, Store.signature(inside.files)) == inside
    assert store.get(outside.key, Store.signature(outside.files)) is None
    assert store.db.execute("SELECT rel_path FROM events").fetchall() == [("2025/2025-06-01-test",)]
