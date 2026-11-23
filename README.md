# karsija

Photo culling helper. It walks a library of event folders, scores every photo for sharpness and exposure, groups
near-identical shots (bursts, repeats) inside each event, picks the best of each group and suggests which shots to
keep and which to reject. You review the suggestions in the browser (`karsija serve`), and only then are your
decisions carried out in the library: rejects move into a `_karsitut/` subfolder and ratings go into the XMP
sidecars. **Nothing is ever deleted**, and every apply in the browser (Toteuta) can be undone.

`karsija scan` does the same analysis in one batch and writes an HTML report instead (see [`scan`](#scan)).

## Install

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .       # installs the `karsija` command
uv sync                 # or, in a development checkout; then run it as `uv run karsija …`
```

Dependencies: numpy, opencv-python-headless, pillow, rawpy, fastapi and uvicorn, all installed by uv.

RAW files are read with [rawpy](https://github.com/letmaik/rawpy) (LibRaw), a required dependency with wheels for
the common platforms; where it has none, the install fails. If rawpy is installed but cannot be imported (e.g. a
broken LibRaw), karsija still runs: a RAW+JPEG pair is analysed and previewed
from its JPEG, but a RAW without a JPEG cannot be analysed (it is judged *error* and never suggested for rejection)
and its preview fails with `RAW-kuvan esikatselu vaatii rawpy-paketin`.

## Library layout

```
~/Kuvat/                          ROOT
  2025/                           YYYY
    2025-06-14-juhannus/          YYYY-MM-DD[-name]: one event
      DSC_0001.NEF                a shot: files with the same stem (RAW + JPEG)
      DSC_0001.JPG
      DSC_0001.NEF.xmp            sidecars (darktable style name.ext.xmp, or name.xmp)
      _karsitut/                  rejects moved here by Toteuta (or scan --move)
    2025-06-20/
```

Year folders are named with four digits, event folders start with `YYYY-MM-DD`; anything after that is the event's
name. Other folders are ignored, and only the files directly in an event folder count (so `_karsitut/` is not
reviewed again). Files with the same stem form one shot, with the RAW as its primary file. Recognised formats: JPEG,
PNG, TIFF, WebP, HEIC and the RAW formats NEF, NRW, CR2, CR3, ARW, DNG, ORF, RW2, RAF, PEF and SRW.

## Usage

```sh
karsija serve ~/Kuvat                            # opens http://127.0.0.1:8765/ in the browser
karsija serve ~/Kuvat --port 9000 --no-browser
karsija serve ~/Kuvat --docs                     # also the API test page at /docs
karsija serve --help                             # --host, --allow-host, --workers, --db, --previews

karsija scan ~/Kuvat                             # batch analysis and HTML report, see below
karsija scan ~/Kuvat --only 2025-06 --report ~/karsinta
karsija scan --help                              # all options and thresholds
```

In a development checkout, prefix these with `uv run` (`uv run karsija serve ~/Kuvat`); `python -m karsija` works
too.

The workflow: **serve** the library, **Analysoi** an event (or all of them), review it in **Tapahtuma** with the
keyboard, check the result in the **Loupe** and **Vertailu**, then **Toteuta** to carry out your decisions.

## Views

The UI is in Finnish. It needs no network: Preact and htm are vendored in `karsija/static/vendor/` (versions and
licences in its README), there is no build step, and the page's Content-Security-Policy lets it load nothing from
other sites. Stopping the server (Ctrl+C) waits at most two seconds for open pages to let go.

### Kirjasto

The page `serve` opens: the event folders by year, with each event's shot count, cull candidates (shots judged
blurry or similar) and how many shots are reviewed and unreviewed. Events the server has not analysed yet are
marked *Ei analysoitu*; the others show when they were last analysed. Analysis does not start on its own:
**Analysoi** analyses one event (**Analysoi uudelleen** once it has been analysed; unchanged shots come from the
cache, so a second run mostly regroups), **Analysoi kaikki** the whole library, and a progress bar follows it; the
counts update when it finishes. While **Analysoi kaikki** runs, the events show *mukana kirjaston analyysissä*
instead of their own button: they are part of that analysis and cannot be started on their own meanwhile.

If an analysis fails, its error stays on the event's row (or under **Analysoi kaikki**) until the event is analysed
again; the page forgets it on reload. An analysis cut short by stopping the server fails with *keskeytyi*. While
the server cannot be reached, a banner says so, and the page reconnects, shows the analyses that ended meanwhile
and reloads the counts by itself.

### Tapahtuma

An event's name opens **Tapahtuma** (`#/event/<id>`), the review grid. Similar shots (bursts, repeats) sit together
in a box, single shots without one. A card's colour is the automatic verdict (green best, grey keep, amber similar,
red blurry, purple error), with its suggestion (*→ pidä* / *→ hylkää*), the reason and the sharpness (in a group
also as % of the group's sharpest). Your own decision is a badge in the corner (✓ kept, ✕ rejected and dimmed) and
the stars at the bottom; without one, the suggestion stands. **Vain ehdokkaat** shows only shots judged similar or
blurry, **Vain ryhmät** only groups of more than one shot, **Vain tarkastamattomat** only shots without a decision
(a shot decided meanwhile stays until the filters change or the event reloads, e.g. when an analysis ends). A click
picks a shot, a double-click opens it (like Enter). The whole event can be reviewed with the keyboard (see
[Keyboard shortcuts](#keyboard-shortcuts)).

Decisions show at once and are saved in the background, one at a time in the order they were made. A decision the
server refuses is taken back, and the error stays under the toolbar until closed (× or Esc); further failures are
counted there.
Ctrl+Z undoes the last decision stored on the server, even one made before the page was opened or in another event
(the page then says so).

The toolbar also has **Asetukset** (see [Calibrating thresholds](#calibrating-thresholds)) and **Toteuta**, and
under the grid **Toteutukset** lists the event's applies (see [Safety model](#safety-model)).

### Loupe

Enter (or a double-click) opens the current shot in the **loupe** (`#/event/<id>/loupe/<shot>`): its 2560 px
preview, fitted to the window (never enlarged). Z or a click zooms in to 100 % (one preview pixel per screen pixel)
around the pointer; beside the image, around its nearest edge, and with Z while the pointer is off the image area,
around the centre. Where 100 % would hardly enlarge the fitted image (a HiDPI screen), it zooms to one preview pixel
per CSS pixel instead (200 % on a 2× screen); the corner label shows which. Where even that would not enlarge it
(the image already shows whole at full size), the image does not zoom. Drag to pan, Z or a click again to fit. The
zoom and the spot stay when moving to the next shot, so a burst can be compared zoomed in; until the next full
preview has loaded, its grid preview stands in at the same place, labelled *ladataan…*. If the full preview cannot
be made, the stage says so over the grid preview, which is too small to judge sharpness from.

Beside the image: where the shot is among the shown ones (and which filters hide the others), your decision and
stars, the automatic verdict with its suggestion and reason, the sharpness (in a group also as % of the group's
sharpest), blown highlights and crushed shadows (% of the pixels), the capture time, and the shutter speed,
aperture, ISO, focal length (and its 35 mm equivalent when the camera stores another), camera and lens when the
file's EXIF has them, and the shot's files (RAW and JPEG). Every key except Enter works in the loupe as in the grid,
the filters still apply, and the next and previous shots' previews load ahead. Esc (or Back) returns to the grid
where it was left, scrolled only as far as the current shot needs.

### Vertailu

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

## Keyboard shortcuts

The same keys work in Tapahtuma, the loupe and Vertailu; ? lists them on the page.

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
| Esc | back to the grid; in the grid, closes the message under the toolbar, then Asetukset |
| ? | all of these on the page |

Only ← → and ↑ ↓ repeat while held, so holding P or Space does not decide shot after shot. While the Toteuta
dialog is open, the review keys do nothing.

## Safety model

In `karsija serve`:

- **Nothing is deleted.** karsija moves files within the event folder and writes XMP sidecars and its apply log;
  restoring moves the files back and sets the sidecars the apply created aside in `~/.cache/karsija/undone/`.
- **Decisions live only in the server's database** until you apply them. Reviewing, analysing and changing the
  thresholds never touch the library; the server writes into it only when an event is applied (Toteuta) or
  restored (Palauta karsitut).
- **Only reviewed shots are applied.** Shots without a decision of your own (keep or reject) are never touched,
  whatever their stars or automatic verdict.
- **Rejects go to `_karsitut/`** in the event folder, together with their RAW, JPEG and sidecars.
- **XMP is merged, not replaced**: only the rating and label are written, everything else in the sidecar stays.
- **Every apply is logged and can be reversed** with Palauta karsitut.
- **The server listens on loopback only** by default.

`karsija scan` is different: `--move` and `--write-xmp` act on the automatic verdicts directly, without review. They
are not logged and cannot be restored with Palauta karsitut (`--move` can only be undone by moving the files back by
hand), and `--xmp-overwrite` replaces your own ratings and labels without keeping a copy.

### Toteuta

**Toteuta** (in the grid's toolbar) carries out the event's decisions in the library. It first shows what it would
do (*siirretään N kuvaa, kirjoitetaan M XMP:tä, K tarkastamatta*) and does nothing until confirmed. While it is
open, the review keys do nothing and Esc closes it (except while the apply runs):

- Each shot you rejected moves into the event folder's `_karsitut/`, with its RAW, its JPEG and its sidecars of both
  styles (`name.xmp`, `name.ext.xmp`).
- Every shot with a decision (keep or reject) gets its rating and label in its XMP sidecars, merged as described in
  [XMP sidecars](#xmp-sidecars): a reject is -1 / Red, a keep your stars or else 3 (best 5 / Green). A keep without
  stars leaves the sidecars of a shot that hold a rating or label of your own alone; the confirmation counts those
  shots.
- Shots without a decision are never touched, whatever their stars or automatic verdict.
- The apply's log goes into `_karsitut/` too, so the folder is made even when no shot is rejected.
- Nothing is overwritten. A file already in `_karsitut/` under the same name, a sidecar that is not valid XMP or is
  read-only, a file of the shot that has gone (analyse the event again), or a `_karsitut` that is a symlink or not
  a folder, is listed as a conflict with what to do. Toteuta cannot be confirmed until it is fixed; **Tarkista
  uudelleen** then checks the plan again. Nor can it while an analysis of the event is queued or running.
- If the files or decisions change between the confirmation and the click (another tab, another program), the
  server refuses with *suunnitelma on muuttunut* and nothing changes; check the plan again. If an apply fails part
  way (a full disk), the dialog says so, and the apply is in the history below, from where it can be restored.

The moved shots leave the grid and no longer count in the event's or Kirjasto's numbers. Ctrl+Z does not undo an
apply, nor any longer the decisions it carried out: the first Ctrl+Z after Toteuta only says so, the next one
undoes the latest decision still on the undo stack.

### Palauta karsitut

Under the grid, **Toteutukset** lists the event's applies; **Palauta karsitut** reverses one: the files move back,
the sidecars get their earlier content back, and the sidecars the apply created move into
`~/.cache/karsija/undone/<id>/` (`$XDG_CACHE_HOME/karsija/undone/` when that is set), with the apply's log. A
sidecar changed since the apply (e.g. in darktable) is copied there before its earlier content is written back.
Nothing is deleted; `_karsitut/` is removed only if the apply made it and it is empty again. A shot none of whose
moved files is found (deleted by hand) is left out of the grid. Only the newest apply of an event not yet restored
can be restored, so restore them newest first. A file the apply was to write through a path that now leads out of
the library (a symlink put there since) stops the restore before it changes anything. Your decisions stay as they
were (though Ctrl+Z still cannot undo them), so the event can be applied again.

Every operation is logged before anything changes, in the server's database and in
`<event>/_karsitut/karsija-apply-<id>.json` (with each sidecar's earlier content), so an apply cut short by a crash
or a full disk can still be restored: restore checks each operation against the files rather than trusting the log.
A restore cut short can be run again.

### XMP sidecars

Toteuta and `scan --write-xmp` write the rating and label the same way:

- Only `xmp:Rating` and `xmp:Label` are written. Every existing `name.xmp` (digiKam, Lightroom) and `name.ext.xmp`
  (darktable, also for the JPEG of a RAW+JPEG pair) is updated in place, keeping all its other data (edit history,
  tags, face regions). `name.xmp` is created only when the photo has no sidecar of either kind.
- A photo whose sidecar already holds a rating or label of the user's own (anything karsija does not write itself:
  e.g. 4 stars or Purple) is left alone and counted as such; with `scan`, `--xmp-overwrite` replaces those too. For
  keep, an existing label is removed only if it is one karsija writes (Green, Yellow, Red).
- Lightroom and Bridge show both fields; darktable and digiKam read the stars from `xmp:Rating`, but keep their
  colour labels elsewhere (`darktable:colorlabels`, `digiKam:ColorLabel`), so karsija's label does not show there.
  darktable reads sidecars only for new images unless "look for updated XMP files on startup" is enabled (or the
  sidecar is reloaded by hand); otherwise it later overwrites karsija's rating from its own database.
- A sidecar that already has the values is not rewritten. If a sidecar is not valid XMP, cannot be read or is
  read-only, none of that photo's sidecars is changed.
- The other data survives, but the file is re-serialised, so its layout can change (e.g. line breaks between
  attributes, where namespaces are declared, quote style).

### Network

The server binds `127.0.0.1` by default. Any `--host` that is not a loopback address (`localhost`, `127.0.0.0/8`,
`::1`) prints a warning: the server has no login, so anyone who can reach it can browse the library, record
decisions and apply them (move files). Writes from other web sites are refused, and requests must name the server in
their `Host` header, so a web page open in the browser cannot use the server either. The accepted names are
`localhost`, `127.0.0.1`, `[::1]` and the `--host` address; with `--host 0.0.0.0` or `::`, also this machine's host
name (`name` and `name.local`) and its addresses. Open the server by one of these, or add another name with
`--allow-host NAME` (repeatable). Any other name gets `400 tuntematon Host-otsake`, which lists the accepted names.

Only image files inside the library root (and previews made from them) are served; `..`, absolute paths and
symlinks that lead outside it are rejected. `--docs` is off by default because the `/docs` page loads Swagger UI
from cdn.jsdelivr.net.

## Calibrating thresholds

How shots are grouped and judged depends on five thresholds. **Asetukset** (in the grid's toolbar) shows them as
sliders, the same for every event, beside the event's sharpness as a histogram on a logarithmic axis.

First, how sharpness is measured: the frame is split into 8 × 8 tiles, flat tiles (sky, blown-out areas) are
skipped, and each tile's sharpness is its second-derivative energy in its *weakest* of four directions, so camera
shake, which smears detail along one direction, scores low. A shot's sharpness is the mean of its sharpest ~10 % of
tiles: a shallow depth-of-field shot with a sharp subject scores high, a missed focus has no sharp tiles. The
number has no unit; it depends on the camera, the scene and the image size, which is why the histogram helps.

Grouping (within an event, shots in capture-time order; a shot joins the previous shot's group when both hold):

- **`gap`** (default 3 s, 0–3600): the most seconds between two consecutive shots of a group. Raise it if bursts
  with pauses split into several groups, lower it if separate moments are lumped together.
- **`hamming`** (default 40, 0–256): how different two consecutive shots may look, as differing bits of a 256-bit
  difference hash (dHash) of the image. Up to twice this is still allowed when the colour histograms are nearly the
  same. Raise it to group shots with more movement or reframing, lower it if shots of different subjects end up
  together.

Judging:

- **`abs_blur`** (default 10, 0–1000000): a shot sharper than this is never blurry by this rule; anything below is.
  The solid line in the histogram.
- **`rel_blur`** (default 0.25, 0–1): a shot under this fraction of the event's median sharpness is blurry, so the
  limit follows the event (a dim indoor event has a lower median than a sunny one). The dashed line in the
  histogram, at `rel_blur` × the median; the faint dotted one is the median itself.
- **`sim_keep`** (default 0, 0–1): in a group, the sharpest shot is *best* (unless it is blurry) and the others
  *similar*, suggested for rejection. With `sim_keep` above 0, a shot at least this fraction of the best's
  sharpness is suggested to keep instead (e.g. 0.95 keeps shots within 5 % of the best). 0 keeps only the best.

A shot is blurry when it is under either line, so the higher line decides. In the histogram the part of each bar
under it (the shots judged blurry) is red and the caption counts them; the lines move while the sliders do. A good
start: look at an event you know, and move `abs_blur` and `rel_blur` until the red part is the shots you would
throw away, then check a few in the loupe. If the event's histogram has two humps, the limit belongs in the valley
between them.

Nothing changes until **Käytä**, which stores the thresholds and groups and judges every analysed event again from
the stored metrics (no image is analysed again); the grid, its counts and Kirjasto's then show the new verdicts.
Your decisions stay as they are, also on shots whose group or verdict changes. **Peru muutokset** puts the sliders
back to the stored values, **Oletukset** to the defaults (a threshold at its default follows it, also if a later
version changes it). Esc or × closes the panel; a change not yet applied stays in it until the event view is left.

The server's thresholds are its own; `scan` takes the same five as flags (`--abs-blur`, `--rel-blur`, `--gap`,
`--hamming`, `--sim-keep`), which do not apply to the server.

## Where data lives

| What | Where |
|---|---|
| Server database (analyses, decisions, thresholds, apply history) | `~/.local/share/karsija/<hash>.sqlite` (`$XDG_DATA_HOME/karsija/`), or `serve --db FILE` |
| Preview cache | `~/.cache/karsija/previews/` (`$XDG_CACHE_HOME/karsija/previews/`), or `serve --previews DIR` |
| Apply logs | `<event>/_karsitut/karsija-apply-<id>.json`, and in the server database |
| Restored applies (created sidecars, changed sidecars, logs) | `~/.cache/karsija/undone/<id>/` (`$XDG_CACHE_HOME/karsija/undone/`) |
| `scan` report and its database | the `--report` folder (default `./karsintaraportti`) |

`<hash>` is the first 16 hex digits of the SHA-256 of the resolved library root, so every library gets its own
database. It is separate from the `scan` command's `karsija.sqlite`, so the server's first analysis repeats the work
even if `scan` has already analysed the library. Upgrading karsija upgrades it too (now schema 3), after which an
older karsija can no longer open it. Events analysed before schema 3 count as analysed, at the end of the last
finished analysis job that covered them (with no time shown when there is none). Shots whose files have disappeared
are left out of the event; if they come back, their decisions are still there.

Previews are made by the analysis as it goes (a missing one on the first request), one file per shot and size,
typically 0.5–1 MB per shot. A preview is made again when a file of the shot changes. Old previews are not removed;
the folder can be deleted at any time. `serve` warns at startup when the preview folder cannot be written, or when
it is inside the library.

## `scan`

`karsija scan ROOT` analyses the library (or with `--only TEXT` the event folders whose path contains `TEXT`) and
writes a report. It does not use the server's database, thresholds or decisions: it acts on the automatic verdicts.
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

Optional actions, both on the automatic verdicts (nothing is deleted here either):

- `--write-xmp` writes the verdict to the XMP sidecars as described in [XMP sidecars](#xmp-sidecars): best = 5 /
  Green, keep = 3, similar = 1 / Yellow, blurry = -1 / Red. Shots that could not be analysed are left alone. If a
  write fails, the photo's sidecars already written are put back where possible; the error names any that could not
  be. Either way the photo is reported and `scan` exits with status 1.
- `--move blurry,similar` moves rejects into the event folder's `_karsitut/` subfolder, together with their RAW/JPEG
  pair and XMP sidecars. This is not logged and cannot be restored with Palauta karsitut: move them back by hand to
  undo.

The first run after upgrading imports the analyses from the old script's `cache.sqlite`; after that the old file can
be deleted.

> Before the package, the tool was a single script run as `python karsija.py ROOT`.
> The same flags now go after `karsija scan ROOT`.

## JSON API

The server can also be used through its JSON API. With `--docs`, the API is listed and can be tried out at
`/docs`. Send every POST/PUT/PATCH with `Content-Type: application/json`, even without a body (e.g.
`curl -X POST localhost:8765/api/decisions/undo -H 'Content-Type: application/json'`).

### Analysis

```sh
curl -X POST localhost:8765/api/analyse -H 'Content-Type: application/json' -d '{"scope": "all"}'
curl localhost:8765/api/library                  # events with shot counts and analysed_at; an event id also works as "scope"
```

`analysed_at` (in `/api/library` and in `event` of `/api/events/{id}`) is when the last server analysis that
covered the event finished, in Unix seconds: `null` if none has, `0` if one has but its time is not known (events
analysed before schema 3). Only a job that finishes sets it; a failed one leaves the old value.

Progress is pushed over server-sent events (`/api/events/stream`) and can be polled at `/api/jobs/{id}`. A shot
is analysed only once as long as its files do not change. An analysis that is still running when the server stops
shows as failed after a restart; start it again. Asking for an analysis while one with the same scope is still
queued or running returns that job (200) instead of starting another.

### Settings

The server groups and judges with its own thresholds, by default those of `scan` (`GET /api/settings`).
`PUT /api/settings` with at least one of `{"abs_blur": 0–1000000, "rel_blur": 0–1, "gap": 0–3600 (seconds),
"hamming": 0–256 (an integer), "sim_keep": 0–1}` changes them (a field left out keeps its value; `null`, or the
default's own value, puts back the default) and groups and judges every analysed event again from the stored
metrics, without analysing any image. An empty body, an unknown field or a value out of range is refused with `422`
and changes nothing. Decisions stay as they are, also on shots whose group or verdict changes, and so does the undo
stack. It returns the `settings` in effect and how many `events` were judged again. `/api/events/{id}` also has
`default_settings`, `sharpness` (for the histogram: the sharpness of each shot of the event that is in place and was
analysed without error, in ascending order) and `median_sharp`, their median as the judging takes it (of an even
count, the mean of the middle two): `rel_blur` × `median_sharp` is the relative blur limit.

### Shots and decisions

`GET /api/shots/{id}` returns one shot with its `exif`, which the shots of `/api/events/{id}` leave out:
`datetime_original`, `exposure_time` (seconds), `f_number`, `iso`, `focal_length` and `focal_length_35mm` (mm),
`camera` and `lens`, each only when the file has it.

- `PATCH /api/shots/{id}` with `{"user_verdict": "keep" | "reject" | null, "rating": 0-5 | null}`. Either field
  can be left out; it then keeps its value. `null` clears it. At least one field is required.
- `POST /api/groups/{event_id}/{group_id}/accept` applies the suggested keep/reject to the shots of the group that
  have no decision yet. Shots already decided, and shots that could not be analysed, are left alone.
- `POST /api/decisions/undo` undoes the latest decision (a group accept counts as one); call it again to go
  further back

### Apply and restore

Decisions are stored in the server's database until the event is applied (`scan --move` and `--write-xmp` do not
use them). Toteuta and its history; paths are relative to the library root unless said otherwise:

- `GET /api/events/{id}/apply-plan`: a dry run. `event_id`, `reject_dir` (the event's `_karsitut`), `move` (the
  rejects: `{shot_id, files}` each), `xmp_create` and `xmp_update` (sidecars), `user_values` (one sidecar of each
  shot whose sidecars are left alone), `unreviewed` (shots without a decision), `conflicts` (`path` and `reason`),
  `analysing` (an analysis of the event is queued or running, so an apply would be refused now) and
  `confirm_token`. `404` for an unknown event.
- `POST /api/events/{id}/apply` with `{"confirm_token": "…"}` carries out that plan and returns `{"apply": …}`,
  shaped as in `/applies`. `409` when the plan has changed since the token was given, has conflicts or nothing to
  do, or an analysis of the event is queued or running; nothing is changed then. `500` when an operation failed part
  way; the apply is then `failed` and can be restored. `404` for an unknown event.
- `GET /api/events/{id}/applies`: the event's applies, newest first: `id`, `event_id`, `status` (`running`, `done`,
  `failed`, `undone`), `error` (why it failed or why its restore stopped), `started`, `finished` and `undone_at`
  (Unix seconds), `shots_moved`, `files_moved`, `xmp_created`, `xmp_updated` (as planned), `ops` and `done`
  (operations planned and done) and `log` (its JSON log; once restored, an absolute path in the undone folder). An
  apply still running when the server stopped is `failed` after a restart, with `error` saying so, and can be
  restored. `404` for an unknown event.
- `POST /api/applies/{id}/restore` reverses one. `409` when it is restored already, a newer apply of the event is
  not, a moved file is back in its place while its copy is still in `_karsitut/`, or a path now leads out of the
  library; nothing is changed then. `404` for an unknown apply. `500` when it stops part way (the reason is in the
  apply's `error`); run it again. Returns the `apply`, `missing` (moved files found nowhere), and as absolute paths:
  `created` (the sidecars the apply created), `changed` (copies of sidecars changed since the apply), `log` (where
  the log went; `null` if it could not be moved) and `undone_dir` (the apply's folder in the undone folder).

```sh
TOKEN=$(curl -s localhost:8765/api/events/1/apply-plan | python3 -c 'import json,sys; print(json.load(sys.stdin)["confirm_token"])')
curl -X POST localhost:8765/api/events/1/apply -H 'Content-Type: application/json' -d "{\"confirm_token\": \"$TOKEN\"}"
curl -X POST localhost:8765/api/applies/1/restore -H 'Content-Type: application/json'
```

### Previews

`GET /api/shots/{id}/preview?size=grid` (long edge 400 px, the default) or `size=full` (2560 px) returns an upright
JPEG, never larger than the image it is made from. A RAW+JPEG pair is previewed from the JPEG, a RAW alone from its
embedded preview (or a half-size conversion when it has none), which is often smaller than 2560 px, so `full` can
be smaller too. Nearly all of a shot's cache space is the full size (see [Where data lives](#where-data-lives)).

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
written.

```sh
curl -o grid.jpg 'localhost:8765/api/shots/1/preview?size=grid'
curl -o full.jpg 'localhost:8765/api/shots/1/preview?size=full'
```

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check
uv run ruff format --check   # `uv run ruff format` fixes it
uv run mypy karsija
```

The tests generate a synthetic library; no real photos or network are needed.
CI (`.github/workflows/ci.yml`) runs these on every pull request and push to `main`, on Python 3.12 and 3.13, with
Node 22 so the `tests/js/` UI tests also run. It also runs `node --check` on `karsija/static/`, builds the wheel and
checks that the wheel includes the static files.
The UI's logic is tested in `tests/js/` with Node's own test runner (`node --test tests/js/`); pytest runs those
tests too when `node` is installed and skips them otherwise.
