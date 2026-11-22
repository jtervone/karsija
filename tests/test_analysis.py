from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from conftest import BURST, EXPECTED, Library

from karsija.analysis import Shot, analyse_file, dhash, group_shots, hamming, judge, sharpness
from karsija.library import collect_shots, load_image

DEFAULTS = {"abs_blur": 10.0, "rel_blur": 0.25, "sim_keep": 0.0}


@pytest.fixture(scope="session")
def analysed(library: Library) -> dict[str, Shot]:
    """Every shot of the synthetic event, keyed by the primary file name."""
    shots = [analyse_file(primary, files) for primary, files in collect_shots(library.event)]
    return {Path(s.key).name: s for s in shots}


def gray(library: Library, name: str) -> np.ndarray:
    bgr, _ = load_image(library.path(name))
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def test_sharpness_ranking(library: Library) -> None:
    score = {n: sharpness(gray(library, n))[0] for n in ("sharp.jpg", "shallow_dof.jpg", "motion.jpg", "defocus.jpg")}
    assert score["sharp.jpg"] > score["shallow_dof.jpg"]
    assert score["shallow_dof.jpg"] > score["motion.jpg"]
    assert score["shallow_dof.jpg"] > score["defocus.jpg"]


def test_dhash_and_hamming(library: Library) -> None:
    a, b = dhash(gray(library, "burst_0.jpg")), dhash(gray(library, "burst_1.jpg"))
    assert len(a) == 64                       # 16x16 bits
    assert hamming(a, a) == 0
    assert hamming(a, b) <= 40
    assert hamming(a, dhash(gray(library, "sharp.jpg"))) > 40


def test_analyse_file_records_errors(tmp_path: Path) -> None:
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not a jpeg")
    shot = analyse_file(str(bad), [str(bad)])
    assert shot.error.startswith("UnidentifiedImageError")
    assert shot.taken == bad.stat().st_mtime


def test_burst_forms_one_group(analysed: dict[str, Shot]) -> None:
    shots = list(analysed.values())
    group_shots(shots, 3.0, 40, 0.9)
    burst_groups = {analysed[n].group for n in BURST}
    assert len(burst_groups) == 1
    others = [s.group for n, s in analysed.items() if n not in BURST]
    assert len(set(others)) == len(others)
    assert burst_groups.isdisjoint(others)


def test_verdicts_on_synthetic_library(analysed: dict[str, Shot]) -> None:
    shots = list(analysed.values())
    group_shots(shots, 3.0, 40, 0.9)
    judge(shots, **DEFAULTS)
    for name, verdict in EXPECTED.items():
        assert analysed[name].verdict == verdict, (name, analysed[name].sharp)
    burst = sorted(analysed[n].verdict for n in BURST)
    assert burst == ["best", "similar", "similar", "similar", "similar"]


# --------------------------------------------------------------------------- #
# judge: every branch on hand-made shots
# --------------------------------------------------------------------------- #
def shot(name: str, sharp: float, group: int, error: str = "") -> Shot:
    return Shot(key=name, files=[name], sharp=sharp, group=group, error=error)


def verdicts(shots: list[Shot]) -> dict[str, str]:
    return {s.key: s.verdict for s in shots}


def test_judge_best_similar_keep() -> None:
    shots = [shot("a", 100, 1), shot("b", 90, 1), shot("c", 100, 2)]
    judge(shots, **DEFAULTS)
    assert verdicts(shots) == {"a": "best", "b": "similar", "c": "keep"}
    assert shots[0].reason == "2 samankaltaista"
    assert shots[1].reason == "ryhmän paras on terävämpi (90% parhaasta)"
    assert shots[2].reason == ""


def test_judge_sim_keep() -> None:
    shots = [shot("a", 100, 1), shot("b", 96, 1), shot("c", 80, 1)]
    judge(shots, **{**DEFAULTS, "sim_keep": 0.95})
    assert verdicts(shots) == {"a": "best", "b": "keep", "c": "similar"}
    assert shots[1].reason == "lähes yhtä terävä kuin ryhmän paras (96%)"


def test_judge_blurry() -> None:
    # median 100: "b" is below rel_blur * median, "d" below abs_blur, "c" is the blurry best of its group
    shots = [shot("a", 100, 1), shot("b", 20, 1), shot("c", 20, 2), shot("d", 5, 3), shot("e", 100, 4),
             shot("f", 100, 5), shot("g", 100, 6)]
    judge(shots, **DEFAULTS)
    assert verdicts(shots) == {"a": "best", "b": "blurry", "c": "blurry", "d": "blurry",
                               "e": "keep", "f": "keep", "g": "keep"}
    assert shots[1].reason == "terävyys 20 (kansion mediaani 100)"
    assert shots[2].reason == "paras ryhmästään mutta epäterävä (20, kansion mediaani 100)"


def test_judge_error() -> None:
    shots = [shot("a", 100, 1), shot("x", 0, -1, error="OSError: boom")]
    judge(shots, **DEFAULTS)
    assert verdicts(shots) == {"a": "keep", "x": "error"}
    assert shots[1].reason == "OSError: boom"


def test_judge_only_errors() -> None:
    shots = [shot("x", 0, -1, error="OSError: boom")]
    judge(shots, **DEFAULTS)
    assert verdicts(shots) == {"x": "error"}
