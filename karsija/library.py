"""Library layout, file discovery, image loading and EXIF capture time."""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any

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
RAW_FLIP = {3: 3, 5: 8, 6: 6}  # LibRaw sizes.flip -> EXIF orientation (180, 90 counter-clockwise, 90 clockwise)


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #
class RawpyMissing(RuntimeError):
    """A RAW file was to be read but rawpy is not installed."""


def open_image(path: Path, draft: int | None = None) -> Image.Image:
    """Decoded image at full size, as stored (EXIF orientation not applied). RAW gives its embedded JPEG preview.

    An embedded preview usually carries no orientation of its own (the camera stores it in the RAW): the RAW's is
    then set as the image's EXIF orientation, so ``ImageOps.exif_transpose`` turns it upright like a JPEG.

    ``draft``: a JPEG may be decoded at a reduced scale, as long as both sides stay at least this many pixels.
    """
    pil: Image.Image
    if path.suffix.lower() in RAW_EXT:
        if rawpy is None:
            raise RawpyMissing("rawpy not installed, cannot read RAW")
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except Exception:
                thumb = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                pil = Image.open(io.BytesIO(thumb.data))
                if draft:
                    pil.draft("RGB", (draft, draft))
                pil.load()
            elif thumb is not None and thumb.format == rawpy.ThumbFormat.BITMAP:
                pil = Image.fromarray(np.asarray(thumb.data))
            else:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True)      # already turned upright
                return Image.fromarray(rgb)
            flip = RAW_FLIP.get(int(getattr(getattr(raw, "sizes", None), "flip", 0) or 0))
            if flip is not None and pil.getexif().get(0x0112, 1) == 1:
                pil.getexif()[0x0112] = flip
    else:
        pil = Image.open(path)
        if draft:
            pil.draft("RGB", (draft, draft))
        pil.load()
    return pil


def load_image(path: Path) -> tuple[np.ndarray, Image.Image | None]:
    """Return BGR image (downscaled) and the PIL image for EXIF."""
    pil = open_image(path)
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


# EXIF tag -> metadata field; IFD0 tags first, then the Exif sub-IFD
_EXIF_IFD0 = {271: "make", 272: "model"}
_EXIF_SUB = {
    36867: "datetime_original",
    33434: "exposure_time",       # seconds
    33437: "f_number",
    34855: "iso",
    37386: "focal_length",        # mm
    41989: "focal_length_35mm",
    42035: "lens_make",
    42036: "lens_model",
}


def exif_metadata(pil: Image.Image | None) -> dict[str, Any]:
    """Capture metadata stored per shot: time, exposure, aperture, ISO, focal length, camera and lens.

    Fields missing from the file are left out.
    """
    if pil is None:
        return {}
    try:
        exif = pil.getexif()
        sub = exif.get_ifd(0x8769)
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for tags, ifd in ((_EXIF_IFD0, exif), (_EXIF_SUB, sub)):
        for tag, name in tags.items():
            val = _exif_value(ifd.get(tag))
            if val not in (None, ""):
                out[name] = val
    if "make" in out or "model" in out:
        make, model = str(out.pop("make", "")), str(out.pop("model", ""))
        # most cameras repeat the maker in the model name ("NIKON CORPORATION" / "NIKON Z 6")
        out["camera"] = model if make.split(" ")[0].lower() in model.lower() else f"{make} {model}".strip()
    if "lens_model" in out:
        out["lens"] = str(out.pop("lens_model"))
    out.pop("lens_make", None)
    return out


def read_exif(primary: str, files: list[str]) -> dict[str, Any]:
    """EXIF metadata of a shot without analysing it, read from the same file the analysis uses."""
    src = Path(next((f for f in files if Path(f).suffix.lower() in JPEG_EXT), primary))
    try:
        if src.suffix.lower() in RAW_EXT:
            _, pil = load_image(src)
            return exif_metadata(pil)
        with Image.open(src) as img:        # reads the header only, no decoding
            return exif_metadata(img)
    except Exception:
        return {}


def _exif_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode("ascii", "replace")
    if isinstance(val, str):
        return val.strip("\x00 ")
    if isinstance(val, tuple):           # e.g. ISO as a 1-tuple
        return _exif_value(val[0]) if val else None
    try:
        f = float(val)                   # IFDRational, int
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if f != f:                           # 0/0 rationals are NaN
        return None
    return int(f) if isinstance(val, int) else round(f, 6)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
class UnsafePath(ValueError):
    """A path that would leave the library root."""


def safe_path(root: Path, rel: str) -> Path:
    """Resolve a library-root-relative path; reject anything that ends up outside ``root``.

    Absolute paths are rejected outright. ``..`` and symlinks are resolved first, so a symlink that points
    outside the root is rejected as well. The path need not exist.
    """
    if not rel or "\x00" in rel or Path(rel).is_absolute() or rel.startswith(("/", "\\")):
        raise UnsafePath(rel)
    base = root.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise UnsafePath(rel)
    return p


def inside_root(root: Path, files: list[str]) -> bool:
    """Every file of a shot resolves inside the library root (no symlink out of it)."""
    try:
        for f in files:
            safe_path(root, Path(f).relative_to(root).as_posix())
    except (UnsafePath, ValueError):
        return False
    return True


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

