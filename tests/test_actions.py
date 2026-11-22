from __future__ import annotations

from pathlib import Path

from karsija.actions import REJECT_DIR, move_rejects, write_xmp
from karsija.analysis import Shot


def make_pair(tmp_path: Path, verdict: str) -> Shot:
    raw, jpg = tmp_path / "a.nef", tmp_path / "a.jpg"
    raw.write_bytes(b"raw")
    jpg.write_bytes(b"jpeg")
    return Shot(key=str(raw), files=[str(jpg), str(raw)], verdict=verdict)


def test_write_xmp(tmp_path: Path) -> None:
    shot = make_pair(tmp_path, "best")
    assert write_xmp(shot) == "written"
    text = (tmp_path / "a.xmp").read_text(encoding="utf-8")
    assert 'xmp:Rating="5"' in text and 'xmp:Label="Green"' in text
    assert write_xmp(shot) == "exists"
    assert write_xmp(make_pair(tmp_path, "error")) == "skip"


def test_move_rejects(tmp_path: Path) -> None:
    shot = make_pair(tmp_path, "blurry")
    (tmp_path / "a.nef.xmp").write_text("darktable")
    (tmp_path / "a.xmp").write_text("digikam")
    assert not move_rejects(shot, {"similar"})
    assert move_rejects(shot, {"blurry", "similar"})
    assert sorted(p.name for p in tmp_path.iterdir()) == [REJECT_DIR]
    assert sorted(p.name for p in (tmp_path / REJECT_DIR).iterdir()) == ["a.jpg", "a.nef", "a.nef.xmp", "a.xmp"]
