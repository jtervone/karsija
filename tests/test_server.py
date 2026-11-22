from __future__ import annotations

import asyncio
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import BURST, EVENT, EXIF_META, EXPECTED, T0, Library, save_jpeg, texture
from fastapi import FastAPI
from fastapi.testclient import TestClient

from karsija.server import Broker, create_app

SECRET = b"secret outside the library"


@dataclass
class Server:
    client: TestClient
    app: FastAPI
    root: Path
    db: Path
    event_id: int
    published: list[tuple[str, Any]]
    job: dict[str, Any]

    def shots(self) -> dict[str, dict[str, Any]]:
        """Shots of the synthetic event by file name."""
        data = self.client.get(f"/api/events/{self.event_id}").json()
        return {s["name"]: s for g in data["groups"] for s in g["shots"]}


def wait_for_job(client: TestClient, job_id: int, timeout: float = 120) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


@pytest.fixture(scope="module")
def server(library: Library, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """The synthetic library plus traps outside it, analysed once by the server."""
    base = tmp_path_factory.mktemp("server")
    root = base / "lib"
    shutil.copytree(library.root, root)
    outside = base / "outside"
    (outside / "2025-07-01-elsewhere").mkdir(parents=True)
    (base / "secret.jpg").write_bytes(SECRET)
    save_jpeg(outside / "2025-07-01-elsewhere" / "x.jpg", texture(9), T0)
    # a file and an event folder inside the root that are symlinks pointing outside it
    (root / EVENT / "link.jpg").symlink_to(base / "secret.jpg")
    (root / "2025" / "2025-07-01-elsewhere").symlink_to(outside / "2025-07-01-elsewhere")

    db = base / "state" / "karsija.sqlite"
    db.parent.mkdir()
    app = create_app(root, db, workers=2)
    published: list[tuple[str, Any]] = []
    broker: Broker = app.state.broker
    publish = broker.publish

    def record(event: str, data: Any) -> None:
        published.append((event, data))
        publish(event, data)

    broker.publish = record  # type: ignore[method-assign]
    with TestClient(app) as client:
        lib = client.get("/api/library").json()
        event_id = lib["years"][0]["events"][0]["id"]
        resp = client.post("/api/analyse", json={"scope": "all"})
        assert resp.status_code == 202
        job = wait_for_job(client, resp.json()["job_id"])
        yield Server(client, app, root, db, event_id, published, job)


@pytest.fixture
def clean(server: Server) -> Server:
    """No user decisions and an empty undo stack."""
    db = sqlite3.connect(server.db)
    db.execute("UPDATE shots SET user_verdict = NULL, rating = NULL, decided_at = NULL")
    db.execute("DELETE FROM decisions")
    db.commit()
    db.close()
    return server


# --------------------------------------------------------------------------- #
# Library and analysis
# --------------------------------------------------------------------------- #
def test_library_lists_only_events_inside_root(server: Server) -> None:
    lib = server.client.get("/api/library").json()
    assert lib["root"] == str(server.root.resolve())
    assert [y["year"] for y in lib["years"]] == [2025]
    (ev,) = lib["years"][0]["events"]
    assert ev["rel_path"] == EVENT.as_posix()
    assert ev["shots"] == 10 and ev["reviewed"] + ev["unreviewed"] == 10
    assert ev["candidates"] == 6               # motion, defocus and 4 similar burst frames


def test_analysis_job_completes(server: Server) -> None:
    assert server.job["status"] == "done", server.job["error"]
    assert server.job["total"] == server.job["done"] == 10
    assert server.job["started"] <= server.job["finished"]


def test_event_groups_and_verdicts(server: Server) -> None:
    data = server.client.get(f"/api/events/{server.event_id}").json()
    shots = {s["name"]: s for g in data["groups"] for s in g["shots"]}
    assert "link.jpg" not in shots                     # symlink pointing outside the root was skipped
    assert {n: shots[n]["auto_verdict"] for n in EXPECTED} == EXPECTED
    burst_groups = {shots[n]["group_id"] for n in BURST}
    assert len(burst_groups) == 1
    (burst,) = [g for g in data["groups"] if g["group_id"] in burst_groups]
    assert sorted(s["auto_verdict"] for s in burst["shots"]) == ["best", "similar", "similar", "similar", "similar"]
    assert len(data["groups"]) == 6                    # 5 single shots + the burst
    assert all(s["reason"] is not None for s in shots.values())
    assert shots["DSC_0100.NEF"]["files"] == [f"{EVENT.as_posix()}/DSC_0100.JPG", f"{EVENT.as_posix()}/DSC_0100.NEF"]
    assert len(data["sharpness"]) == 10 and data["median_sharp"] > 0
    assert data["settings"]["hamming"] == 40


def test_shot_detail_has_exif(server: Server) -> None:
    shot_id = server.shots()["sharp.jpg"]["id"]
    detail = server.client.get(f"/api/shots/{shot_id}").json()
    assert detail["exif"] == {**EXIF_META, "datetime_original": "2025:06:01 12:00:00"}
    assert detail["path"] == f"{EVENT.as_posix()}/sharp.jpg"
    assert server.client.get("/api/shots/99999").status_code == 404


def test_progress_is_published(server: Server) -> None:
    jobs = [d for e, d in server.published if e == "job" and d["id"] == server.job["id"]]
    assert [j["status"] for j in jobs][:2] == ["running", "running"]
    assert jobs[-1]["status"] == "done"
    assert [j["done"] for j in jobs if j["status"] == "running"][-1] == 10


def test_second_analysis_is_served_from_cache(server: Server) -> None:
    resp = server.client.post("/api/analyse", json={"scope": server.event_id})
    job = wait_for_job(server.client, resp.json()["job_id"])
    assert job["status"] == "done" and job["total"] == 0 and job["scope"] == str(server.event_id)
    assert {n: s["auto_verdict"] for n, s in server.shots().items() if n in EXPECTED} == EXPECTED


def test_analyse_unknown_event(server: Server) -> None:
    assert server.client.post("/api/analyse", json={"scope": 999}).status_code == 404
    assert server.client.post("/api/analyse", json={"scope": "some"}).status_code == 422
    assert server.client.get("/api/jobs/999").status_code == 404


def test_settings_defaults(server: Server) -> None:
    assert server.client.get("/api/settings").json() == {
        "gap": 3.0, "hamming": 40, "abs_blur": 10.0, "rel_blur": 0.25, "sim_keep": 0.0}


def test_static_index(server: Server) -> None:
    resp = server.client.get("/")
    assert resp.status_code == 200 and "karsija" in resp.text


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def test_decision_and_undo(clean: Server) -> None:
    client, shot_id = clean.client, clean.shots()["sharp.jpg"]["id"]
    resp = client.patch(f"/api/shots/{shot_id}", json={"user_verdict": "reject"})
    assert resp.status_code == 200
    (shot,) = resp.json()["shots"]
    assert shot["user_verdict"] == "reject" and shot["rating"] is None and shot["decided_at"]

    (shot,) = client.patch(f"/api/shots/{shot_id}", json={"rating": 4}).json()["shots"]
    assert (shot["user_verdict"], shot["rating"]) == ("reject", 4)          # the verdict is kept

    undo = client.post("/api/decisions/undo").json()
    assert [(s["user_verdict"], s["rating"]) for s in undo["shots"]] == [("reject", None)]
    undo = client.post("/api/decisions/undo").json()
    assert [(s["user_verdict"], s["rating"], s["decided_at"]) for s in undo["shots"]] == [(None, None, None)]
    assert client.post("/api/decisions/undo").json() == {"batch": None, "shots": []}


def test_decision_can_clear_verdict(clean: Server) -> None:
    shot_id = clean.shots()["sharp.jpg"]["id"]
    clean.client.patch(f"/api/shots/{shot_id}", json={"user_verdict": "keep", "rating": 5})
    (shot,) = clean.client.patch(f"/api/shots/{shot_id}", json={"user_verdict": None}).json()["shots"]
    assert (shot["user_verdict"], shot["rating"]) == (None, 5)


@pytest.mark.parametrize("body", [{}, {"user_verdict": "maybe"}, {"rating": 6}, {"rating": -1}, {"other": 1}])
def test_decision_validation(clean: Server, body: dict[str, Any]) -> None:
    shot_id = clean.shots()["sharp.jpg"]["id"]
    assert clean.client.patch(f"/api/shots/{shot_id}", json=body).status_code == 422
    assert clean.client.patch("/api/shots/99999", json={"rating": 1}).status_code == 404


def test_group_accept_is_undone_as_one_batch(clean: Server) -> None:
    client, shots = clean.client, clean.shots()
    burst = {n: shots[n] for n in BURST}
    best = next(n for n, s in burst.items() if s["auto_verdict"] == "best")
    other = next(n for n in BURST if n != best)
    client.patch(f"/api/shots/{burst[other]['id']}", json={"user_verdict": "keep", "rating": 3})

    resp = client.post(f"/api/groups/{clean.event_id}/{burst[best]['group_id']}/accept")
    assert resp.status_code == 200
    after = {s["name"]: s for s in resp.json()["shots"]}
    assert set(after) == set(BURST)
    assert {n: s["user_verdict"] for n, s in after.items()} == {n: "keep" if n == best else "reject" for n in BURST}
    assert after[other]["rating"] == 3                 # accept sets the verdict only

    client.patch(f"/api/shots/{shots['sharp.jpg']['id']}", json={"user_verdict": "keep"})
    assert [s["name"] for s in client.post("/api/decisions/undo").json()["shots"]] == ["sharp.jpg"]

    undone = {s["name"]: s for s in client.post("/api/decisions/undo").json()["shots"]}
    assert set(undone) == set(BURST)                   # the whole group in one step
    now = clean.shots()
    assert {n: (now[n]["user_verdict"], now[n]["rating"]) for n in BURST} == {
        n: ("keep", 3) if n == other else (None, None) for n in BURST}


def test_group_accept_unknown_group(clean: Server) -> None:
    assert clean.client.post(f"/api/groups/{clean.event_id}/999/accept").status_code == 404


def test_library_counts_reviewed(clean: Server) -> None:
    clean.client.patch(f"/api/shots/{clean.shots()['sharp.jpg']['id']}", json={"user_verdict": "keep"})
    ev = clean.client.get("/api/library").json()["years"][0]["events"][0]
    assert (ev["reviewed"], ev["unreviewed"]) == (1, 9)


# --------------------------------------------------------------------------- #
# Path traversal
# --------------------------------------------------------------------------- #
def raw_get(app: FastAPI, path: str) -> tuple[int, bytes]:
    """GET with the path sent as is: HTTP clients would normalise the ``..`` segments away."""
    status, body = 0, b""

    async def run() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg: dict[str, Any]) -> None:
            nonlocal status, body
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                body += msg.get("body", b"")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [], "server": ("testserver", 80), "client": ("testclient", 1)}
        await app(scope, receive, send)

    asyncio.run(run())
    return status, body


def test_files_inside_root_are_served(server: Server) -> None:
    resp = server.client.get(f"/api/files/{EVENT.as_posix()}/sharp.jpg")
    assert resp.status_code == 200
    assert resp.content == (server.root / EVENT / "sharp.jpg").read_bytes()
    assert server.client.get(f"/api/files/{EVENT.as_posix()}/missing.jpg").status_code == 404
    assert server.client.get("/api/files/2025").status_code == 404          # folders are not files


@pytest.mark.parametrize("path", [
    "/api/files/../secret.jpg",
    "/api/files/2025/2025-06-01-test/../../../secret.jpg",
    "/api/files/%2e%2e/secret.jpg",
    "/api/files/2025/%2e%2e/%2E%2E/secret.jpg",
    "/api/files/..%2fsecret.jpg",
    "/api/files/2025/2025-06-01-test/link.jpg",                       # symlink to ../../../secret.jpg
    "/api/files/2025/2025-07-01-elsewhere/x.jpg",                     # inside a symlinked folder
    "/..%2f..%2fsecret.jpg",                                          # static files
    "/%2e%2e/%2e%2e/secret.jpg",
    "/../secret.jpg",
])
def test_path_traversal_is_rejected(server: Server, path: str) -> None:
    for status, body in (raw_get(server.app, path), (lambda r: (r.status_code, r.content))(server.client.get(path))):
        assert status in (403, 404), path
        assert SECRET not in body and b"\xff\xd8" not in body[:2]


def test_absolute_paths_are_rejected(server: Server) -> None:
    for target in (server.root.parent / "secret.jpg", server.root / EVENT / "sharp.jpg"):
        status, body = raw_get(server.app, f"/api/files/{target}")       # /api/files//abs/path
        assert status == 403 and SECRET not in body
        assert server.client.get(f"/api/files/{str(target).replace('/', '%2F')}").status_code == 403


# --------------------------------------------------------------------------- #
# Server-sent events
# --------------------------------------------------------------------------- #
def test_broker_stream() -> None:
    broker = Broker()
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    async def run() -> list[str]:
        nonlocal disconnected
        stream = broker.stream(is_disconnected, ["event: job\ndata: {}\n\n"])
        got = [await anext(stream)]
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.05)                       # subscribed and waiting
        t = threading.Thread(target=broker.publish, args=("job", {"id": 1, "done": 2}))
        t.start()
        t.join()
        got.append(await asyncio.wait_for(pending, 5))
        disconnected = True
        got.extend([m async for m in stream])
        return got

    assert asyncio.run(run()) == ["event: job\ndata: {}\n\n", 'event: job\ndata: {"id": 1, "done": 2}\n\n']
    assert not broker._subs
