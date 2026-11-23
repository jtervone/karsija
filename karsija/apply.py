"""Toteuta and Palauta karsitut: carry out an event's decisions in the library, and reverse that.

An apply moves the shots the user rejected into ``<event>/_karsitut/``, each with its RAW, its JPEG and the sidecars
of both styles (``name.xmp``, ``name.ext.xmp``), and writes the rating and label of every reviewed shot (one with a
keep or reject) into its sidecars (``actions.xmp_changes``). Shots without a decision are never touched. Nothing is
overwritten: a destination that exists, or a sidecar that cannot be merged, is a conflict, and the apply refuses to
start.

Every operation is logged before anything changes, in ``applies``/``apply_ops`` (with each sidecar's previous
content) and in ``<event>/_karsitut/karsija-apply-<id>.json``. Restore does not trust what the log says was done: it
checks each operation against the files, so an apply cut short (a crash, a full disk) can be reversed too, and a
restore cut short can be run again. Restore deletes nothing: the sidecars an apply created, a sidecar changed since
the apply and the JSON log go into ``undone/<id>/`` of the cache folder.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karsija.actions import REJECT_DIR, UserValuesKept, XmpError, write_atomic, xmp_changes, xmp_values
from karsija.library import UnsafePath, inside_root, safe_path
from karsija.store import Store

INTERRUPTED = "keskeytyi: palvelin pysäytettiin kesken toteutuksen; palauta se"


class ApplyConflict(Exception):
    """The apply or restore cannot be done as asked; nothing was changed."""


class ApplyFailed(Exception):
    """An apply stopped part way; its log is complete, so it can be restored."""

    def __init__(self, apply_id: int, message: str) -> None:
        super().__init__(message)
        self.apply_id = apply_id


def default_undone_dir() -> Path:
    """``$XDG_CACHE_HOME/karsija/undone``, by default ``~/.cache/karsija/undone``."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "karsija" / "undone"


@dataclass
class Op:
    kind: str  # xmp_create, xmp_update or move
    shot_id: int
    src: Path  # the sidecar written, or the file moved
    dst: Path | None = None  # move: where to
    backup: str | None = None  # xmp_update: the content before
    new: str | None = None  # xmp ops: the content written

    @property
    def new_sha256(self) -> str | None:
        return None if self.new is None else sha256(self.new)


@dataclass
class Plan:
    event_id: int
    folder: Path
    ops: list[Op] = field(default_factory=list)
    unreviewed: int = 0
    user_values: list[Path] = field(default_factory=list)  # sidecars with the user's own rating or label
    conflicts: list[tuple[Path, str]] = field(default_factory=list)
    token: str = ""

    def moved_shots(self) -> list[int]:
        return list(dict.fromkeys(op.shot_id for op in self.ops if op.kind == "move"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def build_plan(store: Store, root: Path, event_id: int) -> Plan:
    """What an apply of the event would do now. Raises LookupError when there is no such event."""
    row = store.db.execute("SELECT rel_path FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise LookupError("tapahtumaa ei ole")
    try:
        folder = safe_path(root, row[0])
    except UnsafePath:
        raise LookupError("tapahtuman kansio ei ole kirjastossa") from None
    plan = Plan(event_id, folder)
    if problem := reject_dir_problem(folder / REJECT_DIR):
        plan.conflicts.append((folder / REJECT_DIR, problem))
    rows = store.db.execute(
        "SELECT id, key, files, auto_verdict, user_verdict, rating FROM shots "
        "WHERE event_id = ? AND state = 'present' ORDER BY key",
        (event_id,),
    ).fetchall()
    for shot_id, key, files_json, auto, verdict, rating in rows:
        if verdict is None:
            plan.unreviewed += 1
            continue
        _plan_shot(plan, root, shot_id, key, json.loads(files_json), auto, verdict, rating)
    plan.token = _token(plan)
    return plan


def reject_dir_problem(reject_dir: Path) -> str | None:
    """Why the event's reject folder cannot take the rejects, or None. A symlink could lead out of the library."""
    if reject_dir.is_symlink():
        return "on symbolinen linkki; karsija siirtää kuvat vain tavalliseen kansioon, poista linkki ensin"
    if os.path.lexists(reject_dir) and not reject_dir.is_dir():
        return "on olemassa, mutta ei ole kansio; siirrä tai nimeä se ensin"
    return None


def _plan_shot(
    plan: Plan, root: Path, shot_id: int, key: str, files: list[str], auto: str | None, verdict: str, rating: int | None
) -> None:
    if not inside_root(root, [key, *files]):
        plan.conflicts.append((Path(key), "polku ei ole kirjastossa"))
        return
    if missing := [f for f in files if not os.path.lexists(f)]:
        plan.conflicts.extend((Path(f), "tiedostoa ei ole; analysoi tapahtuma uudelleen") for f in missing)
        return
    values = xmp_values(auto or "", verdict, rating)
    assert values is not None  # a user decision always gives values
    try:
        # a keep without stars of the user's own must not replace a rating the user gave in another program
        changes = xmp_changes(key, *values, files=files, keep_user_values=verdict == "keep" and rating is None)
    except UserValuesKept as e:
        plan.user_values.append(e.sidecar)
        changes = {}
    except XmpError as e:
        plan.conflicts.append((Path(key), f"sivutiedosto ei ole kelvollista XMP:tä: {e}"))
        return
    except OSError as e:
        why = "kirjoitussuojattu" if isinstance(e, PermissionError) else f"ei voi lukea ({e.strerror or e})"
        plan.conflicts.append((Path(e.filename or key), f"sivutiedosto on {why}; korjaa se ensin"))
        return
    for sidecar in changes:
        if not inside_root(root, [str(sidecar)]):
            plan.conflicts.append((sidecar, "polku ei ole kirjastossa"))
            return
    for sidecar, (prev, new) in changes.items():
        plan.ops.append(Op("xmp_create" if prev is None else "xmp_update", shot_id, sidecar, backup=prev, new=new))
    if verdict != "reject":
        return
    for src in shot_files(files, created=changes):
        dst = src.parent / REJECT_DIR / src.name
        if not inside_root(root, [str(dst.parent)]):
            plan.conflicts.append((dst, "polku ei ole kirjastossa"))
        elif os.path.lexists(dst):
            plan.conflicts.append((dst, "on jo olemassa; siirrä tai nimeä se ensin"))
        plan.ops.append(Op("move", shot_id, src, dst))


def shot_files(files: list[str], created: Any = ()) -> list[Path]:
    """The files that move with a shot: its images and the sidecars of both styles that exist (or are in
    ``created``)."""
    out: list[Path] = []
    for f in map(Path, files):
        out += [f, f.with_name(f.name + ".xmp"), f.with_suffix(".xmp")]
    return [p for p in dict.fromkeys(out) if os.path.lexists(p) or p in created]


def _token(plan: Plan) -> str:
    """Binds a confirmation to the plan: any change in what would be done, or in the files moved, gives another."""

    def stat(p: Path) -> list[int] | None:
        try:
            st = os.lstat(p)
        except OSError:
            return None
        return [st.st_size, st.st_mtime_ns]

    data = [
        plan.event_id,
        plan.unreviewed,
        [str(p) for p in plan.user_values],
        [[str(p), r] for p, r in plan.conflicts],
        [
            [
                op.kind,
                op.shot_id,
                str(op.src),
                str(op.dst),
                op.backup and sha256(op.backup),
                op.new_sha256,
                stat(op.src) if op.kind == "move" else None,
            ]
            for op in plan.ops
        ],
    ]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:32]


def plan_json(plan: Plan, root: Path) -> dict[str, Any]:
    rel = _rel(root)
    moves: dict[int, list[str]] = {}
    for op in plan.ops:
        if op.kind == "move":
            moves.setdefault(op.shot_id, []).append(rel(op.src))
    return {
        "event_id": plan.event_id,
        "reject_dir": rel(plan.folder / REJECT_DIR),
        "move": [{"shot_id": sid, "files": files} for sid, files in moves.items()],
        "xmp_create": [rel(op.src) for op in plan.ops if op.kind == "xmp_create"],
        "xmp_update": [rel(op.src) for op in plan.ops if op.kind == "xmp_update"],
        "user_values": [rel(p) for p in plan.user_values],
        "unreviewed": plan.unreviewed,
        "conflicts": [{"path": rel(p), "reason": reason} for p, reason in plan.conflicts],
        "confirm_token": plan.token,
    }


def _rel(root: Path) -> Callable[[Path | str], str]:
    def rel(p: Path | str) -> str:
        try:
            return Path(p).relative_to(root).as_posix()
        except ValueError:
            return str(p)

    return rel


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply(store: Store, root: Path, plan: Plan) -> int:
    """Carry out a plan without conflicts. Returns the apply's id.

    The log is stored before anything changes. Raises ApplyFailed when an operation fails part way (the apply is
    then ``failed`` and can be restored).
    """
    if plan.conflicts:
        raise ApplyConflict("suunnitelmassa on ristiriitoja")
    if not plan.ops:
        raise ApplyConflict("ei toteutettavaa: tapahtumassa ei ole hylättyjä kuvia eikä kirjoitettavia XMP:itä")
    reject_dir = plan.folder / REJECT_DIR
    if problem := reject_dir_problem(reject_dir):  # made since the plan: the token does not cover it
        raise ApplyConflict(f"{reject_dir}: {problem}")
    created_dir = not reject_dir.exists()
    cur = store.db.execute(
        "INSERT INTO applies (event_id, status, created_dir, started) VALUES (?, 'running', ?, ?)",
        (plan.event_id, int(created_dir), time.time()),
    )
    apply_id = int(cur.lastrowid or 0)
    log = reject_dir / f"karsija-apply-{apply_id}.json"
    if os.path.lexists(log):  # e.g. a symlink: the log would be written where it points
        store.db.rollback()
        raise ApplyConflict(f"{log} on jo olemassa; siirrä tai nimeä se ensin")
    store.db.execute("UPDATE applies SET log = ? WHERE id = ?", (str(log), apply_id))
    store.db.executemany(
        "INSERT INTO apply_ops (apply_id, seq, kind, shot_id, src, dst, backup, new_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (apply_id, seq, op.kind, op.shot_id, str(op.src), op.dst and str(op.dst), op.backup, op.new_sha256)
            for seq, op in enumerate(plan.ops)
        ],
    )
    store.commit()

    done = 0
    try:
        reject_dir.mkdir(exist_ok=True)
        write_log(store, root, apply_id)
        for seq, op in enumerate(plan.ops):
            _do(op)
            # stored as each one is done: restore trusts it, except for the one a crash may have cut short
            store.db.execute("UPDATE apply_ops SET done = 1 WHERE apply_id = ? AND seq = ?", (apply_id, seq))
            if op.kind == "move":
                store.db.execute("UPDATE shots SET state = 'moved' WHERE id = ?", (op.shot_id,))
            store.commit()
            done += 1
    except (OSError, ApplyConflict) as e:
        error = f"{e.strerror}: {e.filename}" if isinstance(e, OSError) and e.strerror and e.filename else str(e)
        _finish(store, root, apply_id, plan, done, "failed", error)
        raise ApplyFailed(apply_id, error) from e
    except Exception as e:
        # anything else (the database locked for too long): the apply must not stay "running" until a restart
        store.db.rollback()
        _finish(store, root, apply_id, plan, done, "failed", f"{type(e).__name__}: {e}")
        raise
    _finish(store, root, apply_id, plan, done, "done", "")
    return apply_id


def _do(op: Op) -> None:
    if op.kind == "move":
        assert op.dst is not None
        _move(op.src, op.dst)
        return
    assert op.new is not None
    # the sidecar must still be what the plan merged into: a change made meanwhile (another program) is not lost
    now = _current(op.src)
    if now != op.backup:
        raise ApplyConflict(f"{op.src} muuttui kesken toteutuksen")
    write_atomic(op.src, op.new)


def _current(path: Path) -> str | None:
    """A sidecar's content as it is now, None if there is none: compared with what the log expects."""
    return path.read_bytes().decode("utf-8", "replace") if os.path.lexists(path) else None


def _move(src: Path, dst: Path) -> None:
    """Rename without overwriting anything.

    A hard link fails at once if ``dst`` exists, where a rename would replace a file made there meanwhile. On a
    filesystem without hard links (FAT, exFAT), the check and the rename are separate steps.
    """
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise FileExistsError(errno.EEXIST, "kohde on jo olemassa", str(dst)) from None
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "kohde on jo olemassa", str(dst)) from None
        os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        os.unlink(dst)  # the link just made: the file stays where it was
        raise


def _finish(store: Store, root: Path, apply_id: int, plan: Plan, done: int, status: str, error: str) -> None:
    # A decision carried out in the library is no longer undone from the grid (Ctrl+Z): that would leave a moved
    # shot undecided, or a sidecar out of step with the database. Palauta karsitut reverses it instead.
    store.forget_decisions({op.shot_id for op in plan.ops[:done]})
    store.db.execute(
        "UPDATE applies SET status = ?, error = ?, finished = ? WHERE id = ?", (status, error, time.time(), apply_id)
    )
    store.commit()
    try:
        write_log(store, root, apply_id)
    except OSError:
        pass  # the database has it all; the file is a copy for reading by hand


def write_log(store: Store, root: Path, apply_id: int, path: Path | None = None) -> None:
    """The apply's log as JSON, by default at its place in the reject folder."""
    info = apply_info(store, root, apply_id)
    assert info is not None
    rel = _rel(root)
    ops = store.db.execute(
        "SELECT seq, kind, shot_id, src, dst, backup, new_sha256, done FROM apply_ops WHERE apply_id = ? ORDER BY seq",
        (apply_id,),
    ).fetchall()
    data = {
        **info,
        "root": str(root),
        "ops": [
            {
                "seq": seq,
                "kind": kind,
                "shot_id": shot_id,
                "src": rel(src),
                "dst": dst and rel(dst),
                "backup": backup,
                "new_sha256": new,
                "done": bool(d),
            }
            for seq, kind, shot_id, src, dst, backup, new, d in ops
        ],
    }
    write_atomic(
        path or Path(store.db.execute("SELECT log FROM applies WHERE id = ?", (apply_id,)).fetchone()[0]),
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
    )


def apply_info(store: Store, root: Path, apply_id: int) -> dict[str, Any] | None:
    """An apply as the API shows it: its state and how many of each operation it has."""
    row = store.db.execute(
        "SELECT id, event_id, status, error, started, finished, undone_at, log FROM applies WHERE id = ?", (apply_id,)
    ).fetchone()
    if row is None:
        return None
    info = dict(zip(("id", "event_id", "status", "error", "started", "finished", "undone_at", "log"), row, strict=True))
    info["log"] = _rel(root)(info["log"])
    counts = store.db.execute(
        "SELECT COUNT(DISTINCT CASE WHEN kind = 'move' THEN shot_id END), SUM(kind = 'move'), SUM(kind = 'xmp_create'),"
        " SUM(kind = 'xmp_update'), COUNT(*), SUM(done) FROM apply_ops WHERE apply_id = ?",
        (apply_id,),
    ).fetchone()
    keys = ("shots_moved", "files_moved", "xmp_created", "xmp_updated", "ops", "done")
    info.update(zip(keys, (int(c or 0) for c in counts), strict=True))
    return info


def event_applies(store: Store, root: Path, event_id: int) -> list[dict[str, Any]]:
    """The event's applies, newest first."""
    ids = [r[0] for r in store.db.execute("SELECT id FROM applies WHERE event_id = ? ORDER BY id DESC", (event_id,))]
    return [info for i in ids if (info := apply_info(store, root, i)) is not None]


def fail_interrupted(store: Store) -> None:
    """Applies of a previous server run that never finished: their log is there, so they can be restored."""
    store.db.execute(
        "UPDATE applies SET status = 'failed', error = ?, finished = ? WHERE status = 'running'",
        (INTERRUPTED, time.time()),
    )


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def restore(store: Store, root: Path, apply_id: int, undone_dir: Path) -> dict[str, Any]:
    """Reverse an apply: its operations in reverse order, each checked against the files.

    A moved file goes back (unless it is already there); a sidecar gets its previous content back (one changed since
    the apply is first copied into ``undone_dir/<id>/``); a sidecar the apply created moves into ``undone_dir/<id>/``,
    and so does the apply's log. Operations the apply did not get to are skipped; when it was cut short (a crash),
    the one under way then is checked against the files too.

    Raises LookupError for an unknown apply and ApplyConflict, before changing anything, when it is undone already,
    a newer apply of the event is not, a file and its moved copy both exist, or a path now leads out of the library.
    Raises ApplyFailed when an operation fails; what was reversed stays reversed, and restore can be run again.

    Returns ``missing``: moved files found in neither place, ``created``: the sidecars the apply created, now in the
    undone folder, ``changed``: copies of sidecars changed since the apply, ``log``: where the log went (None if it
    could not be moved), ``undone_dir``: the apply's folder in ``undone_dir``.
    """
    row = store.db.execute(
        "SELECT event_id, status, error, created_dir, log FROM applies WHERE id = ?", (apply_id,)
    ).fetchone()
    if row is None:
        raise LookupError("toteutusta ei ole")
    event_id, status, error, created_dir, log = row
    if status == "undone":
        raise ApplyConflict("toteutus on jo palautettu")
    if store.db.execute(
        "SELECT 1 FROM applies WHERE event_id = ? AND id > ? AND status != 'undone'", (event_id, apply_id)
    ).fetchone():
        raise ApplyConflict("tapahtumalla on uudempi toteutus; palauta se ensin")
    rows = store.db.execute(
        "SELECT seq, kind, src, dst, backup, new_sha256, done, shot_id FROM apply_ops WHERE apply_id = ? "
        "ORDER BY seq DESC",
        (apply_id,),
    ).fetchall()
    interrupted = status == "running" or error == INTERRUPTED
    in_flight = min((r[0] for r in rows if not r[6]), default=None) if interrupted else None
    ops = [(*r[1:6], r[7]) for r in rows if r[6] or r[0] == in_flight]
    rel = _rel(root)

    # a moved file is renamed, so only its folders must stay in the library; a sidecar is written through
    outside = [
        rel(src)
        for kind, src, dst, _, _, _ in ops
        if not inside_root(root, [str(Path(src).parent), str(Path(dst).parent)] if kind == "move" else [src])
    ]
    if outside:
        raise ApplyConflict(f"polku ei enää ole kirjastossa: {', '.join(outside)}")
    both = [
        rel(src) for kind, src, dst, _, _, _ in ops if kind == "move" and os.path.lexists(src) and os.path.lexists(dst)
    ]
    if both:
        raise ApplyConflict(f"tiedosto on sekä paikallaan että {REJECT_DIR}-kansiossa: {', '.join(both)}")

    keep_dir = undone_dir / str(apply_id)
    missing: list[str] = []
    created: list[str] = []
    changed: list[str] = []
    found: set[int] = set()  # shots with a file back in place
    lost: set[int] = set()  # shots with a moved file found nowhere
    try:
        for kind, src, dst, backup, new_sha, shot_id in ops:
            src_p = Path(src)
            if kind == "move":
                if os.path.lexists(dst):
                    _move(Path(dst), src_p)
                    found.add(shot_id)
                elif os.path.lexists(src):
                    found.add(shot_id)
                else:
                    missing.append(rel(dst))
                    lost.add(shot_id)
            elif kind == "xmp_create":
                if os.path.lexists(src_p):
                    created.append(str(_keep(root, src_p, keep_dir)))
            else:
                now = _current(src_p)
                if now == backup:
                    continue
                if now is not None and sha256(now) != new_sha:
                    changed.append(str(_keep(root, src_p, keep_dir, copy=True)))  # changed since the apply
                write_atomic(src_p, backup)
    except OSError as e:
        store.db.rollback()
        error = f"palautus keskeytyi: {e.strerror or e}{f': {e.filename}' if e.filename else ''}; yritä uudelleen"
        store.db.execute("UPDATE applies SET error = ? WHERE id = ?", (error, apply_id))
        store.commit()
        raise ApplyFailed(apply_id, error) from e
    store.db.execute(
        "UPDATE shots SET state = 'present' WHERE state = 'moved' AND id IN "
        "(SELECT shot_id FROM apply_ops WHERE apply_id = ? AND kind = 'move')",
        (apply_id,),
    )
    # a shot none of whose files was found stays out of the grid, as the next analysis would leave it
    store.db.executemany("UPDATE shots SET state = 'missing' WHERE id = ?", [(i,) for i in lost - found])
    store.db.execute(
        "UPDATE applies SET status = 'undone', error = '', undone_at = ? WHERE id = ?", (time.time(), apply_id)
    )
    log_dest = _move_log(store, root, apply_id, Path(log), keep_dir)
    store.commit()
    if created_dir:
        try:
            Path(log).parent.rmdir()  # only when empty: anything put there since stays
        except OSError:
            pass
    return {
        "missing": missing,
        "created": created,
        "changed": changed,
        "log": log_dest and str(log_dest),
        "undone_dir": str(keep_dir),
    }


def _move_log(store: Store, root: Path, apply_id: int, log: Path, keep_dir: Path) -> Path | None:
    """The log goes, updated, with the other files restore keeps, so the event folder is again as it was. Best
    effort: the files are already back, and the database has the log too."""
    if not os.path.lexists(log):
        return None
    try:
        dest = _free(keep_dir / log.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        store.db.execute("UPDATE applies SET log = ? WHERE id = ?", (str(dest), apply_id))
        write_log(store, root, apply_id, dest)
        log.unlink()
    except OSError:
        store.db.execute("UPDATE applies SET log = ? WHERE id = ?", (str(log), apply_id))
        return None
    return dest


def _free(path: Path) -> Path:
    """``path``, or ``path.1``, ``path.2``… if it is taken: nothing in the undone folder is overwritten."""
    out, n = path, 0
    while os.path.lexists(out):
        n += 1
        out = path.with_name(f"{path.name}.{n}")
    return out


def _keep(root: Path, path: Path, keep_dir: Path, copy: bool = False) -> Path:
    """Move (or copy) a library file into ``keep_dir``, under its path in the library."""
    try:
        sub = path.relative_to(root)
    except ValueError:
        sub = Path(path.name)
    dest = _free(keep_dir / sub)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(path, dest)
    else:
        shutil.move(path, dest)
    return dest
