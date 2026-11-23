"""Review server: JSON API under ``/api``, background analysis, progress over SSE, static UI from the package.

All library file access goes through ``library.safe_path``: nothing outside the library root is read or served.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
import multiprocessing
import queue
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator, Sequence
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from karsija.analysis import Shot, analyse_file, group_and_judge
from karsija.library import JPEG_EXT, RAW_EXT, UnsafePath, collect_shots, find_event_dirs, safe_path
from karsija.store import UNSET, Store

STATIC_DIR = Path(__file__).parent / "static"
KEEPALIVE = 15.0              # seconds between SSE comments on an idle stream
SUGGESTION = {"best": "keep", "keep": "keep", "similar": "reject", "blurry": "reject"}   # auto verdict -> decision

_SHOT_COLS = ("id, event_id, key, files, taken, sharp, sharp_global, clip_hi, clip_lo, error, group_id, "
              "auto_verdict, reason, user_verdict, rating, decided_at, state")


# --------------------------------------------------------------------------- #
# Progress events
# --------------------------------------------------------------------------- #
class Broker:
    """Fan-out of server-sent events to every connected client. ``publish`` may be called from any thread."""

    def __init__(self) -> None:
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = set()
        self._lock = threading.Lock()

    def publish(self, event: str, data: Any) -> None:
        msg = sse(event, data)
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, msg)
            except RuntimeError:          # the client's loop is gone
                pass

    async def stream(self, is_disconnected: Callable[[], Awaitable[bool]],
                     initial: Callable[[], Sequence[str]] = tuple) -> AsyncIterator[str]:
        """SSE body: the messages of ``initial()``, then every published event, with keep-alive comments when idle.

        ``initial`` runs after the subscription is in place, so an event published meanwhile is not lost (at worst
        the client gets it twice).
        """
        sub = (asyncio.get_running_loop(), asyncio.Queue[str]())
        with self._lock:
            self._subs.add(sub)
        try:
            for msg in await asyncio.to_thread(initial):
                yield msg
            while not await is_disconnected():
                try:
                    yield await asyncio.wait_for(sub[1].get(), KEEPALIVE)
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with self._lock:
                self._subs.discard(sub)


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --------------------------------------------------------------------------- #
# Background analysis
# --------------------------------------------------------------------------- #
def sync_events(store: Store, root: Path) -> list[tuple[int, Path]]:
    """Register the event folders on disk; returns ``(event_id, folder)``. Folders that resolve outside the root
    (symlinks) are skipped."""
    out = []
    for ev in find_event_dirs(root):
        rel = ev.relative_to(root).as_posix()
        try:
            path = safe_path(root, rel)
        except UnsafePath:
            continue
        out.append((store.event_id(rel), path))
    return out


def job_error(e: Exception) -> str:
    """Message of a failed job: plain Finnish for the expected failures, the exception otherwise."""
    if isinstance(e, BrokenProcessPool):
        return "analyysiprosessi kaatui; yritä uudelleen"
    if isinstance(e, UnsafePath):
        return "tapahtuman kansio ei ole kirjastossa"
    if isinstance(e, LookupError):
        return str(e.args[0]) if e.args else "ei löydy"
    return f"{type(e).__name__}: {e}"


class JobRunner:
    """Runs analysis jobs one at a time in a thread; the image work goes to a process pool."""

    def __init__(self, root: Path, db_path: Path, workers: int, broker: Broker):
        self.root, self.db_path, self.workers, self.broker = root, db_path, workers, broker
        self._queue: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._pool: cf.ProcessPoolExecutor | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="karsija-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def submit(self, job_id: int, scope: str) -> None:
        self._queue.put((job_id, scope))

    def _loop(self) -> None:
        while (item := self._queue.get()) is not None:
            self.run(*item)

    def _publish(self, store: Store, job_id: int, **values: Any) -> None:
        store.update_job(job_id, **values)
        store.commit()
        self.broker.publish("job", store.job(job_id))

    def run(self, job_id: int, scope: str) -> None:
        store = Store(self.db_path)
        futs: dict[cf.Future[Shot], tuple[int, str]] = {}
        try:
            self._publish(store, job_id, status="running", started=time.time())
            if scope == "all":
                events = sync_events(store, self.root)
                store.commit()          # never hold the write lock across the scan: decisions must get through
            else:
                row = store.db.execute("SELECT rel_path FROM events WHERE id = ?", (int(scope),)).fetchone()
                if row is None:
                    raise LookupError("tapahtumaa ei ole")
                events = [(int(scope), safe_path(self.root, row[0]))]

            todo: list[tuple[int, str, list[str], str]] = []
            on_disk: dict[int, set[str]] = {}
            for event_id, folder in events:
                on_disk[event_id] = set()
                if not folder.is_dir():
                    continue
                for primary, files in collect_shots(folder):
                    if not self._inside_root(files):
                        continue
                    on_disk[event_id].add(primary)
                    sig = Store.signature(files)
                    if store.get(primary, sig) is None:       # cache hit by signature skips the work
                        todo.append((event_id, primary, files, sig))
                    elif store.db.in_transaction:
                        store.commit()                        # get() stored the EXIF of an old row
                store.mark_missing(event_id, on_disk[event_id])
                store.commit()
            self._publish(store, job_id, total=len(todo))

            if todo:
                pool = self._get_pool()
                futs.update({pool.submit(analyse_file, p, f): (eid, sig) for eid, p, f, sig in todo})
                for done, fut in enumerate(cf.as_completed(futs), 1):
                    eid, sig = futs[fut]
                    store.put(eid, fut.result(), sig)
                    self._publish(store, job_id, done=done)

            settings = store.settings()
            for event_id, keys in on_disk.items():
                shots = store.event_shots(event_id, keys)
                group_and_judge(shots, settings)
                store.save_verdicts(shots)
            self._publish(store, job_id, status="done", finished=time.time())
        except Exception as e:  # noqa: BLE001
            for f in futs:
                f.cancel()
            if isinstance(e, BrokenProcessPool) and self._pool is not None:
                # a worker died (killed, crashed in a native library): the pool is unusable, start a new one next time
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            store.db.rollback()
            self._publish(store, job_id, status="failed", error=job_error(e), finished=time.time())
        finally:
            store.close()

    def _inside_root(self, files: list[str]) -> bool:
        try:
            for f in files:
                safe_path(self.root, Path(f).relative_to(self.root).as_posix())
        except (UnsafePath, ValueError):
            return False
        return True

    def _get_pool(self) -> cf.ProcessPoolExecutor:
        if self._pool is None:
            # spawn: the server process has threads, forking it is unsafe
            self._pool = cf.ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"))
        return self._pool


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class AnalyseRequest(BaseModel):
    scope: Literal["all"] | int = "all"


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_verdict: Literal["keep", "reject"] | None = None
    rating: int | None = Field(default=None, ge=0, le=5)


# --------------------------------------------------------------------------- #
# Request guard
# --------------------------------------------------------------------------- #
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def host_name(host: str) -> str:
    """``Host`` header without the port: ``127.0.0.1:8765`` -> ``127.0.0.1``, ``[::1]:8765`` -> ``[::1]``."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":")[0]


class RequestGuard:
    """Keeps other web sites out, although the browser can reach the server.

    - ``Host`` must be one of ``allowed_hosts``: a DNS-rebinding page (``Host: attacker.example``) cannot read
      the library.
    - Writes (POST, PUT, PATCH, DELETE): an ``Origin`` header, when present, must be the server's own, and the
      request must be ``Content-Type: application/json``, which a cross-site form or ``no-cors`` fetch cannot send
      without a CORS preflight (the server allows no cross-origin requests). A write without a body may leave the
      content type out when it comes from the server's own origin (Swagger UI at ``/docs`` does so for
      ``.../accept`` and ``.../undo``): browsers always send ``Origin`` with a POST from another site.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Collection[str]):
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        response: Response | None = None
        if host_name(host) not in self.allowed_hosts:
            name = host_name(host)
            response = JSONResponse({"detail": f'tuntematon Host-otsake "{name}"; käytä osoitetta '
                                               f'{" tai ".join(sorted(self.allowed_hosts))} tai käynnistä '
                                               f'palvelin valitsimella --allow-host {name or "NIMI"}'}, 400)
        elif scope["method"] in WRITE_METHODS:
            origin = headers.get("origin")
            own = origin is not None and origin.lower() == f"{scope['scheme']}://{host}".lower()
            bodyless = headers.get("content-length", "0") == "0" and "transfer-encoding" not in headers
            if origin is not None and not own:
                response = JSONResponse({"detail": "pyyntö toiselta sivustolta"}, 403)
            elif headers.get("content-type", "").split(";")[0].strip().lower() != "application/json" \
                    and not (own and bodyless):
                response = JSONResponse({"detail": "vaatii Content-Type: application/json"}, 415)
        if response is not None:
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def get_store(request: Request) -> Iterator[Store]:
    """One connection per request, committed when the handler succeeds and before the response is sent (the
    dependency has function scope), so a failed commit is a 500, not a success that was not stored."""
    store = Store(request.app.state.db_path)
    try:
        yield store
        store.commit()
    finally:
        store.close()


Db = Annotated[Store, Depends(get_store, scope="function")]


def create_app(root: Path, db_path: Path, workers: int = 2, on_ready: Callable[[], None] | None = None,
               allowed_hosts: Collection[str] = LOOPBACK_HOSTS, docs: bool = False) -> FastAPI:
    """``allowed_hosts``: accepted ``Host`` header names, see ``RequestGuard``.

    ``docs`` turns on Swagger UI at ``/docs``. It loads its scripts from cdn.jsdelivr.net into the server's origin,
    so it is off unless asked for. ReDoc is always off.
    """
    root = root.resolve()
    broker = Broker()
    runner = JobRunner(root, db_path, workers, broker)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(db_path)
        store.fail_unfinished_jobs()
        store.commit()
        store.close()
        runner.start()
        if on_ready:
            on_ready()
        yield
        runner.stop()

    app = FastAPI(title="karsija", lifespan=lifespan, docs_url="/docs" if docs else None, redoc_url=None)
    app.add_middleware(RequestGuard, allowed_hosts=allowed_hosts)
    app.state.root, app.state.db_path, app.state.runner, app.state.broker = root, db_path, runner, broker

    def rel(path: str) -> str:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            return Path(path).name

    def shot_json(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        key = d.pop("key")
        d["name"], d["path"] = Path(key).name, rel(key)
        d["files"] = [rel(f) for f in json.loads(d["files"])]
        if "exif" in d:
            d["exif"] = json.loads(d["exif"]) if d["exif"] else {}
        return d

    def query(store: Store, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        cur = store.db.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, params).fetchall()

    def shots_by_id(store: Store, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = query(store, f"SELECT {_SHOT_COLS} FROM shots WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY id",
                     tuple(ids))
        return [shot_json(r) for r in rows]

    def event_summaries(store: Store, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = query(store, f"""
            SELECT e.id, e.rel_path, e.year, e.name,
                   COUNT(s.id) AS shots,
                   COALESCE(SUM(s.auto_verdict IN ('blurry', 'similar')), 0) AS candidates,
                   COALESCE(SUM(s.user_verdict IS NOT NULL), 0) AS reviewed
            FROM events e LEFT JOIN shots s ON s.event_id = e.id AND s.state = 'present'
            WHERE e.id IN ({', '.join('?' * len(ids))})
            GROUP BY e.id ORDER BY e.rel_path DESC""", tuple(ids))
        return [{**dict(r), "unreviewed": r["shots"] - r["reviewed"]} for r in rows]

    # ----------------------------------------------------------------------- #
    @app.get("/api/library")
    def library(store: Db) -> dict[str, Any]:
        # registers new event folders as a side effect, so the list matches the disk without running an analysis
        events = event_summaries(store, [eid for eid, _ in sync_events(store, root)])
        years: dict[int, list[dict[str, Any]]] = {}
        for ev in events:
            years.setdefault(ev["year"], []).append(ev)
        return {"root": str(root), "years": [{"year": y, "events": evs} for y, evs in years.items()]}

    @app.post("/api/analyse", status_code=202)
    def analyse(body: AnalyseRequest, store: Db, response: Response) -> dict[str, Any]:
        """Start an analysis (202). If one with the same scope is already queued or running, return it (200)."""
        if body.scope != "all" and store.db.execute("SELECT 1 FROM events WHERE id = ?", (body.scope,)).fetchone() \
                is None:
            raise HTTPException(404, "tapahtumaa ei ole")
        pending = store.db.execute(
            "SELECT id FROM jobs WHERE kind = 'analyse' AND scope = ? AND status IN ('queued', 'running') "
            "ORDER BY id LIMIT 1", (str(body.scope),)).fetchone()
        if pending:
            response.status_code = 200
            return {"job_id": pending[0], "job": store.job(pending[0])}
        job_id = store.create_job("analyse", str(body.scope))
        store.commit()
        runner.submit(job_id, str(body.scope))
        return {"job_id": job_id, "job": store.job(job_id)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int, store: Db) -> dict[str, Any]:
        found = store.job(job_id)
        if found is None:
            raise HTTPException(404, "työtä ei ole")
        return found

    # declared before /api/events/{event_id}
    @app.get("/api/events/stream")
    async def events_stream(request: Request) -> StreamingResponse:
        def initial() -> list[str]:
            """Queued and running jobs, so a new client sees progress at once."""
            store = Store(db_path)
            try:
                ids = [r[0] for r in store.db.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running')")]
                return [sse("job", store.job(i)) for i in ids]
            finally:
                store.close()

        return StreamingResponse(broker.stream(request.is_disconnected, initial), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/events/{event_id}")
    def event(event_id: int, store: Db) -> dict[str, Any]:
        summary = event_summaries(store, [event_id])
        if not summary:
            raise HTTPException(404, "tapahtumaa ei ole")
        rows = query(store, f"SELECT {_SHOT_COLS} FROM shots WHERE event_id = ? AND state = 'present' "
                            "ORDER BY taken, key", (event_id,))
        groups: dict[int | None, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r["group_id"], []).append(shot_json(r))
        sharp = sorted(r["sharp"] for r in rows if not r["error"])
        return {
            "event": summary[0],
            "settings": store.settings(),
            "median_sharp": sharp[len(sharp) // 2] if sharp else None,
            "sharpness": sharp,                 # histogram data
            "groups": [{"group_id": gid, "shots": shots} for gid, shots in groups.items()],
        }

    @app.get("/api/shots/{shot_id}")
    def shot(shot_id: int, store: Db) -> dict[str, Any]:
        rows = query(store, f"SELECT {_SHOT_COLS}, exif, dhash FROM shots WHERE id = ?", (shot_id,))
        if not rows:
            raise HTTPException(404, "kuvaa ei ole")
        return shot_json(rows[0])

    @app.patch("/api/shots/{shot_id}")
    def decide(shot_id: int, body: DecisionRequest, store: Db) -> dict[str, Any]:
        if not body.model_fields_set:
            raise HTTPException(422, "anna user_verdict tai rating")
        if store.db.execute("SELECT 1 FROM shots WHERE id = ?", (shot_id,)).fetchone() is None:
            raise HTTPException(404, "kuvaa ei ole")
        fields = body.model_fields_set
        batch = store.decide([(shot_id,
                               body.user_verdict if "user_verdict" in fields else UNSET,
                               body.rating if "rating" in fields else UNSET)])
        return {"batch": batch, "shots": shots_by_id(store, [shot_id])}

    @app.post("/api/groups/{event_id}/{group_id}/accept")
    def accept_group(event_id: int, group_id: int, store: Db) -> dict[str, Any]:
        """Accept the suggested keep/reject for the shots of the group that have no decision yet.

        Shots already decided keep their decision. ``batch`` is null when nothing was left to decide.
        """
        rows = store.db.execute(
            "SELECT id, auto_verdict, user_verdict FROM shots "
            "WHERE event_id = ? AND group_id = ? AND state = 'present'",
            (event_id, group_id),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "ryhmää ei ole")
        changes = [(sid, SUGGESTION[v], UNSET) for sid, v, decided in rows if v in SUGGESTION and decided is None]
        if not changes:
            return {"batch": None, "shots": []}
        batch = store.decide(changes)
        return {"batch": batch, "shots": shots_by_id(store, [c[0] for c in changes])}

    @app.post("/api/decisions/undo")
    def undo(store: Db) -> dict[str, Any]:
        batch, ids = store.undo()
        return {"batch": batch, "shots": shots_by_id(store, ids)}

    @app.get("/api/settings")
    def settings(store: Db) -> dict[str, float]:
        return store.settings()

    @app.get("/api/files/{path:path}")
    def file(path: str) -> FileResponse:
        """An original image file of the library."""
        try:
            p = safe_path(root, path)
        except UnsafePath:
            raise HTTPException(403, "polku ei ole kirjastossa") from None
        if p.suffix.lower() not in JPEG_EXT | RAW_EXT or not p.is_file():
            raise HTTPException(404, "tiedostoa ei ole")
        media_type = "application/octet-stream" if p.suffix.lower() in RAW_EXT else None
        return FileResponse(p, media_type=media_type, filename=p.name, content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff"})

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
