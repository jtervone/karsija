# karsija

Photo culling helper. It walks a library laid out as `ROOT/YYYY/YYYY-MM-DD[-event-name]/`,
scores every photo for sharpness and exposure, groups near-identical shots (bursts, repeats)
inside each event folder, picks the best of each group and writes an HTML review report.

**Nothing is ever deleted.** Optional actions:

- `--write-xmp` writes rating and label to an XMP sidecar, only where no sidecar exists yet
  (both `name.xmp` and `name.ext.xmp` are checked).
- `--move blurry,similar` moves rejects into the event folder's `_karsitut/` subfolder, together
  with their RAW/JPEG pair and XMP sidecars. Move them back to undo.

## Install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync                 # development checkout
uv tool install .       # or: install the `karsija` command
```

Dependencies: numpy, opencv-python-headless, pillow, rawpy (for RAW files), fastapi and uvicorn.

## Usage

```sh
uv run karsija scan ~/Kuvat                      # or: karsija scan ..., python -m karsija scan ...
uv run karsija scan ~/Kuvat --only 2025-06 --report ~/karsinta
karsija scan --help                              # all options and thresholds
```

### Review server

```sh
uv run karsija serve ~/Kuvat                     # opens http://127.0.0.1:8765/ in the browser
karsija serve ~/Kuvat --port 9000 --no-browser
karsija serve ~/Kuvat --docs                     # also the API test page at /docs
karsija serve --help                             # --host, --allow-host, --workers, --db, --previews
```

The page it opens is **Kirjasto**: the event folders by year, with each event's shot count, cull candidates
(shots judged blurry or similar) and how many shots are reviewed and unreviewed. Events the server has not
analysed yet are marked *Ei analysoitu*. Analysis does not start on its own: **Analysoi** analyses one event,
**Analysoi kaikki** the whole library, and a progress bar follows it; the counts update when it finishes. The
event, loupe and compare views (`#/event/<id>`, `#/event/<id>/loupe/<shot>`, `#/event/<id>/compare/<group>`) come
later.

The UI needs no network: Preact and htm are vendored in `karsija/static/vendor/` (versions and licences in its
README), there is no build step, and the page's Content-Security-Policy lets it load nothing from other sites.
Stopping the server (Ctrl+C) waits at most two seconds for open pages to let go.

The server can also be used through its JSON API. With `--docs`, the API is listed and can be tried out at
`/docs`. That page loads Swagger UI from cdn.jsdelivr.net, so it is off by default.

```sh
curl -X POST localhost:8765/api/analyse -H 'Content-Type: application/json' -d '{"scope": "all"}'
curl localhost:8765/api/library                  # events with shot counts and analysed_at; an event id also works as "scope"
```

Progress is pushed over server-sent events (`/api/events/stream`) and can be polled at `/api/jobs/{id}`. A shot
is analysed only once as long as its files do not change. The server has its own database, so the first analysis
repeats the work even if `scan` has already analysed the library. An analysis that is still running when the
server stops shows as failed after a restart; start it again. Asking for an analysis while one with the same scope
is still queued or running returns that job (200) instead of starting another.

The server groups and judges with the default thresholds of `scan` (see `GET /api/settings`). The `scan`
threshold flags do not apply to it, and the thresholds cannot be changed yet.

Decisions:

- `PATCH /api/shots/{id}` with `{"user_verdict": "keep" | "reject" | null, "rating": 0-5 | null}`. Either field
  can be left out; it then keeps its value. `null` clears it. At least one field is required.
- `POST /api/groups/{event_id}/{group_id}/accept` applies the suggested keep/reject to the shots of the group that
  have no decision yet. Shots already decided, and shots that could not be analysed, are left alone.
- `POST /api/decisions/undo` undoes the latest decision (a group accept counts as one); call it again to go
  further back

Decisions are stored in the server's database only. They are not written to XMP sidecars or used by
`scan --move` yet. Send every POST/PATCH with `Content-Type: application/json`, even without a body (e.g.
`curl -X POST localhost:8765/api/decisions/undo -H 'Content-Type: application/json'`).

Previews: `GET /api/shots/{id}/preview?size=grid` (long edge 400 px, the default) or `size=full` (2560 px) returns
an upright JPEG, never larger than the image it is made from. A RAW+JPEG pair is previewed from the JPEG, a RAW
alone from its embedded preview (or a half-size conversion when it has none), which is often smaller than 2560 px,
so `full` can be smaller too. The analysis writes both sizes as it goes; a missing one is made on the first
request. They are kept in `$XDG_CACHE_HOME/karsija/previews/` (by default `~/.cache/karsija/previews/`;
choose another folder with `serve --previews DIR`), one file per shot and size, typically 0.5–1 MB per shot,
nearly all of it the full size. A preview is made again when a file of the shot changes. Old previews are not
removed; the folder can be deleted at any time.

Every shot in the JSON has a `preview` field with the URL of each size, e.g.
`{"grid": "/api/shots/12/preview?size=grid&v=…", "full": "…"}`. Its `v` names the current version of the shot's
files: the browser may keep that response for a year, and a changed file gets a new URL after the next analysis.
Any other URL (no `v`, or an old one) is answered with `Cache-Control: no-cache`, so the browser asks again each
time and gets `304 Not Modified` by the `ETag` while the files are unchanged.

Errors: a missing shot or file gives `404`, a file that resolves outside the library `403`. A broken file or a
format that cannot be read gives `422 kuvaa ei voi lukea: tiedosto on rikki tai sen muotoa ei tueta` (a bad `size`
is also `422`, but with FastAPI's list of validation errors as `detail` instead of a string). `500` means the
server cannot make the preview: `RAW-kuvan esikatselu vaatii rawpy-paketin`, `kuvaa ei voi lukea: ei
lukuoikeutta tiedostoon`, or `esikatselua ei voi tallentaa välimuistiin: …` when the preview folder cannot be
written. `serve` warns at startup when the preview folder cannot be written, or when it is inside the library.

```sh
curl -o grid.jpg 'localhost:8765/api/shots/1/preview?size=grid'
curl -o full.jpg 'localhost:8765/api/shots/1/preview?size=full'
```

The server binds `127.0.0.1` by default. Any `--host` that is not a loopback address (`localhost`,
`127.0.0.0/8`, `::1`) prints a warning: the server has no login, so anyone who can reach it can browse the
library and record decisions. Writes from other web sites are refused, and requests must name the server in their
`Host` header, so a web page open in the browser cannot use the server either. The accepted names are `localhost`,
`127.0.0.1`, `[::1]` and the `--host` address; with `--host 0.0.0.0` or `::`, also this machine's host name
(`name` and `name.local`) and its addresses. Open the server by one of these, or add another name with
`--allow-host NAME` (repeatable). Any other name gets `400 tuntematon Host-otsake`, which lists the accepted names.

The server never writes into the library. By default its state database lives at
`~/.local/share/karsija/<hash>.sqlite` (`$XDG_DATA_HOME/karsija/` when that is set). `<hash>` is the first 16 hex digits of the
SHA-256 of the resolved library root, so every library gets its own database. Use `--db` to choose another file.
This database is separate from the `scan` command's `karsija.sqlite`. Previews go to `~/.cache/karsija/previews/`
(see above; `--previews` chooses another folder). Only image files inside the library root (and previews made from
them) are served; `..`, absolute paths and symlinks that lead outside it are rejected. Shots whose files have
disappeared are left out of the event; if they come back, their decisions are still there.

> Before the package, the tool was a single script run as `python karsija.py ROOT`.
> The same flags now go after `karsija scan ROOT`.

## Output

Everything goes into the `--report` folder (default `./karsintaraportti`):

- `index.html`: the review report, grouped by event and similar-shot group
- `tulokset.csv`: one row per shot (folder, file, verdict, group, sharpness, clipping, time, reason)
- `thumbs/`: report thumbnails
- `karsija.sqlite`: the state database. It caches analyses, so a shot is analysed again only
  when one of its files changes (path, size or modification time). It also stores the EXIF
  capture data of every shot (time, exposure, aperture, ISO, focal length, camera, lens).
  Shots cached by an older version get their EXIF read on the next run without being analysed again.
  The first run with this version upgrades the database (schema 2); older versions of karsija can
  no longer open it after that.

The first run after upgrading imports the analyses from the old script's `cache.sqlite`;
after that the old file can be deleted.

## Development

```sh
uv run pytest
uv run ruff check
uv run mypy
```

The tests generate a synthetic library; no real photos or network are needed.
