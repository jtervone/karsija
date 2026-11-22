from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import BURST, EXPECTED, Library

from karsija.cli import main


def read_verdicts(report: Path) -> dict[str, str]:
    with (report / "tulokset.csv").open(encoding="utf-8") as f:
        return {row["tiedosto"]: row["tuomio"] for row in csv.DictReader(f)}


def test_scan(library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "report"
    args = ["scan", str(library.root), "--report", str(report), "--workers", "2"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "1 kansiota, 10 kuvaa (0 välimuistista, 10 analysoitavana)" in out

    verdicts = read_verdicts(report)
    assert {n: verdicts[n] for n in EXPECTED} == EXPECTED
    assert sorted(verdicts[n] for n in BURST) == ["best", "similar", "similar", "similar", "similar"]
    html = (report / "index.html").read_text(encoding="utf-8")
    assert "Karsintaraportti" in html and "burst_0.jpg" in html
    assert len(list((report / "thumbs").iterdir())) == 10

    # second run is served from the store and gives the same result
    assert main(args) == 0
    assert "(10 välimuistista, 0 analysoitavana)" in capsys.readouterr().out
    assert read_verdicts(report) == verdicts


def test_scan_only_filter(library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(library.root), "--report", str(tmp_path), "--only", "2024"]) == 1
    assert "Ei YYYY/YYYY-MM-DD" in capsys.readouterr().err


def test_scan_rejects_bad_move_before_analysis(
    library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report"
    assert main(["scan", str(library.root), "--report", str(report), "--move", "blury"]) == 1
    assert "--move hyväksyy vain arvot blurry,similar" in capsys.readouterr().err
    assert not report.exists()


def test_scan_move_marks_shots_moved(library: Library, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    shutil.copytree(library.root, root)
    report = tmp_path / "report"
    assert main(["scan", str(root), "--report", str(report), "--workers", "2", "--move", "blurry"]) == 0

    event = root / library.event.relative_to(library.root)
    assert sorted(p.name for p in (event / "_karsitut").iterdir()) == ["defocus.jpg", "motion.jpg"]
    db = sqlite3.connect(report / "karsija.sqlite")
    states = {Path(k).name: s for k, s in db.execute("SELECT key, state FROM shots")}
    assert states["motion.jpg"] == states["defocus.jpg"] == "moved"
    assert {n for n, s in states.items() if s == "moved"} == {"motion.jpg", "defocus.jpg"}


def test_scan_imports_legacy_cache(library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "report"
    assert main(["scan", str(library.root), "--report", str(report), "--workers", "2"]) == 0
    verdicts = read_verdicts(report)
    # rebuild the old karsija.py cache from the new store, then start from it alone
    db = sqlite3.connect(report / "karsija.sqlite")
    rows = db.execute("SELECT key, sig, files, taken, sharp, sharp_global, clip_hi, clip_lo, dhash, hist, error "
                      "FROM shots").fetchall()
    db.close()
    (report / "karsija.sqlite").unlink()
    legacy = sqlite3.connect(report / "cache.sqlite")
    legacy.execute("CREATE TABLE shots (key TEXT PRIMARY KEY, sig TEXT, data TEXT)")
    for key, sig, files, taken, sharp, sharp_global, clip_hi, clip_lo, dh, hist, error in rows:
        data = {"key": key, "files": json.loads(files), "taken": taken, "sharp": sharp, "sharp_global": sharp_global,
                "clip_hi": clip_hi, "clip_lo": clip_lo, "dhash": dh, "hist": json.loads(hist), "error": error}
        legacy.execute("INSERT INTO shots VALUES (?, ?, ?)", (key, sig, json.dumps(data)))
    legacy.commit()
    legacy.close()
    capsys.readouterr()

    assert main(["scan", str(library.root), "--report", str(report), "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "Tuotiin 10 analyysiä vanhasta välimuistista (cache.sqlite)" in out
    assert "(10 välimuistista, 0 analysoitavana)" in out
    assert read_verdicts(report) == verdicts


def test_old_invocation_gets_a_hint(library: Library, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(library.root)]) == 2
    assert f"Käytä: karsija scan {library.root}" in capsys.readouterr().err


def test_scan_requires_existing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(tmp_path / "missing"), "--report", str(tmp_path / "r")]) == 1
    assert "Kansiota ei löydy" in capsys.readouterr().err
