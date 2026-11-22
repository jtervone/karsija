"""Library layout, file discovery, image loading and EXIF capture time."""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:
    import rawpy
except ImportError:  # RAW support is optional
    rawpy = None  # type: ignore[assignment]

JPEG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
RAW_EXT = {".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw"}
EVENT_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
ANALYSIS_EDGE = 1024          # long edge used for all measurements


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #
def load_image(path: Path) -> tuple[np.ndarray, Image.Image | None]:
    """Return BGR image (downscaled) and the PIL image for EXIF."""
    pil: Image.Image
    if path.suffix.lower() in RAW_EXT:
        if rawpy is None:
            raise RuntimeError("rawpy not installed, cannot read RAW")
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except Exception:
                thumb = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                pil = Image.open(io.BytesIO(thumb.data))
                pil.load()
            elif thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
                pil = Image.fromarray(thumb.data)
            else:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True)
                pil = Image.fromarray(rgb)
    else:
        pil = Image.open(path)
        pil.load()

    pil_rot = _apply_orientation(pil)
    rgb = np.asarray(pil_rot.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    scale = ANALYSIS_EDGE / max(h, w)
    if scale < 1:
        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return bgr, pil


def _apply_orientation(pil: Image.Image) -> Image.Image:
    try:
        from PIL import ImageOps
        return ImageOps.exif_transpose(pil)
    except Exception:
        return pil


_EXIF_DT_TAGS = (36867, 36868, 306)  # DateTimeOriginal, Digitized, DateTime


def capture_time(path: Path, pil: Image.Image | None) -> float:
    if pil is not None:
        try:
            exif = pil.getexif()
            sub = exif.get_ifd(0x8769) if hasattr(exif, "get_ifd") else {}
            for tag in _EXIF_DT_TAGS:
                val = sub.get(tag) or exif.get(tag)
                if val:
                    dt = datetime.strptime(str(val).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
                    # sub-second precision helps with bursts
                    subsec = sub.get(37521) or sub.get(37520)
                    frac = float("0." + str(subsec).strip()) if subsec and str(subsec).strip().isdigit() else 0.0
                    return dt.timestamp() + frac
        except Exception:
            pass
    return path.stat().st_mtime


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def find_event_dirs(root: Path) -> list[Path]:
    dirs: list[Path] = []
    for year in sorted(p for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}", p.name)):
        for ev in sorted(p for p in year.iterdir() if p.is_dir() and EVENT_DIR_RE.match(p.name)):
            dirs.append(ev)
    return dirs


def collect_shots(event_dir: Path) -> list[tuple[str, list[str]]]:
    """Group files by stem so RAW+JPEG pairs count as one shot."""
    by_stem: dict[str, list[Path]] = {}
    for p in event_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext in JPEG_EXT or ext in RAW_EXT:
            by_stem.setdefault(p.stem, []).append(p)
    out: list[tuple[str, list[str]]] = []
    for _stem, paths in sorted(by_stem.items()):
        raw = [p for p in paths if p.suffix.lower() in RAW_EXT]
        primary = raw[0] if raw else paths[0]
        out.append((str(primary), [str(p) for p in sorted(paths)]))
    return out

