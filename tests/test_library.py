from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import EVENT, EXIF_META, T0, Library, fake_rawpy, jpeg_bytes, texture
from PIL import Image

from karsija import library as library_mod
from karsija.library import (
    UnsafePath,
    capture_time,
    collect_shots,
    exif_metadata,
    find_event_dirs,
    load_image,
    read_exif,
    safe_path,
)


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
    assert bgr.shape == (768, 1024, 3)  # long edge scaled down to ANALYSIS_EDGE
    assert pil is not None and pil.size == (1600, 1200)


def test_load_image_raw_without_rawpy(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "rawpy", None)
    with pytest.raises(RuntimeError, match="rawpy"):
        load_image(library.path("DSC_0100.NEF"))


def test_load_image_jpeg(library: Library) -> None:
    bgr, pil = load_image(Path(library.path("sharp.jpg")))
    assert bgr.shape == (600, 800, 3)
    assert pil is not None


# --------------------------------------------------------------------------- #
# EXIF and safe paths
# --------------------------------------------------------------------------- #
def test_exif_metadata(library: Library) -> None:
    with Image.open(library.path("sharp.jpg")) as img:
        assert exif_metadata(img) == {**EXIF_META, "datetime_original": "2025:06:01 12:00:00"}
    assert exif_metadata(None) == {}
    assert exif_metadata(Image.new("RGB", (4, 4))) == {}


def test_exif_camera_name(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[271], exif[272] = "Canon", "EOS R5"
    Image.new("RGB", (4, 4)).save(tmp_path / "a.jpg", exif=exif)
    with Image.open(tmp_path / "a.jpg") as img:
        assert exif_metadata(img) == {"camera": "Canon EOS R5"}


def test_read_exif_prefers_the_jpeg(library: Library) -> None:
    raw, jpg = str(library.path("DSC_0100.NEF")), str(library.path("DSC_0100.JPG"))
    assert read_exif(raw, [jpg, raw])["iso"] == 400
    assert read_exif(raw, [raw]) == {}  # placeholder NEF: unreadable, no metadata


def test_safe_path(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / "2025").mkdir(parents=True)
    (tmp_path / "out.jpg").write_bytes(b"x")
    (root / "2025" / "link.jpg").symlink_to(tmp_path / "out.jpg")
    (root / "2025" / "inner.jpg").symlink_to(root / "2025" / "a.jpg")
    assert safe_path(root, "2025/a.jpg") == root.resolve() / "2025" / "a.jpg"
    assert safe_path(root, "2025/../2025/./a.jpg") == root.resolve() / "2025" / "a.jpg"
    assert safe_path(root, "2025/inner.jpg") == root.resolve() / "2025" / "a.jpg"
    assert safe_path(root, ".") == root.resolve()
    for bad in (
        "",
        "..",
        "../out.jpg",
        "2025/../../out.jpg",
        "/etc/passwd",
        str(root / "2025"),
        "2025/link.jpg",
        "a\x00b",
    ):
        with pytest.raises(UnsafePath):
            safe_path(root, bad)
