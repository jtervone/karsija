"""Synthetic photo library: ROOT/2025/2025-06-01-test/ with known sharp, blurred and burst shots."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

W, H = 800, 600                 # below ANALYSIS_EDGE, so images are analysed at full size
T0 = datetime(2025, 6, 1, 12, 0, 0)
EVENT = Path("2025") / "2025-06-01-test"


def texture(seed: int, w: int = W, h: int = H) -> np.ndarray:
    """Grey image with large shapes (stable dHash) and crisp 4 px blocks (high-frequency detail)."""
    rng = np.random.default_rng(seed)
    low = cv2.resize(rng.uniform(-90, 90, (6, 8)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    blocks = rng.uniform(-60, 60, (h // 4 + 1, w // 4 + 1)).astype(np.float32)
    high = cv2.resize(blocks, ((w // 4 + 1) * 4, (h // 4 + 1) * 4), interpolation=cv2.INTER_NEAREST)[:h, :w]
    return np.clip(128 + low + high, 0, 255).astype(np.uint8)


def motion_blur(img: np.ndarray, length: int = 21) -> np.ndarray:
    kernel = np.zeros((length, length), np.float32)
    kernel[length // 2, :] = 1.0 / length          # horizontal camera shake
    return cv2.filter2D(img, -1, kernel)


def defocus(img: np.ndarray, sigma: float = 4.0) -> np.ndarray:
    return cv2.GaussianBlur(img, (0, 0), sigma)


def shallow_dof(img: np.ndarray) -> np.ndarray:
    """Sharp 2x2 block of analysis tiles in the centre, the rest heavily blurred."""
    out = defocus(img, 6.0)
    th, tw = H // 8, W // 8
    sl = (slice(3 * th, 5 * th), slice(3 * tw, 5 * tw))
    out[sl] = img[sl]
    return out


def save_jpeg(path: Path, gray: np.ndarray, taken: datetime) -> None:
    """Write a JPEG with EXIF capture time and the exposure fields of EXIF_META."""
    exif = Image.Exif()
    exif[271], exif[272] = "NIKON CORPORATION", "NIKON Z 6"
    sub = exif.get_ifd(0x8769)
    sub[36867] = taken.strftime("%Y:%m:%d %H:%M:%S")
    sub[37521] = f"{taken.microsecond // 10000:02d}"
    sub[33434] = IFDRational(1, 250)      # ExposureTime
    sub[33437] = IFDRational(28, 10)      # FNumber
    sub[34855] = 400                      # ISO
    sub[37386] = IFDRational(50, 1)       # FocalLength
    sub[42036] = "NIKKOR Z 50mm f/1.8 S"
    Image.fromarray(gray).convert("RGB").save(path, "JPEG", quality=95, exif=exif)


# karsija.library.exif_metadata() of every synthetic JPEG, without datetime_original
EXIF_META = {"camera": "NIKON Z 6", "exposure_time": 0.004, "f_number": 2.8, "iso": 400, "focal_length": 50.0,
             "lens": "NIKKOR Z 50mm f/1.8 S"}


@pytest.fixture(scope="session", autouse=True)
def preview_cache_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Previews go to a temporary cache directory, never to ~/.cache."""
    home = tmp_path_factory.mktemp("cache")
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(home)
    yield home
    if old is None:
        del os.environ["XDG_CACHE_HOME"]
    else:
        os.environ["XDG_CACHE_HOME"] = old


class FakeRaw:
    def __init__(self, thumb: SimpleNamespace | None, rgb: np.ndarray):
        self.thumb, self.rgb = thumb, rgb

    def __enter__(self) -> FakeRaw:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_thumb(self) -> SimpleNamespace:
        if self.thumb is None:
            raise RuntimeError("no thumbnail")
        return self.thumb

    def postprocess(self, **kwargs: Any) -> np.ndarray:
        return self.rgb


def fake_rawpy(thumb: SimpleNamespace | None, rgb: np.ndarray, opened: list[str]) -> SimpleNamespace:
    def imread(path: str) -> FakeRaw:
        opened.append(path)
        return FakeRaw(thumb, rgb)

    return SimpleNamespace(imread=imread, ThumbFormat=SimpleNamespace(JPEG="jpeg", BITMAP="bitmap"))


def jpeg_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=95)
    return buf.getvalue()


@dataclass
class Library:
    root: Path
    event: Path

    def path(self, name: str) -> Path:
        return self.event / name


# name -> expected verdict with default thresholds
EXPECTED = {
    "sharp.jpg": "keep",
    "motion.jpg": "blurry",
    "defocus.jpg": "blurry",
    "shallow_dof.jpg": "keep",
    "DSC_0100.NEF": "keep",
}
BURST = [f"burst_{i}.jpg" for i in range(5)]


@pytest.fixture(scope="session")
def library(tmp_path_factory: pytest.TempPathFactory) -> Library:
    root = tmp_path_factory.mktemp("lib")
    event = root / EVENT
    event.mkdir(parents=True)

    # single shots, a minute apart so they never group with each other
    save_jpeg(event / "sharp.jpg", texture(1), T0)
    save_jpeg(event / "motion.jpg", motion_blur(texture(2)), T0 + timedelta(minutes=1))
    save_jpeg(event / "defocus.jpg", defocus(texture(3)), T0 + timedelta(minutes=2))
    save_jpeg(event / "shallow_dof.jpg", shallow_dof(texture(4)), T0 + timedelta(minutes=3))

    # burst: 5 frames 0.3 s apart, each shifted by a few pixels
    big = texture(5, W + 20, H + 20)
    for i, name in enumerate(BURST):
        frame = np.ascontiguousarray(big[2 * i : 2 * i + H, 3 * i : 3 * i + W])
        save_jpeg(event / name, frame, T0 + timedelta(minutes=4, milliseconds=300 * i))

    # RAW+JPEG pair: the .nef is a placeholder, reading it goes through rawpy (monkeypatched in tests)
    save_jpeg(event / "DSC_0100.JPG", texture(6), T0 + timedelta(minutes=5))
    (event / "DSC_0100.NEF").write_bytes(b"not a real raw file")

    return Library(root=root, event=event)
