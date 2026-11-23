"""Toteuta (apply) and Palauta karsitut (restore) on a small library of placeholder files."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from karsija import apply as applies
from karsija.actions import REJECT_DIR, XMP_NS
from karsija.analysis import Shot
from karsija.library import collect_shots
from karsija.server import create_app
from karsija.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
EVENT = "2025/2025-06-01-test"
RATING, LABEL = f"{{{XMP_NS}}}Rating", f"{{{XMP_NS}}}Label"
JSON = {"Content-Type": "application/json"}

# file -> content; image files are placeholders, nothing here reads them as images
FILES = {
    "a.jpg": b"jpeg a",  # reject, sidecars of both styles
    "a.jpg.xmp": (FIXTURES / "darktable.nef.xmp").read_bytes(),
    "a.xmp": (FIXTURES / "digikam.xmp").read_bytes(),
    "DSC_1.NEF": b"raw 1",  # reject, RAW+JPEG with every sidecar
    "DSC_1.JPG": b"jpeg 1",
    "DSC_1.NEF.xmp": (FIXTURES / "darktable.nef.xmp").read_bytes(),
    "DSC_1.JPG.xmp": (FIXTURES / "darktable.nef.xmp").read_bytes(),
    "DSC_1.xmp": (FIXTURES / "digikam.xmp").read_bytes(),
    "b.jpg": b"jpeg b",  # keep, no sidecar: one is created
    "c.jpg": b"jpeg c",  # keep with 4 stars: its sidecar is updated
    "c.xmp": (FIXTURES / "digikam.xmp").read_bytes(),
    "d.jpg": b"jpeg d",  # no decision: never touched
    "d.xmp": (FIXTURES / "digikam.xmp").read_bytes(),
    "e.jpg": b"jpeg e",  # reject without a sidecar: created, then moved
    "f.jpg": b"jpeg f",  # keep without stars, the user's own 2 stars stay
    "f.xmp": (FIXTURES / "digikam.xmp").read_bytes(),
}
DECISIONS = {
    "a.jpg": ("reject", None),
    "DSC_1.NEF": ("reject", None),
    "b.jpg": ("keep", None),
    "c.jpg": ("keep", 4),
    "e.jpg": ("reject", None),
    "f.jpg": ("keep", None),
}


@dataclass
class Lib:
    root: Path
    event: Path
    store: Store
    event_id: int
    undone: Path

    def shot_id(self, name: str) -> int:
        return int(self.store.db.execute("SELECT id FROM shots WHERE key = ?", (str(self.event / name),)).fetchone()[0])

    def states(self) -> dict[str, str]:
        return {Path(k).name: s for k, s in self.store.db.execute("SELECT key, state FROM shots")}


@pytest.fixture
def lib(tmp_path: Path) -> Iterator[Lib]:
    root = tmp_path / "lib"
    event = root / EVENT
    event.mkdir(parents=True)
    for name, content in FILES.items():
        (event / name).write_bytes(content)
    store = Store(tmp_path / "state.sqlite")
    event_id = store.event_id(EVENT)
    for primary, files in collect_shots(event):
        store.put(event_id, Shot(key=primary, files=files), "sig")
    store.db.execute("UPDATE shots SET auto_verdict = 'keep', group_id = 0")
    for name, (verdict, rating) in DECISIONS.items():
        store.db.execute(
            "UPDATE shots SET user_verdict = ?, rating = ? WHERE key = ?", (verdict, rating, str(event / name))
        )
    store.commit()
    yield Lib(root, event, store, event_id, tmp_path / "undone")
    store.close()


def tree(folder: Path) -> dict[str, tuple[int, str] | str]:
    """Every path under ``folder``: size and content hash of files, ``dir`` for folders."""
    out: dict[str, tuple[int, str] | str] = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        out[rel] = "dir" if p.is_dir() else (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def xmp(path: Path) -> tuple[str | None, str | None]:
    desc = ET.parse(path).getroot().find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc is not None
    return desc.get(RATING), desc.get(LABEL)


def plan(lib: Lib) -> applies.Plan:
    return applies.build_plan(lib.store, lib.root, lib.event_id)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def test_plan_lists_moves_xmps_and_unreviewed_without_changing_anything(lib: Lib) -> None:
    before = tree(lib.root)
    data = applies.plan_json(plan(lib), lib.root)
    assert tree(lib.root) == before
    ev = f"{EVENT}/"
    moves = {Path(m["files"][0]).name: sorted(Path(f).name for f in m["files"]) for m in data["move"]}
    assert moves == {
        "a.jpg": ["a.jpg", "a.jpg.xmp", "a.xmp"],
        "DSC_1.JPG": ["DSC_1.JPG", "DSC_1.JPG.xmp", "DSC_1.NEF", "DSC_1.NEF.xmp", "DSC_1.xmp"],
        "e.jpg": ["e.jpg", "e.xmp"],
    }
    assert sorted(data["xmp_create"]) == [ev + "b.xmp", ev + "e.xmp"]
    assert sorted(data["xmp_update"]) == sorted(
        ev + n for n in ("a.jpg.xmp", "a.xmp", "DSC_1.NEF.xmp", "DSC_1.JPG.xmp", "DSC_1.xmp", "c.xmp")
    )
    assert data["user_values"] == [ev + "f.xmp"]
    assert data["unreviewed"] == 1
    assert data["conflicts"] == []
    assert data["reject_dir"] == ev + REJECT_DIR
    assert data["confirm_token"] == plan(lib).token  # the same files give the same token


def test_token_changes_with_the_decisions_and_the_files(lib: Lib) -> None:
    token = plan(lib).token
    lib.store.db.execute("UPDATE shots SET user_verdict = 'keep' WHERE id = ?", (lib.shot_id("e.jpg"),))
    assert plan(lib).token != token
    lib.store.db.execute("UPDATE shots SET user_verdict = 'reject' WHERE id = ?", (lib.shot_id("e.jpg"),))
    assert plan(lib).token == token
    (lib.event / "a.jpg").write_bytes(b"edited meanwhile")
    assert plan(lib).token != token


# --------------------------------------------------------------------------- #
# Apply and restore
# --------------------------------------------------------------------------- #
def test_apply_and_restore_leave_the_event_as_it_was(lib: Lib) -> None:
    before = tree(lib.root)
    apply_id = applies.apply(lib.store, lib.root, plan(lib))

    rejects = lib.event / REJECT_DIR
    assert sorted(p.name for p in rejects.iterdir()) == sorted(
        [
            "a.jpg",
            "a.jpg.xmp",
            "a.xmp",
            "DSC_1.JPG",
            "DSC_1.JPG.xmp",
            "DSC_1.NEF",
            "DSC_1.NEF.xmp",
            "DSC_1.xmp",
            "e.jpg",
            "e.xmp",
            f"karsija-apply-{apply_id}.json",
        ]
    )
    for name in ("a.jpg.xmp", "a.xmp", "DSC_1.NEF.xmp", "DSC_1.JPG.xmp", "DSC_1.xmp", "e.xmp"):
        assert xmp(rejects / name) == ("-1", "Red"), name
    assert xmp(lib.event / "b.xmp") == ("3", None)
    assert xmp(lib.event / "c.xmp") == ("4", "Blue")  # the user's stars; a label of the user's own stays
    for name in ("d.jpg", "d.xmp", "f.xmp"):
        assert (lib.event / name).read_bytes() == FILES[name], name
    assert lib.states() == {
        "a.jpg": "moved",
        "DSC_1.NEF": "moved",
        "e.jpg": "moved",
        "b.jpg": "present",
        "c.jpg": "present",
        "d.jpg": "present",
        "f.jpg": "present",
    }
    info = applies.apply_info(lib.store, lib.root, apply_id)
    assert info is not None
    assert (info["status"], info["shots_moved"], info["files_moved"], info["xmp_created"], info["xmp_updated"]) == (
        "done",
        3,
        10,
        2,
        6,
    )
    assert info["done"] == info["ops"]
    log = json.loads((rejects / f"karsija-apply-{apply_id}.json").read_text())
    assert log["status"] == "done" and len(log["ops"]) == info["ops"]
    assert all(op["done"] for op in log["ops"])
    backups = [op["backup"] for op in log["ops"] if op["kind"] == "xmp_update" and op["src"].endswith("/c.xmp")]
    assert backups == [FILES["c.xmp"].decode()]
    # the apply is done: the next plan has nothing left to move
    assert applies.plan_json(plan(lib), lib.root)["move"] == []

    result = applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert tree(lib.root) == before
    kept = lib.undone / str(apply_id)
    assert (result["missing"], result["changed"]) == ([], [])
    assert sorted(result["created"]) == [str(kept / EVENT / "b.xmp"), str(kept / EVENT / "e.xmp")]
    assert result["log"] == str(kept / f"karsija-apply-{apply_id}.json")
    assert sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*") if p.is_file()) == sorted(
        [f"{EVENT}/b.xmp", f"{EVENT}/e.xmp", f"karsija-apply-{apply_id}.json"]
    )
    assert json.loads((kept / f"karsija-apply-{apply_id}.json").read_text())["status"] == "undone"
    assert set(lib.states().values()) == {"present"}
    info = applies.apply_info(lib.store, lib.root, apply_id)
    assert info is not None and info["status"] == "undone" and info["undone_at"]
    assert info["log"] == str(kept / f"karsija-apply-{apply_id}.json")

    with pytest.raises(applies.ApplyConflict, match="jo palautettu"):
        applies.restore(lib.store, lib.root, apply_id, lib.undone)


def test_unreviewed_shots_are_never_touched(lib: Lib) -> None:
    lib.store.db.execute("UPDATE shots SET user_verdict = NULL, rating = NULL")
    data = applies.plan_json(plan(lib), lib.root)
    assert (data["move"], data["xmp_create"], data["xmp_update"], data["unreviewed"]) == ([], [], [], 7)
    before = tree(lib.root)
    with pytest.raises(applies.ApplyConflict, match="ei toteutettavaa"):
        applies.apply(lib.store, lib.root, plan(lib))
    assert tree(lib.root) == before


def test_a_destination_that_exists_aborts_with_nothing_moved(lib: Lib) -> None:
    (lib.event / REJECT_DIR).mkdir()
    (lib.event / REJECT_DIR / "e.jpg").write_bytes(b"an earlier reject")
    before = tree(lib.root)
    p = plan(lib)
    assert [(str(path.relative_to(lib.root)), why) for path, why in p.conflicts] == [
        (f"{EVENT}/{REJECT_DIR}/e.jpg", "on jo olemassa; siirrä tai nimeä se ensin")
    ]
    with pytest.raises(applies.ApplyConflict):
        applies.apply(lib.store, lib.root, p)
    assert tree(lib.root) == before
    assert lib.store.db.execute("SELECT COUNT(*) FROM applies").fetchone()[0] == 0


def test_an_invalid_sidecar_is_a_conflict(lib: Lib) -> None:
    (lib.event / "c.xmp").write_text("not xmp")
    (conflict,) = plan(lib).conflicts
    assert conflict[0].name == "c.jpg" and "XMP" in conflict[1]


def test_a_sidecar_changed_after_the_apply_is_kept_on_restore(lib: Lib) -> None:
    apply_id = applies.apply(lib.store, lib.root, plan(lib))
    (lib.event / "c.xmp").write_text((lib.event / "c.xmp").read_text().replace('xmp:Rating="4"', 'xmp:Rating="5"'))
    edited = (lib.event / "c.xmp").read_bytes()
    applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert (lib.event / "c.xmp").read_bytes() == FILES["c.xmp"]
    assert (lib.undone / str(apply_id) / EVENT / "c.xmp").read_bytes() == edited


def test_a_sidecar_changed_between_plan_and_apply_stops_it(lib: Lib) -> None:
    p = plan(lib)
    (lib.event / "c.xmp").write_bytes(b"\xff changed by another program")
    with pytest.raises(applies.ApplyFailed, match="muuttui kesken"):
        applies.apply(lib.store, lib.root, p)
    assert (lib.event / "c.xmp").read_bytes() == b"\xff changed by another program"
    (apply_id,) = lib.store.db.execute("SELECT id FROM applies WHERE status = 'failed'").fetchone()
    applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert sorted(p.name for p in lib.event.iterdir()) == sorted(FILES)


def test_restore_refuses_when_a_file_is_back_and_moved_at_once(lib: Lib) -> None:
    apply_id = applies.apply(lib.store, lib.root, plan(lib))
    (lib.event / "e.jpg").write_bytes(b"a new e")
    before = tree(lib.root)
    with pytest.raises(applies.ApplyConflict, match="e.jpg"):
        applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert tree(lib.root) == before


def test_the_newest_apply_is_restored_first(lib: Lib) -> None:
    first = applies.apply(lib.store, lib.root, plan(lib))
    lib.store.db.execute("UPDATE shots SET user_verdict = 'reject' WHERE key = ?", (str(lib.event / "b.jpg"),))
    second = applies.apply(lib.store, lib.root, plan(lib))
    with pytest.raises(applies.ApplyConflict, match="uudempi"):
        applies.restore(lib.store, lib.root, first, lib.undone)
    applies.restore(lib.store, lib.root, second, lib.undone)
    applies.restore(lib.store, lib.root, first, lib.undone)
    assert set(lib.states().values()) == {"present"}
    assert not (lib.event / REJECT_DIR).exists()
    assert sorted(p.name for p in lib.event.iterdir()) == sorted(FILES)


@pytest.mark.parametrize("fail_at", [0, 2, 6])
def test_a_failed_apply_can_be_restored(lib: Lib, monkeypatch: pytest.MonkeyPatch, fail_at: int) -> None:
    before = tree(lib.root)
    real, calls = applies._move, [0]

    def move(src: Path, dst: Path) -> None:
        calls[0] += 1
        if calls[0] > fail_at:
            raise OSError(28, "No space left on device", str(dst))
        real(src, dst)

    monkeypatch.setattr(applies, "_move", move)
    with pytest.raises(applies.ApplyFailed) as failed:
        applies.apply(lib.store, lib.root, plan(lib))
    monkeypatch.undo()
    info = applies.apply_info(lib.store, lib.root, failed.value.apply_id)
    assert info is not None and info["status"] == "failed" and "No space left" in info["error"]
    applies.restore(lib.store, lib.root, failed.value.apply_id, lib.undone)
    assert tree(lib.root) == before
    assert set(lib.states().values()) == {"present"}


@pytest.mark.parametrize("moved", [False, True])
def test_an_apply_cut_short_by_a_crash_can_be_restored(lib: Lib, monkeypatch: pytest.MonkeyPatch, moved: bool) -> None:
    """The process dies during the 4th move, before or after the rename: the log says it was not done, restore
    finds out from the files."""
    before = tree(lib.root)
    real, calls = applies._move, [0]

    class Crash(BaseException):
        pass

    def move(src: Path, dst: Path) -> None:
        calls[0] += 1
        if calls[0] == 4:
            if moved:
                real(src, dst)
            raise Crash
        real(src, dst)

    monkeypatch.setattr(applies, "_move", move)
    with pytest.raises(Crash):
        applies.apply(lib.store, lib.root, plan(lib))
    monkeypatch.undo()
    lib.store.db.rollback()
    lib.store.close()
    # the next server run
    lib.store = Store(lib.root.parent / "state.sqlite")
    ((apply_id, status),) = lib.store.db.execute("SELECT id, status FROM applies").fetchall()
    assert status == "running"
    applies.fail_interrupted(lib.store)
    # DSC_1 comes first: its 3 sidecars, then 3 of its moves were done
    assert lib.store.db.execute("SELECT SUM(done), COUNT(*) FROM apply_ops").fetchone() == (3 + 3, 18)
    applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert tree(lib.root) == before


def test_restore_skips_a_sidecar_the_failed_apply_did_not_write(lib: Lib, monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = applies.write_atomic, [0]

    def write(path: Path, text: str) -> None:
        calls[0] += 1
        if calls[0] == 3:  # the 2nd sidecar write (the 1st write is the log)
            raise OSError(28, "No space left on device", str(path))
        real(path, text)

    monkeypatch.setattr(applies, "write_atomic", write)
    with pytest.raises(applies.ApplyFailed) as failed:
        applies.apply(lib.store, lib.root, plan(lib))
    monkeypatch.undo()
    # the user edits a sidecar the apply never got to
    (lib.event / "c.xmp").write_bytes(b"edited later")
    result = applies.restore(lib.store, lib.root, failed.value.apply_id, lib.undone)
    assert (lib.event / "c.xmp").read_bytes() == b"edited later"
    assert result["changed"] == []
    for name in ("a.jpg.xmp", "a.xmp", "DSC_1.xmp"):
        assert (lib.event / name).read_bytes() == FILES[name], name


def test_a_restore_cut_short_can_be_run_again(lib: Lib, monkeypatch: pytest.MonkeyPatch) -> None:
    before = tree(lib.root)
    apply_id = applies.apply(lib.store, lib.root, plan(lib))
    real, calls = applies._move, [0]

    def move(src: Path, dst: Path) -> None:
        calls[0] += 1
        if calls[0] == 5:
            raise OSError(5, "Input/output error", str(dst))
        real(src, dst)

    monkeypatch.setattr(applies, "_move", move)
    with pytest.raises(applies.ApplyFailed, match="palautus keskeytyi"):
        applies.restore(lib.store, lib.root, apply_id, lib.undone)
    monkeypatch.undo()
    info = applies.apply_info(lib.store, lib.root, apply_id)
    assert info is not None and info["status"] == "done" and "palautus keskeytyi" in info["error"]
    applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert tree(lib.root) == before


def test_a_symlinked_reject_folder_is_a_conflict(lib: Lib, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (lib.event / REJECT_DIR).symlink_to(elsewhere)
    conflicts = plan(lib).conflicts
    assert conflicts[0][0].name == REJECT_DIR and "linkki" in conflicts[0][1]
    assert {why for _, why in conflicts[1:]} == {"polku ei ole kirjastossa"}  # every move leads out


def test_a_reject_folder_symlinked_after_the_plan_stops_the_apply(lib: Lib, tmp_path: Path) -> None:
    p = plan(lib)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (lib.event / REJECT_DIR).symlink_to(elsewhere)
    before = tree(lib.root)
    with pytest.raises(applies.ApplyConflict, match="linkki"):
        applies.apply(lib.store, lib.root, p)
    assert tree(lib.root) == before and list(elsewhere.iterdir()) == []


def test_restore_does_not_write_through_a_link_out_of_the_library(lib: Lib, tmp_path: Path) -> None:
    apply_id = applies.apply(lib.store, lib.root, plan(lib))
    target = tmp_path / "bashrc"
    target.write_text("precious")
    (lib.event / "c.xmp").unlink()
    (lib.event / "c.xmp").symlink_to(target)
    with pytest.raises(applies.ApplyConflict, match="ei enää ole kirjastossa"):
        applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert target.read_text() == "precious"
    assert (lib.event / REJECT_DIR / "a.jpg").exists()  # nothing was reversed


def test_a_log_name_taken_in_the_reject_folder_stops_the_apply(lib: Lib, tmp_path: Path) -> None:
    (lib.event / REJECT_DIR).mkdir()
    target = tmp_path / "target"
    target.write_text("precious")
    (lib.event / REJECT_DIR / "karsija-apply-1.json").symlink_to(target)
    before = tree(lib.root)
    with pytest.raises(applies.ApplyConflict, match="karsija-apply-1.json"):
        applies.apply(lib.store, lib.root, plan(lib))
    assert tree(lib.root) == before and target.read_text() == "precious"
    assert lib.store.db.execute("SELECT COUNT(*) FROM applies").fetchone()[0] == 0


def test_an_unexpected_error_marks_the_apply_failed(lib: Lib, monkeypatch: pytest.MonkeyPatch) -> None:
    before = tree(lib.root)
    real, calls = applies._move, [0]

    def move(src: Path, dst: Path) -> None:
        calls[0] += 1
        if calls[0] == 2:
            raise sqlite3.OperationalError("database is locked")
        real(src, dst)

    monkeypatch.setattr(applies, "_move", move)
    with pytest.raises(sqlite3.OperationalError):
        applies.apply(lib.store, lib.root, plan(lib))
    monkeypatch.undo()
    ((apply_id, status, error),) = lib.store.db.execute("SELECT id, status, error FROM applies").fetchall()
    assert status == "failed" and "database is locked" in error
    applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert tree(lib.root) == before


def test_a_shot_whose_moved_files_are_gone_stays_out_of_the_grid(lib: Lib) -> None:
    apply_id = applies.apply(lib.store, lib.root, plan(lib))
    for name in ("e.jpg", "e.xmp"):
        (lib.event / REJECT_DIR / name).unlink()  # the user deleted them by hand
    result = applies.restore(lib.store, lib.root, apply_id, lib.undone)
    assert sorted(result["missing"]) == [f"{EVENT}/{REJECT_DIR}/e.jpg", f"{EVENT}/{REJECT_DIR}/e.xmp"]
    assert lib.states()["e.jpg"] == "missing" and lib.states()["a.jpg"] == "present"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(lib: Lib, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(lib.root, tmp_path / "state.sqlite", workers=1, undone_dir=lib.undone)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def post(client: TestClient, url: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(url, json=body) if body is not None else client.post(url, headers=JSON)


def test_api_apply_and_restore(lib: Lib, client: TestClient) -> None:
    before = tree(lib.root)
    data = client.get(f"/api/events/{lib.event_id}/apply-plan").json()
    assert (len(data["move"]), len(data["xmp_create"]) + len(data["xmp_update"]), data["unreviewed"]) == (3, 8, 1)

    resp = post(client, f"/api/events/{lib.event_id}/apply", {"confirm_token": data["confirm_token"]})
    assert resp.status_code == 200, resp.text
    applied = resp.json()["apply"]
    assert (applied["status"], applied["shots_moved"]) == ("done", 3)
    assert applied["log"] == f"{EVENT}/{REJECT_DIR}/karsija-apply-{applied['id']}.json"
    shots = client.get(f"/api/events/{lib.event_id}").json()
    assert sum(len(g["shots"]) for g in shots["groups"]) == 4  # the moved ones are gone from the event

    history = client.get(f"/api/events/{lib.event_id}/applies").json()["applies"]
    assert [a["id"] for a in history] == [applied["id"]]

    resp = post(client, f"/api/applies/{applied['id']}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["apply"]["status"] == "undone"
    assert tree(lib.root) == before
    assert post(client, f"/api/applies/{applied['id']}/restore").status_code == 409
    assert post(client, "/api/applies/999/restore").status_code == 404


def test_api_undo_does_not_reach_past_an_apply(lib: Lib, client: TestClient) -> None:
    e_id = lib.shot_id("e.jpg")
    assert client.patch(f"/api/shots/{e_id}", json={"user_verdict": "reject"}).status_code == 200
    token = client.get(f"/api/events/{lib.event_id}/apply-plan").json()["confirm_token"]
    assert post(client, f"/api/events/{lib.event_id}/apply", {"confirm_token": token}).status_code == 200
    assert post(client, "/api/decisions/undo").json() == {"batch": None, "shots": []}
    row = lib.store.db.execute("SELECT user_verdict, state FROM shots WHERE id = ?", (e_id,)).fetchone()
    assert row == ("reject", "moved")


def test_api_stale_token_is_rejected(lib: Lib, client: TestClient, tmp_path: Path) -> None:
    token = client.get(f"/api/events/{lib.event_id}/apply-plan").json()["confirm_token"]
    resp = client.patch(f"/api/shots/{lib.shot_id('b.jpg')}", json={"user_verdict": "reject"})
    assert resp.status_code == 200
    before = tree(lib.root)
    resp = post(client, f"/api/events/{lib.event_id}/apply", {"confirm_token": token})
    assert resp.status_code == 409 and "muuttunut" in resp.json()["detail"]
    assert tree(lib.root) == before
    db = sqlite3.connect(tmp_path / "state.sqlite")
    assert db.execute("SELECT COUNT(*) FROM applies").fetchone()[0] == 0
    db.close()


def test_api_plan_says_when_an_analysis_is_under_way(lib: Lib, client: TestClient) -> None:
    assert client.get(f"/api/events/{lib.event_id}/apply-plan").json()["analysing"] is False
    lib.store.db.execute("INSERT INTO jobs (kind, scope, status) VALUES ('analyse', 'all', 'running')")
    lib.store.commit()
    data = client.get(f"/api/events/{lib.event_id}/apply-plan").json()
    assert data["analysing"] is True
    resp = post(client, f"/api/events/{lib.event_id}/apply", {"confirm_token": data["confirm_token"]})
    assert resp.status_code == 409 and "analyysi" in resp.json()["detail"]


def test_api_conflict_and_unknown_event(lib: Lib, client: TestClient) -> None:
    (lib.event / REJECT_DIR).mkdir()
    (lib.event / REJECT_DIR / "a.jpg").write_bytes(b"x")
    data = client.get(f"/api/events/{lib.event_id}/apply-plan").json()
    assert data["conflicts"] == [
        {"path": f"{EVENT}/{REJECT_DIR}/a.jpg", "reason": "on jo olemassa; siirrä tai nimeä se ensin"}
    ]
    resp = post(client, f"/api/events/{lib.event_id}/apply", {"confirm_token": data["confirm_token"]})
    assert resp.status_code == 409
    assert client.get("/api/events/999/apply-plan").status_code == 404
    assert client.get("/api/events/999/applies").status_code == 404
    assert post(client, "/api/events/999/apply", {"confirm_token": "x"}).status_code == 404
