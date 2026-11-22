from __future__ import annotations

import csv
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


def test_scan_rejects_bad_move(library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["scan", str(library.root), "--report", str(tmp_path), "--workers", "1", "--move", "best"]
    assert main(args) == 1
    assert "--move" in capsys.readouterr().err


def test_scan_requires_existing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(tmp_path / "missing"), "--report", str(tmp_path / "r")]) == 1
    assert "Kansiota ei löydy" in capsys.readouterr().err
