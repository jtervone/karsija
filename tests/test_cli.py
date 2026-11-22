from __future__ import annotations

import csv
import json
import shutil
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import BURST, EXPECTED, Library
from fastapi.testclient import TestClient

from karsija import cli as cli_mod
from karsija.cli import bind_error, main
from karsija.store import default_db_path


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


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record uvicorn.run instead of starting a server; run the app's startup hook like uvicorn would."""
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setattr(cli_mod, "bind_error", lambda host, port: "")
    return calls


def test_serve_defaults(library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        fake_uvicorn: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert main(["serve", str(library.root), "--no-browser"]) == 0
    (call,) = fake_uvicorn
    assert (call["host"], call["port"]) == ("127.0.0.1", 8765)
    assert call["app"].state.root == library.root.resolve()
    assert call["app"].state.db_path == default_db_path(library.root)
    assert call["app"].state.db_path.parent.is_dir()
    out, err = capsys.readouterr()
    assert "http://127.0.0.1:8765/" in out and err == ""
    assert not any(library.root.rglob("*.sqlite"))           # nothing is written into the library


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.local"])
def test_serve_warns_on_non_loopback_host(library: Library, tmp_path: Path, host: str,
                                          fake_uvicorn: list[dict[str, Any]],
                                          capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "db" / "k.sqlite"
    assert main(["serve", str(library.root), "--host", host, "--port", "9000", "--no-browser", "--db", str(db)]) == 0
    assert fake_uvicorn[0]["host"] == host and fake_uvicorn[0]["app"].state.db_path == db
    assert "VAROITUS" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_serve_loopback_hosts_do_not_warn(library: Library, tmp_path: Path, host: str,
                                          fake_uvicorn: list[dict[str, Any]],
                                          capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(library.root), "--host", host, "--no-browser", "--db", str(tmp_path / "k.sqlite")]) == 0
    assert capsys.readouterr().err == ""


def test_serve_opens_browser_when_ready(library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        fake_uvicorn: list[dict[str, Any]]) -> None:
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert main(["serve", str(library.root), "--port", "9001", "--db", str(tmp_path / "k.sqlite")]) == 0
    assert opened == []                                       # not before the server is up
    with TestClient(fake_uvicorn[0]["app"]):
        pass
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == ["http://127.0.0.1:9001/"]


def test_serve_requires_existing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", str(tmp_path / "missing")]) == 1
    assert "Kansiota ei löydy" in capsys.readouterr().err


def allowed_hosts(app: Any) -> list[str]:
    (guard,) = [m for m in app.user_middleware if m.cls.__name__ == "RequestGuard"]
    return list(guard.kwargs["allowed_hosts"])


def test_serve_any_address_prints_a_usable_url(library: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               fake_uvicorn: list[dict[str, Any]],
                                               capsys: pytest.CaptureFixture[str]) -> None:
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    assert main(["serve", str(library.root), "--host", "0.0.0.0", "--port", "9002", "--db", str(tmp_path / "k")]) == 0
    assert "Osoite:       http://127.0.0.1:9002/  (kuuntelee kaikissa" in capsys.readouterr().out
    assert allowed_hosts(fake_uvicorn[0]["app"]) == ["*"]
    with TestClient(fake_uvicorn[0]["app"]):
        pass
    for _ in range(50):
        if opened:
            break
        time.sleep(0.02)
    assert opened == ["http://127.0.0.1:9002/"]


def test_serve_allows_its_own_host_name(library: Library, tmp_path: Path, fake_uvicorn: list[dict[str, Any]]) -> None:
    for host in ("::1", "192.168.1.10"):
        assert main(["serve", str(library.root), "--host", host, "--no-browser", "--db", str(tmp_path / "k")]) == 0
    assert allowed_hosts(fake_uvicorn[0]["app"]) == ["127.0.0.1", "localhost", "[::1]", "[::1]"]
    assert allowed_hosts(fake_uvicorn[1]["app"]) == ["127.0.0.1", "localhost", "[::1]", "192.168.1.10"]


def test_serve_busy_port(library: Library, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        assert bind_error("127.0.0.1", port) == f"Portti {port} on jo käytössä; valitse toinen --port."
        assert main(["serve", str(library.root), "--port", str(port), "--no-browser", "--db", str(tmp_path / "k")]) == 1
    out, err = capsys.readouterr()
    assert "on jo käytössä" in err and "Osoite" not in out
    assert bind_error("127.0.0.1", port) == ""
