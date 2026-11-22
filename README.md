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

Dependencies: numpy, opencv-python-headless, pillow and rawpy (for RAW files).

## Usage

```sh
uv run karsija scan ~/Kuvat                      # or: karsija scan ..., python -m karsija scan ...
uv run karsija scan ~/Kuvat --only 2025-06 --report ~/karsinta
karsija scan --help                              # all options and thresholds
```

> Before the package, the tool was a single script run as `python karsija.py ROOT`.
> The same flags now go after `karsija scan ROOT`.

## Output

Everything goes into the `--report` folder (default `./karsintaraportti`):

- `index.html`: the review report, grouped by event and similar-shot group
- `tulokset.csv`: one row per shot (folder, file, verdict, group, sharpness, clipping, time, reason)
- `thumbs/`: report thumbnails
- `karsija.sqlite`: the state database. It caches analyses, so a shot is analysed again only
  when one of its files changes (path, size or modification time). Upcoming review features
  will also store your decisions here, so keep it.

The first run after upgrading imports the analyses from the old script's `cache.sqlite`;
after that the old file can be deleted.

## Development

```sh
uv run pytest
uv run ruff check
uv run mypy
```

The tests generate a synthetic library; no real photos or network are needed.
