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
karsija serve --help                             # --host, --workers, --db
```

The server binds `127.0.0.1` by default. Any other `--host` prints a warning: the server has no
login, so anyone who can reach it can browse the library and record decisions.

It never writes into the library. Its state database lives at
`~/.local/share/karsija/<hash>.sqlite` (`$XDG_DATA_HOME` is respected). `<hash>` is the first 16 hex
digits of the SHA-256 of the resolved library root, so every library gets its own database. Use `--db`
to choose another file. This database is separate from the `scan` command's `karsija.sqlite`.

Analysis runs in the background (`POST /api/analyse`), and progress is pushed over server-sent events
(`/api/events/stream`). Unchanged shots are served from the database. Decisions (`PATCH /api/shots/{id}`,
group accept) are stored in the database only, and `POST /api/decisions/undo` undoes the last one. The API is
listed at `/docs` while the server runs. Only files inside the library root are served; `..`, absolute
paths and symlinks that lead outside it are rejected.

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

The first run after upgrading imports the analyses from the old script's `cache.sqlite`;
after that the old file can be deleted.

## Development

```sh
uv run pytest
uv run ruff check
uv run mypy
```

The tests generate a synthetic library; no real photos or network are needed.
