from __future__ import annotations

import asyncio
import io
import os
import re
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
from PIL import Image

from karsija import previews
from karsija import server as server_mod
from karsija.library import RawpyMissing
from karsija.server import LOOPBACK_HOSTS, Broker, JobRunner, create_app, etag_matches, host_name
from karsija.store import Store

SECRET = b"secret outside the library"
BASE = "http://127.0.0.1:8765"
JSON = {"Content-Type": "application/json"}


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
    with TestClient(app, base_url=BASE) as client:
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
    assert '<script type="module" src="app.js">' in resp.text


@pytest.mark.parametrize("path", ["/", "/app.js", "/app.css", "/vendor/preact.js"])
def test_ui_loads_nothing_from_other_origins(server: Server, path: str) -> None:
    resp = server.client.get(path)
    assert resp.status_code == 200
    csp = {d.split()[0]: d.split()[1:] for d in resp.headers["content-security-policy"].split("; ")}
    assert csp["default-src"] == ["'self'"] and csp["img-src"] == ["'self'", "data:"]
    assert resp.headers["cache-control"] == "no-cache"


def test_ui_files_refer_to_no_other_origin() -> None:
    """The page works offline: no file of the UI names a URL of another site (the policy would block it anyway)."""
    files = [p for p in server_mod.STATIC_DIR.rglob("*") if p.suffix in (".html", ".js", ".css")]
    assert {p.relative_to(server_mod.STATIC_DIR).as_posix() for p in files} >= {
        "index.html", "app.js", "app.css", "vendor/preact.js", "vendor/hooks.js", "vendor/htm.js"}
    for p in files:
        text = p.read_text().replace("http://www.w3.org/", "")    # Preact's SVG and MathML namespace names
        assert not re.search(r"""(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}/""", text, re.I), p
        for target in re.findall(r"""(?:\bfrom|\bimport)\s*["']([^"']+)["']|(?:src|href)=["']([^"']+)["']""", text):
            ref = next(t for t in target if t)
            assert ref.startswith(("./", "data:", "#")) or not re.match(r"[a-z]+:|/", ref), (p, ref)
            if not ref.startswith(("data:", "#")):
                assert (p.parent / ref).is_file(), (p, ref)


def test_ui_routes_have_no_server_side_paths(server: Server) -> None:
    """The router works on the hash: the server only ever serves the page itself."""
    assert server.client.get("/event/1").status_code == 404


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

    undo = client.post("/api/decisions/undo", headers=JSON).json()
    assert [(s["user_verdict"], s["rating"]) for s in undo["shots"]] == [("reject", None)]
    undo = client.post("/api/decisions/undo", headers=JSON).json()
    assert [(s["user_verdict"], s["rating"], s["decided_at"]) for s in undo["shots"]] == [(None, None, None)]
    assert client.post("/api/decisions/undo", headers=JSON).json() == {"batch": None, "shots": []}


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
    other = next(n for n in BURST if n != best)        # suggested "similar", kept by hand
    client.patch(f"/api/shots/{burst[other]['id']}", json={"user_verdict": "keep", "rating": 3})
    client.patch(f"/api/shots/{burst[best]['id']}", json={"rating": 5})
    undecided = set(BURST) - {other}

    resp = client.post(f"/api/groups/{clean.event_id}/{burst[best]['group_id']}/accept", headers=JSON)
    assert resp.status_code == 200
    after = {s["name"]: s for s in resp.json()["shots"]}
    assert set(after) == undecided                     # the decision made by hand is left alone
    assert {n: s["user_verdict"] for n, s in after.items()} == {n: "keep" if n == best else "reject" for n in after}
    assert after[best]["rating"] == 5                  # accept sets the verdict only
    assert clean.shots()[other]["user_verdict"] == "keep"

    client.patch(f"/api/shots/{shots['sharp.jpg']['id']}", json={"user_verdict": "keep"})
    assert [s["name"] for s in client.post("/api/decisions/undo", headers=JSON).json()["shots"]] == ["sharp.jpg"]

    undone = {s["name"]: s for s in client.post("/api/decisions/undo", headers=JSON).json()["shots"]}
    assert set(undone) == undecided                    # the whole group in one step
    now = clean.shots()
    assert {n: (now[n]["user_verdict"], now[n]["rating"]) for n in BURST} == {
        n: ("keep", 3) if n == other else (None, 5) if n == best else (None, None) for n in BURST}


def test_group_accept_with_everything_decided(clean: Server) -> None:
    shots = clean.shots()
    for n in BURST:
        clean.client.patch(f"/api/shots/{shots[n]['id']}", json={"user_verdict": "keep"})
    resp = clean.client.post(f"/api/groups/{clean.event_id}/{shots[BURST[0]]['group_id']}/accept", headers=JSON)
    assert resp.json() == {"batch": None, "shots": []}
    assert {clean.shots()[n]["user_verdict"] for n in BURST} == {"keep"}


def test_group_accept_unknown_group(clean: Server) -> None:
    assert clean.client.post(f"/api/groups/{clean.event_id}/999/accept", headers=JSON).status_code == 404


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
                 "headers": [(b"host", b"127.0.0.1:8765")], "server": ("127.0.0.1", 8765), "client": ("testclient", 1)}
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
        stream = broker.stream(is_disconnected, lambda: ["event: job\ndata: {}\n\n"])
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


def test_snapshot_is_taken_after_subscribing() -> None:
    """An event published while the initial snapshot is read still reaches the client."""
    broker = Broker()

    async def is_disconnected() -> bool:
        return False

    def initial() -> list[str]:
        broker.publish("job", {"id": 1, "status": "done"})      # e.g. the job finishes right now
        return []

    async def run() -> str:
        return await asyncio.wait_for(anext(broker.stream(is_disconnected, initial)), 5)

    assert asyncio.run(run()) == 'event: job\ndata: {"id": 1, "status": "done"}\n\n'


# --------------------------------------------------------------------------- #
# Other web sites: CSRF and DNS rebinding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method, path, body", [
    ("POST", "/api/decisions/undo", None),
    ("POST", "/api/groups/{event}/{group}/accept", None),
    ("PATCH", "/api/shots/{shot}", {"user_verdict": "reject"}),
    ("POST", "/api/analyse", {"scope": "all"}),
])
def test_writes_from_other_sites_are_refused(clean: Server, method: str, path: str, body: Any) -> None:
    client = clean.client
    shot_id = clean.shots()["sharp.jpg"]["id"]
    client.patch(f"/api/shots/{shot_id}", json={"user_verdict": "keep"})
    url = path.format(event=clean.event_id, shot=shot_id, group=clean.shots()[BURST[0]]["group_id"])
    before = clean.shots()

    for origin in ("http://evil.example", "null", "http://127.0.0.1:9999", "https://127.0.0.1:8765"):
        resp = client.request(method, url, json=body, headers={**JSON, "Origin": origin})
        assert resp.status_code == 403, origin
    # what a cross-site form or no-cors fetch can send: no JSON content type
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x", None):
        headers = {"Origin": BASE, **({"Content-Type": ctype} if ctype else {})}
        assert client.request(method, url, content=b"{}", headers=headers).status_code == 415, ctype
    assert clean.shots() == before

    # the server's own page may write
    resp = client.request(method, url, json=body, headers={**JSON, "Origin": BASE})
    assert resp.status_code in (200, 202)
    if method != "POST" or "analyse" not in path:
        assert clean.shots() != before
    else:
        wait_for_job(client, resp.json()["job_id"])


@pytest.mark.parametrize("path", ["/api/library", f"/api/files/{EVENT.as_posix()}/sharp.jpg", "/"])
def test_foreign_host_is_refused(server: Server, path: str) -> None:
    """DNS rebinding: the attacker's name resolves to 127.0.0.1, but the Host header still names it."""
    for host in ("attacker.example", "attacker.example:8765", "127.0.0.1.attacker.example", ""):
        resp = server.client.get(path, headers={"Host": host})
        assert resp.status_code == 400 and b"\xff\xd8" not in resp.content[:2], host
    for host in ("127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765"):
        assert server.client.get(path, headers={"Host": host}).status_code == 200, host


def test_host_name() -> None:
    assert host_name("127.0.0.1:8765") == "127.0.0.1"
    assert host_name("[::1]:8765") == "[::1]"
    assert host_name("[::1]") == "[::1]"
    assert host_name("Example.COM") == "example.com"
    assert host_name("") == ""


def test_foreign_host_error_says_what_to_do(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / "2025").mkdir(parents=True)
    with TestClient(create_app(root, tmp_path / "k.sqlite", allowed_hosts=[*LOOPBACK_HOSTS, "kone.local"])) as client:
        assert client.get("/api/library").status_code == 400                     # Host: testserver
        assert client.get("/api/library", headers={"Host": "kone.local:8765"}).status_code == 200
        detail = client.get("/api/library", headers={"Host": "mypc:8765"}).json()["detail"]
    assert detail == ('tuntematon Host-otsake "mypc"; käytä osoitetta 127.0.0.1 tai [::1] tai kone.local tai '
                      "localhost tai käynnistä palvelin valitsimella --allow-host mypc")


def test_same_origin_bodyless_writes_need_no_content_type(clean: Server) -> None:
    """What Swagger UI at /docs sends for accept and undo: no body, no Content-Type, its own Origin."""
    client, shots = clean.client, clean.shots()
    group = shots[BURST[0]]["group_id"]
    resp = client.post(f"/api/groups/{clean.event_id}/{group}/accept", headers={"Origin": BASE})
    assert resp.status_code == 200 and len(resp.json()["shots"]) == 5
    assert client.post("/api/decisions/undo", headers={"Origin": BASE}).json()["batch"] == resp.json()["batch"]
    # without an Origin (curl, or an old browser posting a form) the content type is still required
    assert client.post("/api/decisions/undo").status_code == 415
    assert client.post("/api/decisions/undo", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/decisions/undo", headers={"Origin": BASE, "Content-Type": "text/plain"},
                       content=b"x").status_code == 415


def test_docs_are_off_by_default(server: Server, tmp_path: Path) -> None:
    assert server.client.get("/docs").status_code == 404
    assert server.client.get("/redoc").status_code == 404
    root = tmp_path / "lib"
    (root / "2025").mkdir(parents=True)
    with TestClient(create_app(root, tmp_path / "k.sqlite", docs=True), base_url=BASE) as client:
        assert client.get("/docs").status_code == 200 and "swagger" in client.get("/docs").text.lower()
        assert client.get("/redoc").status_code == 404


def test_files_are_not_sniffed(server: Server) -> None:
    resp = server.client.get(f"/api/files/{EVENT.as_posix()}/sharp.jpg")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"] == "image/jpeg" and resp.headers["content-disposition"].startswith("inline")
    raw = server.client.get(f"/api/files/{EVENT.as_posix()}/DSC_0100.NEF")
    assert raw.status_code == 200 and raw.headers["content-type"] == "application/octet-stream"
    assert raw.headers["x-content-type-options"] == "nosniff"


def test_repeated_analyse_returns_the_pending_job(own: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    collect = server_mod.collect_shots

    def slow_collect(folder: Path) -> list[tuple[str, list[str]]]:
        release.wait(30)
        return collect(folder)

    monkeypatch.setattr(server_mod, "collect_shots", slow_collect)
    try:
        first = own.client.post("/api/analyse", json={"scope": own.event_id})
        again = own.client.post("/api/analyse", json={"scope": own.event_id})
        other = own.client.post("/api/analyse", json={"scope": "all"})
        assert (first.status_code, again.status_code, other.status_code) == (202, 200, 202)
        assert again.json()["job_id"] == first.json()["job_id"] != other.json()["job_id"]
    finally:
        release.set()
    for resp in (first, other):
        assert wait_for_job(own.client, resp.json()["job_id"])["status"] == "done"


def test_decisions_get_through_while_an_analysis_scans(own: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job must not hold the write lock while it walks the library."""
    scanning, release = threading.Event(), threading.Event()
    collect = server_mod.collect_shots

    def blocking_collect(folder: Path) -> list[tuple[str, list[str]]]:
        scanning.set()
        release.wait(30)
        return collect(folder)

    monkeypatch.setattr(server_mod, "collect_shots", blocking_collect)
    monkeypatch.setattr(server_mod.Store, "__init__", _short_timeout(server_mod.Store.__init__))
    shot_id = own.shots()["sharp.jpg"]["id"]
    try:
        job_id = own.client.post("/api/analyse", json={"scope": "all"}).json()["job_id"]
        assert scanning.wait(30)
        resp = own.client.patch(f"/api/shots/{shot_id}", json={"user_verdict": "keep"})
        assert resp.status_code == 200
        assert own.client.get("/api/library").status_code == 200
    finally:
        release.set()
    assert wait_for_job(own.client, job_id)["status"] == "done"
    assert own.shots()["sharp.jpg"]["user_verdict"] == "keep"


def _short_timeout(init: Any) -> Any:
    """Store.__init__ with a 1 s lock timeout, so a held lock fails the test quickly instead of after 30 s."""
    def wrapped(self: Store, path: Path | str) -> None:
        init(self, path)
        self.db.execute("PRAGMA busy_timeout = 1000")
    return wrapped


def test_failed_commit_is_an_error(own: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    shot_id = own.shots()["sharp.jpg"]["id"]

    def fail(self: Store) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "commit", fail)
    client = TestClient(own.app, base_url=BASE, raise_server_exceptions=False)
    resp = client.patch(f"/api/shots/{shot_id}", json={"user_verdict": "keep"})
    assert resp.status_code == 500
    monkeypatch.undo()
    assert own.shots()["sharp.jpg"]["user_verdict"] is None


# --------------------------------------------------------------------------- #
# Previews
# --------------------------------------------------------------------------- #
def test_analysis_writes_previews(server: Server) -> None:
    cache: previews.PreviewCache = server.app.state.previews
    for name in ("sharp.jpg", "DSC_0100.NEF"):
        files = [str(server.root.resolve() / f) for f in server.shots()[name]["files"]]
        sig = Store.signature(files)
        assert all(previews.preview_path(cache.cache_dir, sig, size).is_file() for size in previews.SIZES), name


@pytest.mark.parametrize(("size", "dims"), [("grid", (400, 300)), ("full", (800, 600))])   # never scaled up
def test_preview_sizes(server: Server, size: str, dims: tuple[int, int]) -> None:
    url = server.shots()["sharp.jpg"]["preview"][size]
    resp = server.client.get(url)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(resp.content)).size == dims
    assert resp.headers["etag"].endswith(f'-{size}"')
    assert resp.headers["cache-control"] == f"private, max-age={server_mod.PREVIEW_MAX_AGE}, immutable"
    assert resp.headers["cross-origin-resource-policy"] == "same-origin"
    assert resp.headers["x-content-type-options"] == "nosniff"
    etag = resp.headers["etag"]
    for inm in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        again = server.client.get(url, headers={"If-None-Match": inm})
        assert again.status_code == 304 and again.headers["etag"] == etag and not again.content, inm
    assert server.client.get(url, headers={"If-None-Match": '"other"'}).status_code == 200


def test_preview_urls_are_versioned(server: Server) -> None:
    shot = server.shots()["sharp.jpg"]
    detail = server.client.get(f"/api/shots/{shot['id']}").json()
    assert detail["preview"] == shot["preview"] and "sig" not in shot
    assert set(shot["preview"]) == {"grid", "full"}
    grid = shot["preview"]["grid"]
    assert grid.startswith(f"/api/shots/{shot['id']}/preview?size=grid&v=")
    # without the version (or with another one) the browser must ask again every time
    for url in (f"/api/shots/{shot['id']}/preview", grid.replace("&v=", "&v=0")):
        resp = server.client.get(url)
        assert resp.status_code == 200 and resp.headers["cache-control"] == "private, no-cache"
        assert resp.headers["etag"] == server.client.get(grid).headers["etag"]


def test_preview_default_size_and_errors(server: Server) -> None:
    shot_id = server.shots()["sharp.jpg"]["id"]
    resp = server.client.get(f"/api/shots/{shot_id}/preview")
    assert Image.open(io.BytesIO(resp.content)).size == (400, 300)
    assert server.client.get(f"/api/shots/{shot_id}/preview", params={"size": "huge"}).status_code == 422
    assert server.client.get("/api/shots/99999/preview").status_code == 404


def test_preview_is_cached_until_the_file_changes(own: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []
    make = previews.make
    monkeypatch.setattr(previews, "make", lambda *a: made.append(a[-1]) or make(*a))
    shot_id = own.shots()["sharp.jpg"]["id"]
    url = f"/api/shots/{shot_id}/preview"
    first = own.client.get(url)
    assert own.client.get(url).content == first.content
    assert made == []                                             # made by the analysis already

    src = own.root / EVENT / "sharp.jpg"
    save_jpeg(src, texture(8), T0)                                # another picture
    st = src.stat()
    os.utime(src, (st.st_atime, st.st_mtime + 10))
    changed = own.client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200 and changed.headers["etag"] != first.headers["etag"]
    old_url = own.shots()["sharp.jpg"]["preview"]["grid"]        # not analysed again: the old version
    assert own.client.get(old_url).headers["cache-control"] == "private, no-cache"
    assert changed.content != first.content and made == ["grid"]
    assert own.client.get(url).content == changed.content and made == ["grid"]

    src.unlink()
    assert own.client.get(url).status_code == 404


def test_broken_image_preview_is_an_error(own: Server) -> None:
    shot_id = own.shots()["sharp.jpg"]["id"]
    (own.root / EVENT / "sharp.jpg").write_bytes(b"not an image")
    resp = own.client.get(f"/api/shots/{shot_id}/preview")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "kuvaa ei voi lukea: tiedosto on rikki tai sen muotoa ei tueta"


@pytest.mark.parametrize(("error", "status", "detail"), [
    (RawpyMissing("rawpy not installed, cannot read RAW"), 500, "RAW-kuvan esikatselu vaatii rawpy-paketin"),
    (previews.NoAccess("[Errno 13] Permission denied"), 500, "kuvaa ei voi lukea: ei lukuoikeutta tiedostoon"),
    (PermissionError(13, "Permission denied"), 500, "esikatselua ei voi tallentaa välimuistiin: Permission denied"),
])
def test_preview_errors(own: Server, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int,
                        detail: str) -> None:
    def fail(*args: Any) -> None:
        raise error

    monkeypatch.setattr(previews, "make", fail)
    shot_id = own.shots()["sharp.jpg"]["id"]
    src = own.root / EVENT / "sharp.jpg"
    os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 10))   # not in the cache any more
    resp = own.client.get(f"/api/shots/{shot_id}/preview")
    assert (resp.status_code, resp.json()["detail"]) == (status, detail)


def test_etag_matches() -> None:
    assert etag_matches('"a-grid"', '"a-grid"') and etag_matches('W/"a-grid"', '"a-grid"')
    assert etag_matches('"b", "a-grid"', '"a-grid"') and etag_matches("*", '"a-grid"')
    assert not etag_matches("", '"a-grid"') and not etag_matches('"a-full"', '"a-grid"')


# --------------------------------------------------------------------------- #
# Jobs on a library of their own
# --------------------------------------------------------------------------- #
@pytest.fixture
def own(library: Library, tmp_path: Path) -> Iterator[Server]:
    """A fresh copy of the synthetic library, analysed once; the tests may change its files."""
    root = tmp_path / "lib"
    shutil.copytree(library.root, root)
    db = tmp_path / "karsija.sqlite"
    app = create_app(root, db, workers=2)
    with TestClient(app, base_url=BASE) as client:
        event_id = client.get("/api/library").json()["years"][0]["events"][0]["id"]
        job = wait_for_job(client, client.post("/api/analyse", json={"scope": event_id}).json()["job_id"])
        assert job["status"] == "done" and job["total"] == 10
        yield Server(client, app, root, db, event_id, [], job)


def analyse(srv: Server, scope: Any = None) -> dict[str, Any]:
    resp = srv.client.post("/api/analyse", json={"scope": srv.event_id if scope is None else scope})
    return wait_for_job(srv.client, resp.json()["job_id"])


def test_missing_shot_leaves_the_event(own: Server) -> None:
    shots = own.shots()
    gone = next(n for n in BURST if shots[n]["auto_verdict"] == "similar")
    own.client.patch(f"/api/shots/{shots[gone]['id']}", json={"user_verdict": "keep", "rating": 2})
    moved_away = own.root.parent / gone
    (own.root / EVENT / gone).rename(moved_away)

    assert analyse(own)["status"] == "done"
    shots = own.shots()
    assert gone not in shots and len(shots) == 9
    ev = own.client.get(f"/api/events/{own.event_id}").json()["event"]
    assert (ev["shots"], ev["candidates"], ev["reviewed"]) == (9, 5, 0)
    group = shots[BURST[0] if gone != BURST[0] else BURST[1]]["group_id"]
    accepted = own.client.post(f"/api/groups/{own.event_id}/{group}/accept", headers=JSON).json()["shots"]
    assert gone not in {s["name"] for s in accepted} and len(accepted) == 4

    # back again: present, judged, the decision kept
    moved_away.rename(own.root / EVENT / gone)
    assert analyse(own)["total"] == 0                           # unchanged file: still a cache hit
    back = own.shots()[gone]
    assert (back["state"], back["user_verdict"], back["rating"]) == ("present", "keep", 2)
    assert back["auto_verdict"] in ("best", "similar") and back["group_id"] == group


def test_library_shows_which_events_are_analysed(own: Server) -> None:
    (own.root / "2025" / "2025-08-01-empty").mkdir()
    events = {ev["name"]: ev for ev in own.client.get("/api/library").json()["years"][0]["events"]}
    assert events[EVENT.name]["analysed_at"] == own.job["finished"]
    assert events["2025-08-01-empty"]["analysed_at"] is None
    assert events["2025-08-01-empty"]["shots"] == 0

    job = analyse(own, "all")
    events = {ev["name"]: ev for ev in own.client.get("/api/library").json()["years"][0]["events"]}
    assert events[EVENT.name]["analysed_at"] == events["2025-08-01-empty"]["analysed_at"] == job["finished"]


def test_next_analysis_uses_stored_settings(own: Server) -> None:
    store = Store(own.db)
    store.set_settings({"gap": 0.1})                              # burst frames are 0.3 s apart
    store.commit()
    store.close()
    job = analyse(own)
    assert job["status"] == "done" and job["total"] == 0
    data = own.client.get(f"/api/events/{own.event_id}").json()
    assert data["settings"]["gap"] == 0.1
    assert len(data["groups"]) == 10
    assert {s["auto_verdict"] for g in data["groups"] for s in g["shots"] if s["name"] in BURST} == {"keep"}


def crash(*args: Any) -> None:
    os._exit(1)                                                   # like an OOM kill or a segfault in a codec


def test_broken_pool_is_replaced(own: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    runner: JobRunner = own.app.state.runner
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(runner.broker, "publish", lambda event, data: published.append(data))
    (own.root / EVENT / "sharp.jpg").touch()                     # new mtime: analysed again
    monkeypatch.setattr(server_mod, "analyse_file", crash)
    job = analyse(own)
    assert (job["status"], job["error"]) == ("failed", "analyysiprosessi kaatui; yritä uudelleen")
    assert published[-1]["status"] == "failed" and published[-1]["error"] == job["error"]
    assert runner._pool is None

    monkeypatch.undo()
    job = analyse(own)
    assert job["status"] == "done" and job["total"] == 1


def test_job_for_a_deleted_event_fails_clearly(own: Server) -> None:
    store = Store(own.db)
    job_id = store.create_job("analyse", "999")
    store.commit()
    own.app.state.runner.run(job_id, "999")
    job = store.job(job_id)
    store.close()
    assert job is not None and (job["status"], job["error"]) == ("failed", "tapahtumaa ei ole")
