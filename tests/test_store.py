from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest
from conftest import EXIF_META, Library

from karsija.analysis import DEFAULT_SETTINGS, Shot
from karsija.store import SCHEMA, SCHEMA_VERSION, UNSET, Store, default_db_path


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


def test_exif_roundtrip_and_backfill(tmp_path: Path, library: Library) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    src = library.path("sharp.jpg")
    shot = Shot(key=str(src), files=[str(src)], exif={"iso": 400})
    sig = Store.signature(shot.files)
    store.put(store.event_id("2025/2025-06-01-test"), shot, sig)
    assert store.get(shot.key, sig) == shot

    # rows of schema 1 have no EXIF: it is read from the file on the next cache hit, without re-analysis
    store.db.execute("UPDATE shots SET exif = NULL")
    hit = store.get(shot.key, sig)
    assert hit is not None and hit.exif == {**EXIF_META, "datetime_original": "2025:06:01 12:00:00"}
    assert json.loads(store.db.execute("SELECT exif FROM shots").fetchone()[0]) == hit.exif


def test_upgrade_from_schema_1(tmp_path: Path) -> None:
    path = tmp_path / "karsija.sqlite"
    v1 = SCHEMA.replace("'moved', 'missing'", "'moved'")
    v1 = v1[: v1.index("CREATE TABLE IF NOT EXISTS decisions")]
    db = sqlite3.connect(path)
    db.executescript(v1 + "PRAGMA user_version = 1;")
    db.execute("INSERT INTO events (rel_path, year, name) VALUES ('2025/2025-06-01-test', 2025, '2025-06-01-test')")
    db.execute("INSERT INTO shots (event_id, key, files, sig, user_verdict) VALUES (1, '/a.jpg', '[]', 's', 'keep')")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE shots SET state = 'missing'")
    db.close()

    store = Store(path)
    assert store.db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    assert store.db.execute("SELECT key, user_verdict FROM shots").fetchall() == [("/a.jpg", "keep")]
    store.mark_missing(1, set())
    assert store.db.execute("SELECT state FROM shots").fetchone() == ("missing",)
    assert store.create_job("analyse", "all") == 1
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {r[0] for r in store.db.execute("SELECT name FROM sqlite_master")} >= {"shots_event", "decisions", "jobs"}


def test_mark_missing(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    event = store.event_id("2025/2025-06-01-test")
    a, b, c = (make_shot(tmp_path / n) for n in ("a.jpg", "b.jpg", "c.jpg"))
    for s in (a, b, c):
        store.put(event, s, "sig")
        s.group, s.verdict, s.reason = 1, "keep", ""
    store.save_verdicts([a, b, c])
    store.mark_moved([c.key])
    state = lambda: store.db.execute("SELECT state, group_id, auto_verdict FROM shots ORDER BY id").fetchall()  # noqa: E731

    store.mark_missing(event, {a.key})
    assert state() == [("present", 1, "keep"), ("missing", None, None), ("moved", 1, "keep")]
    assert [s.key for s in store.event_shots(event)] == [a.key]
    store.mark_missing(event, {a.key, b.key})
    assert state()[1] == ("present", None, None)


def test_settings_in_effect(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    assert store.settings() == DEFAULT_SETTINGS
    store.set_settings({"gap": 5.0})
    assert store.settings() == {**DEFAULT_SETTINGS, "gap": 5.0}


def test_decide_and_undo(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    event = store.event_id("2025/2025-06-01-test")
    ids = []
    for name in ("a.jpg", "b.jpg"):
        store.put(event, make_shot(tmp_path / name), "sig")
        ids.append(store.db.execute("SELECT id FROM shots WHERE key = ?", (str(tmp_path / name),)).fetchone()[0])
    state = lambda: store.db.execute("SELECT user_verdict, rating FROM shots ORDER BY id").fetchall()  # noqa: E731

    assert store.decide([(ids[0], "keep", 4)]) == 1
    assert store.decide([(ids[0], "reject", UNSET), (ids[1], UNSET, 2)]) == 2
    assert state() == [("reject", 4), (None, 2)]
    assert store.undo() == (2, ids)
    assert state() == [("keep", 4), (None, None)]
    assert store.undo() == (1, [ids[0]])
    assert state() == [(None, None), (None, None)]
    assert store.db.execute("SELECT decided_at FROM shots WHERE decided_at IS NOT NULL").fetchall() == []
    assert store.undo() == (None, [])


def test_same_shot_twice_in_a_batch_undoes_to_the_first_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    store.put(store.event_id("2025/2025-06-01-test"), make_shot(tmp_path / "a.jpg"), "sig")
    store.decide([(1, "keep", UNSET), (1, "reject", 1)])
    store.undo()
    assert store.db.execute("SELECT user_verdict, rating FROM shots").fetchone() == (None, None)


def test_jobs(tmp_path: Path) -> None:
    store = Store(tmp_path / "karsija.sqlite")
    a, b = store.create_job("analyse", "all"), store.create_job("analyse", "3")
    store.update_job(a, status="done", total=2, done=2, finished=1.0, ignored="x")
    job = store.job(a)
    assert job is not None and (job["status"], job["total"], job["done"]) == ("done", 2, 2)
    store.fail_unfinished_jobs()
    assert [store.job(i)["status"] for i in (a, b)] == ["done", "failed"]  # type: ignore[index]
    assert store.job(99) is None


def test_default_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = default_db_path(tmp_path / "lib")
    assert path.parent == tmp_path / "data" / "karsija" and path.suffix == ".sqlite"
    assert default_db_path(tmp_path / "lib" / ".." / "lib") == path
    assert default_db_path(tmp_path / "other") != path
    monkeypatch.delenv("XDG_DATA_HOME")
    assert default_db_path(tmp_path / "lib").parent == Path.home() / ".local" / "share" / "karsija"
