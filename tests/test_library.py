from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from conftest import EVENT, T0, Library, texture
from PIL import Image

from karsija import library as library_mod
from karsija.library import capture_time, collect_shots, find_event_dirs, load_image


def test_find_event_dirs(library: Library) -> None:
    (library.root / "misc").mkdir(exist_ok=True)
    (library.root / "2025" / "not-an-event").mkdir(exist_ok=True)
    assert find_event_dirs(library.root) == [library.root / EVENT]


def test_collect_shots_pairs_raw_and_jpeg(library: Library) -> None:
    shots = dict(collect_shots(library.event))
    assert len(shots) == 10
    raw = str(library.path("DSC_0100.NEF"))
    assert shots[raw] == [str(library.path("DSC_0100.JPG")), raw]
    assert str(library.path("DSC_0100.JPG")) not in shots


def test_capture_time_uses_exif_subseconds(library: Library) -> None:
    path = library.path("burst_1.jpg")
    with Image.open(path) as pil:
        taken = capture_time(path, pil)
    assert taken == pytest.approx(T0.timestamp() + 4 * 60 + 0.3)


def test_capture_time_falls_back_to_mtime(library: Library) -> None:
    path = library.path("sharp.jpg")
    assert capture_time(path, None) == path.stat().st_mtime


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


RGB = np.stack([texture(7, 1600, 1200)] * 3, axis=-1)


@pytest.mark.parametrize(
    "thumb",
    [
        SimpleNamespace(format="jpeg", data=jpeg_bytes(RGB)),
        SimpleNamespace(format="bitmap", data=RGB),
        None,  # no embedded thumbnail: falls back to postprocess()
    ],
    ids=["jpeg-thumb", "bitmap-thumb", "postprocess"],
)
def test_load_image_raw(library: Library, monkeypatch: pytest.MonkeyPatch, thumb: SimpleNamespace | None) -> None:
    opened: list[str] = []
    monkeypatch.setattr(library_mod, "rawpy", fake_rawpy(thumb, RGB, opened))
    raw = library.path("DSC_0100.NEF")
    bgr, pil = load_image(raw)
    assert opened == [str(raw)]
    assert bgr.shape == (768, 1024, 3)            # long edge scaled down to ANALYSIS_EDGE
    assert pil is not None and pil.size == (1600, 1200)


def test_load_image_raw_without_rawpy(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "rawpy", None)
    with pytest.raises(RuntimeError, match="rawpy"):
        load_image(library.path("DSC_0100.NEF"))


def test_load_image_jpeg(library: Library) -> None:
    bgr, pil = load_image(Path(library.path("sharp.jpg")))
    assert bgr.shape == (600, 800, 3)
    assert pil is not None
