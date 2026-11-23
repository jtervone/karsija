"""Image metrics, grouping of similar shots and verdicts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from karsija import previews
from karsija.library import capture_time, exif_metadata, load_image

GRID = 8  # sharpness tiles per axis
MIN_HIST = 0.9  # colour histogram correlation that lets a looser dHash match group

# Grouping and judging thresholds; the `settings` table overrides them
DEFAULT_SETTINGS: dict[str, float] = {
    "gap": 3.0,  # max seconds between consecutive shots of a group
    "hamming": 40,  # max dHash difference (0-256) of similar shots
    "abs_blur": 10.0,  # sharpness below this = blurry
    "rel_blur": 0.25,  # below this fraction of the event median = blurry
    "sim_keep": 0.0,  # keep a group member whose sharpness >= this x the best (0 = only the best)
}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    key: str  # path of the "primary" file (RAW if present)
    files: list[str]  # all files of this shot (RAW + JPEG sidecar)
    taken: float = 0.0  # unix time
    sharp: float = 0.0  # focus-region sharpness
    sharp_global: float = 0.0
    clip_hi: float = 0.0  # fraction of blown highlights
    clip_lo: float = 0.0  # fraction of crushed shadows
    dhash: str = ""
    hist: list[float] = field(default_factory=list)
    error: str = ""
    exif: dict[str, Any] = field(default_factory=dict)  # see library.exif_metadata
    # filled in by analysis
    group: int = -1
    verdict: str = ""  # best | keep | similar | blurry | error
    reason: str = ""


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
_DIR_KERNELS = [
    np.array([[0, 0, 0], [-1, 2, -1], [0, 0, 0]], np.float32),  # horizontal
    np.array([[0, -1, 0], [0, 2, 0], [0, -1, 0]], np.float32),  # vertical
    np.array([[-1, 0, 0], [0, 2, 0], [0, 0, -1]], np.float32),  # diagonal \
    np.array([[0, 0, -1], [0, 2, 0], [-1, 0, 0]], np.float32),  # diagonal /
]


def sharpness(gray: np.ndarray) -> tuple[float, float]:
    """
    Focus-region sharpness.

    For each tile, second-derivative energy is measured in four directions and
    the *weakest* direction is used: camera shake / motion blur smears detail
    along one direction, so a shaken frame is soft in at least one direction
    even if edges across the motion still look crisp.

    The score is the mean of the sharpest ~10 % of tiles, so a shallow
    depth-of-field shot with a sharp subject scores high even though most of
    the frame is soft, while a missed-focus or shaken frame has no sharp tiles.
    """
    g32 = gray.astype(np.float32)
    dirs = [cv2.filter2D(g32, -1, k) ** 2 for k in _DIR_KERNELS]
    h, w = gray.shape
    th, tw = h // GRID, w // GRID
    scores = []
    for y in range(GRID):
        for x in range(GRID):
            sl = (slice(y * th, (y + 1) * th), slice(x * tw, (x + 1) * tw))
            # skip flat tiles (sky, blown-out areas) – they say nothing about focus
            if gray[sl].std() < 4:
                continue
            scores.append(min(float(d[sl].mean()) for d in dirs))
    global_score = float(min(d.mean() for d in dirs))
    if not scores:
        return 0.0, global_score
    scores.sort(reverse=True)
    top = scores[: max(2, len(scores) // 10)]
    return float(np.mean(top)), global_score


def dhash(gray: np.ndarray, size: int = 16) -> str:
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    bits = np.packbits(diff.flatten())
    return bits.tobytes().hex()


def hamming(a: str, b: str) -> int:
    return int(
        np.unpackbits(np.frombuffer(bytes.fromhex(a), np.uint8) ^ np.frombuffer(bytes.fromhex(b), np.uint8)).sum()
    )


def color_hist(bgr: np.ndarray) -> list[float]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().round(5).tolist()


def hist_similarity(a: list[float], b: list[float]) -> float:
    return float(cv2.compareHist(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32), cv2.HISTCMP_CORREL))


def analyse_file(primary: str, files: list[str], preview: tuple[Path, str] | None = None) -> Shot:
    """Metrics of one shot. ``preview``: ``(cache dir, signature)``, also write its previews from the decoded image."""
    shot = Shot(key=primary, files=files)
    try:
        # Prefer an out-of-camera JPEG for analysis if one exists (faster, full res)
        src = previews.source_file(primary, files)
        bgr, pil = load_image(src)
        if preview is not None and pil is not None:
            try:
                previews.write_all(pil, *preview)
            except Exception:  # noqa: BLE001
                pass  # made on request instead; a full disk must not fail the analysis
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        shot.taken = capture_time(src, pil)
        shot.exif = exif_metadata(pil)
        shot.sharp, shot.sharp_global = sharpness(gray)
        shot.clip_hi = float((gray >= 250).mean())
        shot.clip_lo = float((gray <= 5).mean())
        shot.dhash = dhash(gray)
        shot.hist = color_hist(bgr)
    except Exception as e:  # noqa: BLE001
        shot.error = f"{type(e).__name__}: {e}"
        try:
            shot.taken = Path(primary).stat().st_mtime
        except OSError:
            pass
    return shot


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def group_shots(shots: list[Shot], max_gap: float, max_hamming: int, min_hist: float) -> None:
    """Chain consecutive shots that are close in time AND look alike."""
    ok = sorted((s for s in shots if not s.error), key=lambda s: s.taken)
    gid = 0
    prev: Shot | None = None
    for s in ok:
        if prev is not None and (s.taken - prev.taken) <= max_gap:
            hd = hamming(s.dhash, prev.dhash)
            hs = hist_similarity(s.hist, prev.hist)
            if hd <= max_hamming or (hd <= max_hamming * 2 and hs >= min_hist):
                s.group = prev.group
                prev = s
                continue
        gid += 1
        s.group = gid
        prev = s


def group_and_judge(shots: list[Shot], settings: dict[str, float]) -> None:
    """Group and judge the shots of one event with the given thresholds (see DEFAULT_SETTINGS)."""
    s = {**DEFAULT_SETTINGS, **settings}
    group_shots(shots, s["gap"], int(s["hamming"]), MIN_HIST)
    judge(shots, s["abs_blur"], s["rel_blur"], s["sim_keep"])


def judge(shots: list[Shot], abs_blur: float, rel_blur: float, sim_keep: float) -> None:
    valid = [s for s in shots if not s.error]
    for s in shots:
        if s.error:
            s.verdict, s.reason = "error", s.error
    if not valid:
        return
    median = float(np.median([s.sharp for s in valid]))

    groups: dict[int, list[Shot]] = {}
    for s in valid:
        groups.setdefault(s.group, []).append(s)

    for members in groups.values():
        members.sort(key=lambda s: (s.sharp, -(s.clip_hi + s.clip_lo)), reverse=True)
        best = members[0]
        for s in members:
            blurry = s.sharp < abs_blur or s.sharp < rel_blur * median
            if s is best:
                if blurry:
                    s.verdict = "blurry"
                    s.reason = f"paras ryhmästään mutta epäterävä ({s.sharp:.0f}, kansion mediaani {median:.0f})"
                else:
                    s.verdict = "best" if len(members) > 1 else "keep"
                    s.reason = f"{len(members)} samankaltaista" if len(members) > 1 else ""
            elif blurry:
                s.verdict = "blurry"
                s.reason = f"terävyys {s.sharp:.0f} (kansion mediaani {median:.0f})"
            elif sim_keep and s.sharp >= sim_keep * best.sharp:
                # nearly as good as the best – keep for a human to decide
                s.verdict = "keep"
                s.reason = f"lähes yhtä terävä kuin ryhmän paras ({s.sharp / best.sharp:.0%})"
            else:
                s.verdict = "similar"
                s.reason = f"ryhmän paras on terävämpi ({s.sharp / best.sharp:.0%} parhaasta)"
