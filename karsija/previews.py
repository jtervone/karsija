"""Preview cache: JPEG previews of a shot in two sizes, upright, outside the library.

``<cache dir>/<first 32 hex of sha256(signature)>-<size>.jpg``. The signature (``Store.signature``) changes with
the path, size or mtime of any file of the shot, so a changed file gets a new preview; old ones are left behind.
A shot is previewed from its JPEG when it has one (RAW+JPEG pair), otherwise from the RAW's embedded preview, the
same file the analysis reads.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import weakref
from pathlib import Path

from PIL import Image

from karsija.library import JPEG_EXT, open_image

SIZES = {"grid": 400, "full": 2560}       # long edge in pixels
QUALITY = {"grid": 80, "full": 88}

# EXIF orientation -> transpose that makes the image upright (as PIL.ImageOps.exif_transpose)
_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/karsija/previews``, by default ``~/.cache/karsija/previews``."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "karsija" / "previews"


def source_file(primary: str, files: list[str]) -> Path:
    """The file a shot is previewed from: its JPEG if it has one, else the primary (RAW) file."""
    return Path(next((f for f in files if Path(f).suffix.lower() in JPEG_EXT), primary))


def preview_path(cache_dir: Path, sig: str, size: str) -> Path:
    return cache_dir / f"{etag(sig)}-{size}.jpg"


def etag(sig: str) -> str:
    return hashlib.sha256(sig.encode()).hexdigest()[:32]


def render(pil: Image.Image, size: str) -> Image.Image:
    """Upright RGB copy of ``pil`` with its long edge at most ``SIZES[size]``."""
    edge = SIZES[size]
    try:
        orientation = int(pil.getexif().get(0x0112, 1))
    except Exception:
        orientation = 1
    scale = edge / max(pil.size)
    out = pil.convert("RGB") if pil.mode != "RGB" else pil
    if scale < 1:
        out = out.resize((max(1, round(pil.width * scale)), max(1, round(pil.height * scale))),
                         Image.Resampling.LANCZOS, reducing_gap=3.0)
    if orientation in _TRANSPOSE:
        out = out.transpose(_TRANSPOSE[orientation])
    return out


def save(img: Image.Image, dest: Path, size: str) -> None:
    """Write atomically: a reader (or another process making the same preview) never sees half a file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.stem}-", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            img.save(f, "JPEG", quality=QUALITY[size], optimize=True)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_all(pil: Image.Image, cache_dir: Path, sig: str) -> None:
    """Every size from an image that is already decoded (the analysis worker's)."""
    for size in SIZES:
        dest = preview_path(cache_dir, sig, size)
        if not dest.exists():
            save(render(pil, size), dest, size)


def make(primary: str, files: list[str], cache_dir: Path, sig: str, size: str) -> Path:
    """Decode the shot and write one size of its preview."""
    pil = open_image(source_file(primary, files), draft=SIZES[size])
    dest = preview_path(cache_dir, sig, size)
    save(render(pil, size), dest, size)
    return dest


class PreviewCache:
    """Previews on request; concurrent requests for the same missing preview make it once."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._locks: weakref.WeakValueDictionary[Path, threading.Lock] = weakref.WeakValueDictionary()
        self._guard = threading.Lock()

    def get(self, primary: str, files: list[str], sig: str, size: str) -> Path:
        dest = preview_path(self.cache_dir, sig, size)
        if dest.exists():
            return dest
        with self._guard:
            lock = self._locks.get(dest)
            if lock is None:
                lock = self._locks[dest] = threading.Lock()
        with lock:
            if dest.exists():          # made by the request that held the lock
                return dest
            return make(primary, files, self.cache_dir, sig, size)
