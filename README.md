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
analysed yet are marked *Ei analysoitu*; the others show when they were last analysed. Analysis does not start on
its own: **Analysoi** analyses one event (**Analysoi uudelleen** once it has been analysed; unchanged shots come
from the cache, so a second run mostly regroups), **Analysoi kaikki** the whole library, and a progress bar follows
it; the counts update when it finishes. While **Analysoi kaikki** runs, the events show *mukana kirjaston
analyysissä* instead of their own button: they are part of that analysis and cannot be started on their own
meanwhile.

If an analysis fails, its error stays on the event's row (or under **Analysoi kaikki**) until the event is analysed
again; the page forgets it on reload. An analysis cut short by stopping the server fails with *keskeytyi*. While
the server cannot be reached, a banner says so, and the page reconnects, shows the analyses that ended meanwhile
and reloads the counts by itself.

An event's name opens **Tapahtuma** (`#/event/<id>`), the review grid. Similar shots (bursts, repeats) sit together
in a box, single shots without one. A card's colour is the automatic verdict (green best, grey keep, amber similar,
red blurry, purple error), with its suggestion (*→ pidä* / *→ hylkää*), the reason and the sharpness (in a group
also as % of the group's sharpest). Your own decision is a badge in the corner (✓ kept, ✕ rejected and dimmed) and
the stars at the bottom; without one, the suggestion stands. **Vain ehdokkaat** shows only shots judged similar or
blurry, **Vain ryhmät** only groups of more than one shot, **Vain tarkastamattomat** only shots without a decision
(a shot decided meanwhile stays until the filters change or the event reloads, e.g. when an analysis ends). A click
picks a shot, a double-click opens it (like Enter). The whole event can be reviewed with the keyboard:

| Key | |
|---|---|
| ← → | previous / next shot (hold to run through them); in compare, within the group |
| ↑ ↓ | previous / next group; in compare, the previous / next group with two or more shots shown |
| P, X | keep, reject |
| 0–5 | stars |
| U | back to the suggestion: clears the decision and the stars |
| Space | accept the suggestion for the group's shots that have no decision yet (also those a filter hides) and go to the next group (in compare, the next group with two or more shots shown) |
| Ctrl+Z | undo the last decision (a Space counts as one); again to go further back |
| Enter, C | open the shot (in compare, the selected one) in the loupe, compare the group's shots |
| Z | in the loupe and compare: zoom in (100 %, or 200 % on a 2× screen) around the pointer (or the centre; in compare, of the selected pane) / fit |
| Esc | back to the grid; in the grid, closes the message under the toolbar |
| ? | all of these on the page |

Decisions show at once and are saved in the background, one at a time in the order they were made. One the server
refuses is taken back, and its error stays under the toolbar until closed (× or Esc), counting further failures.
Ctrl+Z undoes the last decision stored on the server, even one made before the page was opened or in another event
(the page then says so).

Enter (or a double-click) opens the current shot in the **loupe** (`#/event/<id>/loupe/<shot>`): its 2560 px
preview, fitted to the window (never enlarged). Z or a click zooms in to 100 % (one preview pixel per screen pixel)
around the pointer; beside the image, around its nearest edge, and with Z while the pointer is off the image area,
around the centre. Where 100 % would hardly enlarge the fitted image (a HiDPI screen), it zooms to one preview pixel
per CSS pixel instead (200 % on a 2× screen); the corner label shows which. Where even that would not enlarge it
(the image already shows whole at full size), the image does not zoom. Drag to pan, Z or a click again to fit. The
zoom and the spot stay when moving to the next shot, so a burst can be compared zoomed in; until the next full
preview has loaded, its grid preview stands in at the same place, labelled *ladataan…*. If the full preview cannot
be made, the stage says so over the grid preview, which is too small to judge sharpness from. Beside the image:
where the shot is among the shown ones (and which filters hide the others), your decision and stars, the automatic
verdict with its suggestion and reason, the sharpness (in a group also as % of the group's sharpest), blown
highlights and crushed shadows (% of the pixels), the capture time, and the shutter speed, aperture, ISO, focal
length (and its 35 mm equivalent when the camera stores another), camera and lens when the file's EXIF has them, and
the shot's files (RAW and JPEG). Every key above except Enter works in the loupe as in the grid, the filters still
apply, and the next and previous shots' previews load ahead. Esc (or Back) returns to the grid where it was left,
scrolled only as far as the current shot needs.

C opens **Vertailu** (`#/event/<id>/compare/<group>`) for the current shot's group: its shown shots side by side,
best first (the one judged best, then by sharpness, shots that could not be analysed last), two or three in a row
(three in a 2 × 2 grid on a window under 72rem), four in a 2 × 2 grid; on a narrow window one under another, and the
page scrolls. A larger group shows four at a time: ← → move the selection within the group, and the panes follow it
only as far as they need. Above the panes: which of the comparable groups this is, how many shots it shows (and how
many a filter hides), the active filters and, in a larger group, which four are shown. Under each pane: the
automatic verdict and its suggestion, your decision and stars, the sharpness with its % of the group's sharpest, and
the file name. A click selects a pane; P, X, 0–5 and U apply to the selected one, and Enter opens it in the loupe.

A click on the selected pane, or Z, zooms every pane in to the same spot: around the pointer when it is over a pane,
otherwise around the centre of the selected pane (or, if its image would not enlarge, of the first one whose image
would). One viewport drives them all, its centre as fractions of each image and its size as the length of the
image's long edge, so the same spot shows at the same size even where the previews differ in size, shape or
orientation. The zoom is 100 % of the pane zoomed in on (200 % on a 2× screen, as in the loupe), and each pane's
label shows its own; a pane whose image would not enlarge stays fitted, labelled *sovitettu, ei suurennettavissa*.
Drag any zoomed pane to pan them all; a click on the selected pane or Z fits them again. The zoom stays while ← →
move the selection; another group starts fitted. Previews load as in the loupe (the grid preview stands in, labelled
*ladataan…*), and the panes just outside the four shown and the next group's first four load ahead.

Space accepts the group's suggestion and opens the next group with two or more shots shown (after the last one it
stays and says how many shots are still without a decision; Space skips single shots); ↑ ↓ move between such
groups. Esc (or Back) returns to the grid. A shot with no group, or a group a filter has cut to one shot, cannot be
compared: C says so in the grid, an address to such a group says so on the page, and Ctrl+Z in the compare view that
undoes a decision of such a shot shows it in the loupe.

The UI needs no network: Preact and htm are vendored in `karsija/static/vendor/` (versions and licences in its
README), there is no build step, and the page's Content-Security-Policy lets it load nothing from other sites.
Stopping the server (Ctrl+C) waits at most two seconds for open pages to let go.

The server can also be used through its JSON API. With `--docs`, the API is listed and can be tried out at
`/docs`. That page loads Swagger UI from cdn.jsdelivr.net, so it is off by default.

```sh
curl -X POST localhost:8765/api/analyse -H 'Content-Type: application/json' -d '{"scope": "all"}'
curl localhost:8765/api/library                  # events with shot counts and analysed_at; an event id also works as "scope"
```

`analysed_at` (in `/api/library` and in `event` of `/api/events/{id}`) is when the last server analysis that
covered the event finished, in Unix seconds: `null` if none has, `0` if one has but its time is not known (events
analysed before schema 3, see below). Only a job that finishes sets it; a failed one leaves the old value.

Progress is pushed over server-sent events (`/api/events/stream`) and can be polled at `/api/jobs/{id}`. A shot
is analysed only once as long as its files do not change. The server has its own database, so the first analysis
repeats the work even if `scan` has already analysed the library. An analysis that is still running when the
server stops shows as failed after a restart; start it again. Asking for an analysis while one with the same scope
is still queued or running returns that job (200) instead of starting another.

The server groups and judges with the default thresholds of `scan` (see `GET /api/settings`). The `scan`
threshold flags do not apply to it, and the thresholds cannot be changed yet.

`GET /api/shots/{id}` returns one shot with its `exif`, which the shots of `/api/events/{id}` leave out:
`datetime_original`, `exposure_time` (seconds), `f_number`, `iso`, `focal_length` and `focal_length_35mm` (mm),
`camera` and `lens`, each only when the file has it.

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
This database is separate from the `scan` command's `karsija.sqlite`. Upgrading karsija upgrades it too (now
schema 3), after which an older karsija can no longer open it. Events analysed before schema 3 count as analysed,
at the end of the last finished analysis job that covered them (with no time shown when there is none). Previews
go to `~/.cache/karsija/previews/` (see above; `--previews` chooses another folder). Only image files inside the library root (and previews made from
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
  The first run with this version upgrades the database (schema 3); older versions of karsija can
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
The UI's logic is tested in `tests/js/` with Node's own test runner (`node --test tests/js/`); pytest runs those
tests too when `node` is installed and skips them otherwise.
