from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import fake_rawpy, jpeg_bytes, texture
from PIL import Image, ImageOps

from karsija import library as library_mod
from karsija import previews
from karsija.analysis import analyse_file
from karsija.library import RawpyMissing, load_image
from karsija.previews import SIZES, NoAccess, PreviewCache, UnreadableImage, default_cache_dir, preview_path
from karsija.store import Store


def rotated_jpeg(path: Path, orientation: int = 6) -> None:
    """1200x800 as stored: top half white, bottom half black. Orientation 6 = shown rotated 90 degrees clockwise."""
    img = np.zeros((800, 1200, 3), np.uint8)
    img[:400] = 255
    exif = Image.Exif()
    exif[0x0112] = orientation
    Image.fromarray(img).save(path, "JPEG", quality=90, exif=exif)


def assert_upright(preview: Path, edge: int) -> None:
    """The rotated test image shown upright: portrait, the white half on the right."""
    with Image.open(preview) as img:
        assert img.size == (round(edge * 2 / 3), edge)
        assert img.getexif().get(0x0112, 1) == 1          # nothing left for a viewer to rotate
        px = np.asarray(img.convert("L"), np.float32)
    w = px.shape[1]
    assert px[:, : w // 2 - 4].mean() < 20 and px[:, w // 2 + 4 :].mean() > 235


def test_default_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "karsija" / "previews"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert default_cache_dir() == Path.home() / ".cache" / "karsija" / "previews"


def test_preview_path_hashes_the_signature(tmp_path: Path) -> None:
    p = preview_path(tmp_path, "v4|/a.jpg:10:20", "grid")
    assert p.parent == tmp_path and p.name.endswith("-grid.jpg") and len(p.stem) == 32 + len("-grid")
    assert preview_path(tmp_path, "v4|/a.jpg:10:21", "grid") != p


@pytest.mark.parametrize("size", ["grid", "full"])
def test_rotated_exif_gives_an_upright_preview(tmp_path: Path, size: str) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    dest = PreviewCache(tmp_path / "cache").get(str(src), [str(src)], "sig", size)
    assert_upright(dest, min(SIZES[size], 1200))                  # never scaled up


def test_preview_matches_exif_transpose(tmp_path: Path) -> None:
    """Every EXIF orientation turns out as Pillow's own exif_transpose does it."""
    for orientation in range(1, 9):
        src = tmp_path / f"o{orientation}.jpg"
        exif = Image.Exif()
        exif[0x0112] = orientation
        Image.fromarray(np.stack([texture(orientation, 120, 80)] * 3, axis=-1)).save(src, "JPEG", exif=exif)
        dest = previews.make(str(src), [str(src)], tmp_path / "cache", str(orientation), "grid")
        with Image.open(src) as orig, Image.open(dest) as got:
            want = np.asarray(ImageOps.exif_transpose(orig).convert("L"), np.float32)
            have = np.asarray(got.convert("L"), np.float32)
        assert have.shape == want.shape
        assert np.abs(have - want).mean() < 8, orientation


def test_analysis_writes_upright_previews_of_every_size(tmp_path: Path) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    cache = tmp_path / "cache"
    sig = Store.signature([str(src)])
    shot = analyse_file(str(src), [str(src)], (cache, sig))
    assert not shot.error
    assert_upright(preview_path(cache, sig, "grid"), 400)
    assert_upright(preview_path(cache, sig, "full"), 1200)


def test_analysis_survives_a_failing_preview(tmp_path: Path) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    (tmp_path / "file").write_bytes(b"")
    shot = analyse_file(str(src), [str(src)], (tmp_path / "file", "sig"))     # cache dir cannot be created
    assert not shot.error and shot.sharp >= 0


def test_analysis_does_not_resample_existing_previews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    cache, sig = tmp_path / "cache", Store.signature([str(src)])
    analyse_file(str(src), [str(src)], (cache, sig))
    rendered: list[str] = []
    render = previews.render

    def counting_render(pil: Image.Image, size: str, turn: int | None = None) -> Image.Image:
        rendered.append(size)
        return render(pil, size, turn)

    monkeypatch.setattr(previews, "render", counting_render)
    analyse_file(str(src), [str(src)], (cache, sig))
    assert rendered == []
    preview_path(cache, sig, "grid").unlink()
    analyse_file(str(src), [str(src)], (cache, sig))
    assert rendered == ["full", "grid"] and preview_path(cache, sig, "grid").is_file()


RGB = np.stack([texture(7, 1600, 1200)] * 3, axis=-1)


def test_raw_uses_the_embedded_thumbnail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "DSC_0001.NEF"
    raw.write_bytes(b"not a real raw file")
    opened: list[str] = []
    monkeypatch.setattr(library_mod, "rawpy", fake_rawpy(SimpleNamespace(format="jpeg", data=jpeg_bytes(RGB)),
                                                         np.zeros((10, 10, 3), np.uint8), opened))
    cache = PreviewCache(tmp_path / "cache")
    with Image.open(cache.get(str(raw), [str(raw)], "sig", "grid")) as grid:
        assert grid.size == (400, 300)
        got = np.asarray(grid.convert("L"), np.float32)
    with Image.open(cache.get(str(raw), [str(raw)], "sig", "full")) as full:
        assert full.size == (1600, 1200)                # the thumbnail at full size, not the analysis 1024 px
    assert opened == [str(raw)] * 2
    want = np.asarray(Image.fromarray(RGB).convert("L").resize((400, 300), Image.Resampling.LANCZOS), np.float32)
    assert np.abs(got - want).mean() < 8                # the thumbnail, not postprocess() (all black)


def portrait_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flip: int, thumb_orientation: int | None) -> Path:
    """A RAW whose embedded preview is stored as the rotated test image (white half on top)."""
    jpg = tmp_path / "thumb.jpg"
    rotated_jpeg(jpg, thumb_orientation or 1)
    data = jpg.read_bytes()
    if thumb_orientation is None:
        data = jpeg_bytes(np.asarray(Image.open(jpg)))            # no EXIF at all
    raw = tmp_path / "DSC_0003.NEF"
    raw.write_bytes(b"not a real raw file")
    monkeypatch.setattr(library_mod, "rawpy", fake_rawpy(SimpleNamespace(format="jpeg", data=data), RGB, [], flip))
    return raw


@pytest.mark.parametrize("thumb_orientation", [None, 1])
def test_raw_preview_is_turned_by_the_raw_orientation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      thumb_orientation: int | None) -> None:
    raw = portrait_raw(tmp_path, monkeypatch, 6, thumb_orientation)        # LibRaw 6 = 90 degrees clockwise
    assert_upright(PreviewCache(tmp_path / "cache").get(str(raw), [str(raw)], "sig", "grid"), 400)
    bgr, _ = load_image(raw)
    assert bgr.shape[:2] == (1024, 682)                                    # analysed upright as well


def test_raw_preview_keeps_its_own_orientation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = portrait_raw(tmp_path, monkeypatch, 5, 6)               # the preview's own EXIF wins over the RAW's
    assert_upright(PreviewCache(tmp_path / "cache").get(str(raw), [str(raw)], "sig", "grid"), 400)


def test_raw_without_rawpy_and_broken_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw, broken = tmp_path / "DSC_0004.NEF", tmp_path / "broken.jpg"
    raw.write_bytes(b"not a real raw file")
    broken.write_bytes(b"not an image")
    monkeypatch.setattr(library_mod, "rawpy", None)
    cache = PreviewCache(tmp_path / "cache")
    with pytest.raises(RawpyMissing):
        cache.get(str(raw), [str(raw)], "a", "grid")
    with pytest.raises(UnreadableImage):
        cache.get(str(broken), [str(broken)], "b", "grid")
    with pytest.raises(FileNotFoundError):
        cache.get(str(tmp_path / "gone.jpg"), [str(tmp_path / "gone.jpg")], "c", "grid")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_permissions_are_not_a_broken_file(tmp_path: Path) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    src.chmod(0)
    try:
        with pytest.raises(NoAccess):
            PreviewCache(tmp_path / "cache").get(str(src), [str(src)], "sig", "grid")
    finally:
        src.chmod(0o644)


def test_raw_jpeg_pair_prefers_the_jpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw, jpg = tmp_path / "DSC_0002.NEF", tmp_path / "DSC_0002.JPG"
    raw.write_bytes(b"not a real raw file")
    rotated_jpeg(jpg)
    opened: list[str] = []
    monkeypatch.setattr(library_mod, "rawpy", fake_rawpy(None, RGB, opened))
    dest = PreviewCache(tmp_path / "cache").get(str(raw), [str(jpg), str(raw)], "sig", "grid")
    assert opened == []
    assert_upright(dest, 400)


def test_cached_preview_is_not_made_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    cache = PreviewCache(tmp_path / "cache")
    made: list[str] = []
    make = previews.make
    monkeypatch.setattr(previews, "make", lambda *a: made.append(a[-1]) or make(*a))
    first = cache.get(str(src), [str(src)], Store.signature([str(src)]), "grid")
    again = cache.get(str(src), [str(src)], Store.signature([str(src)]), "grid")
    assert first == again and made == ["grid"]

    st = src.stat()
    os.utime(src, (st.st_atime, st.st_mtime + 10))       # same bytes, new mtime: a new signature
    changed = cache.get(str(src), [str(src)], Store.signature([str(src)]), "grid")
    assert changed != first and changed.exists() and made == ["grid", "grid"]


def test_concurrent_requests_make_the_preview_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "rot.jpg"
    rotated_jpeg(src)
    cache = PreviewCache(tmp_path / "cache")
    made: list[str] = []
    make = previews.make

    def slow_make(*args: object) -> Path:
        made.append(str(args[-1]))
        time.sleep(0.2)
        return make(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(previews, "make", slow_make)
    results: list[Path] = []
    threads = [threading.Thread(target=lambda: results.append(cache.get(str(src), [str(src)], "sig", "full")))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert made == ["full"] and len(set(results)) == 1 and len(results) == 8
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [results[0].name]     # no temporary files left
