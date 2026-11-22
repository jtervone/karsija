#!/usr/bin/env python3
"""
karsija.py – photo culling helper.

Walks a library laid out as  ROOT/YYYY/YYYY-MM-DD[-event-name]/ ,
scores every photo for sharpness and exposure, groups near-identical
shots (bursts, repeats) inside each event folder, picks the best of
each group and writes an HTML review report.

Nothing is ever deleted. Optional actions:
  --write-xmp        write rating/label to XMP sidecars (only where none exists)
  --move             move rejects into a "_karsitut" subfolder (reversible)

Requirements:  pip install opencv-python-headless numpy pillow rawpy
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ExifTags

try:
    import rawpy
except ImportError:  # RAW support is optional
    rawpy = None

JPEG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
RAW_EXT = {".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".srw"}
EVENT_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
REJECT_DIR = "_karsitut"
ANALYSIS_EDGE = 1024          # long edge used for all measurements
GRID = 8                      # sharpness tiles per axis
CACHE_VERSION = 4


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    key: str                       # path of the "primary" file (RAW if present)
    files: list[str]               # all files of this shot (RAW + JPEG sidecar)
    taken: float = 0.0             # unix time
    sharp: float = 0.0             # focus-region sharpness
    sharp_global: float = 0.0
    clip_hi: float = 0.0           # fraction of blown highlights
    clip_lo: float = 0.0           # fraction of crushed shadows
    dhash: str = ""
    hist: list[float] = field(default_factory=list)
    error: str = ""
    # filled in by analysis
    group: int = -1
    verdict: str = ""              # best | keep | similar | blurry | error
    reason: str = ""


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #
def load_image(path: Path) -> tuple[np.ndarray, Image.Image | None]:
    """Return BGR image (downscaled) and the PIL image for EXIF."""
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
# Metrics
# --------------------------------------------------------------------------- #
_DIR_KERNELS = [
    np.array([[0, 0, 0], [-1, 2, -1], [0, 0, 0]], np.float32),   # horizontal
    np.array([[0, -1, 0], [0, 2, 0], [0, -1, 0]], np.float32),   # vertical
    np.array([[-1, 0, 0], [0, 2, 0], [0, 0, -1]], np.float32),   # diagonal \
    np.array([[0, 0, -1], [0, 2, 0], [-1, 0, 0]], np.float32),   # diagonal /
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
    return int(np.unpackbits(np.frombuffer(bytes.fromhex(a), np.uint8) ^
                             np.frombuffer(bytes.fromhex(b), np.uint8)).sum())


def color_hist(bgr: np.ndarray) -> list[float]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().round(5).tolist()


def hist_similarity(a: list[float], b: list[float]) -> float:
    return float(cv2.compareHist(np.float32(a), np.float32(b), cv2.HISTCMP_CORREL))


def analyse_file(primary: str, files: list[str]) -> Shot:
    shot = Shot(key=primary, files=files)
    try:
        # Prefer an out-of-camera JPEG for analysis if one exists (faster, full res)
        src = next((f for f in files if Path(f).suffix.lower() in JPEG_EXT), primary)
        bgr, pil = load_image(Path(src))
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        shot.taken = capture_time(Path(src), pil)
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
# Cache
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS shots (key TEXT PRIMARY KEY, sig TEXT, data TEXT)")

    @staticmethod
    def signature(files: list[str]) -> str:
        parts = []
        for f in files:
            st = os.stat(f)
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        return f"v{CACHE_VERSION}|" + "|".join(sorted(parts))

    def get(self, key: str, sig: str) -> Shot | None:
        row = self.db.execute("SELECT sig, data FROM shots WHERE key=?", (key,)).fetchone()
        if row and row[0] == sig:
            return Shot(**json.loads(row[1]))
        return None

    def put(self, shot: Shot, sig: str) -> None:
        d = asdict(shot)
        for k in ("group", "verdict", "reason"):
            d.pop(k)
        self.db.execute("REPLACE INTO shots VALUES (?,?,?)", (shot.key, sig, json.dumps(d)))

    def commit(self):
        self.db.commit()


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def find_event_dirs(root: Path) -> list[Path]:
    dirs = []
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
    out = []
    for stem, paths in sorted(by_stem.items()):
        raw = [p for p in paths if p.suffix.lower() in RAW_EXT]
        primary = raw[0] if raw else paths[0]
        out.append((str(primary), [str(p) for p in sorted(paths)]))
    return out


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


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
RATING = {"best": 5, "keep": 3, "similar": 1, "blurry": -1}
LABEL = {"best": "Green", "keep": "", "similar": "Yellow", "blurry": "Red"}

XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmp:Rating="{rating}"{label}/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_xmp(shot: Shot) -> str:
    if shot.verdict not in RATING:
        return "skip"
    primary = Path(shot.key)
    # darktable uses name.ext.xmp, digiKam/Lightroom name.xmp – check both
    candidates = [primary.with_name(primary.name + ".xmp"), primary.with_suffix(".xmp")]
    if any(c.exists() for c in candidates):
        return "exists"
    label = f'\n    xmp:Label="{LABEL[shot.verdict]}"' if LABEL[shot.verdict] else ""
    primary.with_suffix(".xmp").write_text(
        XMP_TEMPLATE.format(rating=RATING[shot.verdict], label=label), encoding="utf-8")
    return "written"


def move_rejects(shot: Shot, kinds: set[str]) -> bool:
    if shot.verdict not in kinds:
        return False
    for f in shot.files:
        p = Path(f)
        dest_dir = p.parent / REJECT_DIR
        dest_dir.mkdir(exist_ok=True)
        for extra in (p, p.with_name(p.name + ".xmp")):
            if extra.exists():
                shutil.move(str(extra), str(dest_dir / extra.name))
    xmp = Path(shot.key).with_suffix(".xmp")
    if xmp.exists():
        shutil.move(str(xmp), str(xmp.parent / REJECT_DIR / xmp.name))
    return True


def make_thumb(shot: Shot, out_dir: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", shot.key.strip("/")) + ".jpg"
    dest = out_dir / name
    if not dest.exists():
        try:
            src = next((f for f in shot.files if Path(f).suffix.lower() in JPEG_EXT), shot.key)
            bgr, _ = load_image(Path(src))
            h, w = bgr.shape[:2]
            s = 360 / max(h, w)
            bgr = cv2.resize(bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(dest), bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception:
            return ""
    return f"thumbs/{name}"


VERDICT_FI = {"best": "Paras", "keep": "Pidä", "similar": "Samankaltainen",
              "blurry": "Epäterävä", "error": "Virhe"}


def write_report(report_dir: Path, results: dict[Path, list[Shot]], root: Path, workers: int) -> Path:
    thumbs = report_dir / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)

    all_shots = [s for shots in results.values() for s in shots]
    with cf.ThreadPoolExecutor(workers) as ex:
        thumb_of = dict(zip((s.key for s in all_shots), ex.map(lambda s: make_thumb(s, thumbs), all_shots)))

    totals = {k: 0 for k in VERDICT_FI}
    for s in all_shots:
        totals[s.verdict] = totals.get(s.verdict, 0) + 1

    parts = [f"""<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">
<title>Karsintaraportti</title><style>
body{{font-family:system-ui,sans-serif;background:#16181c;color:#ddd;margin:0;padding:1.5rem}}
h1{{margin-top:0}} h2{{border-bottom:1px solid #333;padding-bottom:.3rem;margin-top:2.5rem}}
.summary span{{margin-right:1.2rem}}
.filters label{{margin-right:1rem;cursor:pointer}}
.group{{display:flex;flex-wrap:wrap;gap:.6rem;padding:.6rem;margin:.6rem 0;border-radius:8px;background:#1e2126}}
.group.multi{{background:#23272e;outline:1px solid #3a4150}}
.card{{width:200px;font-size:.75rem;border-radius:6px;overflow:hidden;background:#111;border:3px solid transparent}}
.card img{{width:100%;height:150px;object-fit:contain;background:#000;display:block;cursor:zoom-in}}
.card .meta{{padding:.35rem .45rem}} .card .name{{word-break:break-all;color:#aaa}}
.best{{border-color:#3fb950}} .keep{{border-color:#4a5160}} .similar{{border-color:#d29922;opacity:.7}}
.blurry{{border-color:#f85149;opacity:.6}} .error{{border-color:#a371f7}}
.badge{{font-weight:600}} .best .badge{{color:#3fb950}} .similar .badge{{color:#d29922}}
.blurry .badge{{color:#f85149}} .error .badge{{color:#a371f7}}
body.hide-keep .card.keep, body.hide-best .card.best{{display:none}}
</style></head><body>
<h1>Karsintaraportti</h1>
<p>{html.escape(str(root))} – {datetime.now():%Y-%m-%d %H:%M}</p>
<p class="summary">""" + "".join(
        f"<span>{VERDICT_FI[k]}: <b>{v}</b></span>" for k, v in totals.items() if v) + """</p>
<p class="filters">
<label><input type="checkbox" onchange="document.body.classList.toggle('hide-keep',this.checked)"> piilota "Pidä"</label>
<label><input type="checkbox" onchange="document.body.classList.toggle('hide-best',this.checked)"> piilota "Paras"</label>
</p>"""]

    for ev, shots in results.items():
        flagged = sum(s.verdict in ("blurry", "similar") for s in shots)
        parts.append(f"<h2>{html.escape(str(ev.relative_to(root)))} "
                     f"<small>({len(shots)} kuvaa, {flagged} karsintaehdokasta)</small></h2>")
        groups: dict[int, list[Shot]] = {}
        for s in sorted(shots, key=lambda s: s.taken):
            groups.setdefault(s.group, []).append(s)
        for gid, members in groups.items():
            multi = " multi" if len(members) > 1 else ""
            parts.append(f'<div class="group{multi}">')
            for s in members:
                t = thumb_of.get(s.key, "")
                full = Path(next((f for f in s.files if Path(f).suffix.lower() in JPEG_EXT), s.key)).as_uri()
                img = f'<a href="{full}" target="_blank"><img src="{t}" loading="lazy"></a>' if t else ""
                parts.append(
                    f'<div class="card {s.verdict}">{img}<div class="meta">'
                    f'<span class="badge">{VERDICT_FI.get(s.verdict, s.verdict)}</span> '
                    f'· terävyys {s.sharp:.0f}<br>'
                    f'<span class="name">{html.escape(Path(s.key).name)}</span><br>'
                    f'{html.escape(s.reason)}</div></div>')
            parts.append("</div>")
    parts.append("</body></html>")
    out = report_dir / "index.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def write_csv(report_dir: Path, results: dict[Path, list[Shot]]) -> Path:
    import csv
    out = report_dir / "tulokset.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kansio", "tiedosto", "tuomio", "ryhmä", "terävyys", "terävyys_koko_kuva",
                    "puhki_%", "musta_%", "aika", "syy"])
        for ev, shots in results.items():
            for s in sorted(shots, key=lambda s: s.taken):
                w.writerow([ev.name, Path(s.key).name, s.verdict, s.group, f"{s.sharp:.1f}",
                            f"{s.sharp_global:.1f}", f"{s.clip_hi * 100:.2f}", f"{s.clip_lo * 100:.2f}",
                            datetime.fromtimestamp(s.taken).isoformat(timespec="seconds") if s.taken else "",
                            s.reason])
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Etsi epäterävät kuvat ja valitse samankaltaisista paras.")
    ap.add_argument("root", type=Path, help="kuvakirjaston juuri (sisältää YYYY-kansiot)")
    ap.add_argument("--only", default="", help="käsittele vain kansiot joiden polku sisältää tämän (esim. 2025 tai 2025-06)")
    ap.add_argument("--report", type=Path, default=Path("karsintaraportti"), help="raporttikansio")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--gap", type=float, default=3.0, help="max sekunnit peräkkäisten kuvien välillä samassa ryhmässä")
    ap.add_argument("--hamming", type=int, default=40, help="max dHash-ero (0-256) samankaltaisille")
    ap.add_argument("--abs-blur", type=float, default=10.0, help="tätä pienempi terävyys = epäterävä")
    ap.add_argument("--rel-blur", type=float, default=0.25, help="alle tämä osuus kansion mediaanista = epäterävä")
    ap.add_argument("--sim-keep", type=float, default=0.0,
                    help="pidä ryhmän jäsen myös, jos terävyys ≥ tämä × paras (esim. 0.95). Oletus: vain paras pidetään")
    ap.add_argument("--write-xmp", action="store_true", help="kirjoita arvio XMP-sivutiedostoon (ei ylikirjoita)")
    ap.add_argument("--move", default="", help="siirrä _karsitut-alikansioon: blurry,similar")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Kansiota ei löydy: {root}", file=sys.stderr)
        return 1
    events = [e for e in find_event_dirs(root) if args.only in str(e.relative_to(root))]
    if not events:
        print("Ei YYYY/YYYY-MM-DD* -kansioita.", file=sys.stderr)
        return 1

    args.report.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.report / "cache.sqlite")

    jobs: list[tuple[Path, str, list[str], str]] = []
    results: dict[Path, list[Shot]] = {}
    for ev in events:
        results[ev] = []
        for primary, files in collect_shots(ev):
            sig = Cache.signature(files)
            hit = cache.get(primary, sig)
            if hit:
                results[ev].append(hit)
            else:
                jobs.append((ev, primary, files, sig))

    cached = sum(len(v) for v in results.values())
    print(f"{len(events)} kansiota, {cached + len(jobs)} kuvaa ({cached} välimuistista, {len(jobs)} analysoitavana)")

    t0 = time.time()
    with cf.ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(analyse_file, p, f): (ev, sig) for ev, p, f, sig in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            ev, sig = futs[fut]
            shot = fut.result()
            results[ev].append(shot)
            if not shot.error:
                cache.put(shot, sig)
            if i % 50 == 0 or i == len(jobs):
                cache.commit()
                rate = i / max(time.time() - t0, 1e-6)
                print(f"\r  {i}/{len(jobs)}  ({rate:.1f} kuvaa/s)", end="", flush=True)
    if jobs:
        print()
    cache.commit()

    move_kinds = {k.strip() for k in args.move.split(",") if k.strip()}
    if move_kinds - {"blurry", "similar"}:
        print("--move hyväksyy vain arvot blurry,similar", file=sys.stderr)
        return 1

    xmp_stats = {"written": 0, "exists": 0, "skip": 0}
    moved = 0
    for ev, shots in results.items():
        group_shots(shots, args.gap, args.hamming, 0.9)
        judge(shots, args.abs_blur, args.rel_blur, args.sim_keep)
        if args.write_xmp:
            for s in shots:
                xmp_stats[write_xmp(s)] += 1
        if move_kinds:
            moved += sum(move_rejects(s, move_kinds) for s in shots)

    csv_path = write_csv(args.report, results)
    html_path = write_report(args.report, results, root, args.workers)

    counts: dict[str, int] = {}
    for shots in results.values():
        for s in shots:
            counts[s.verdict] = counts.get(s.verdict, 0) + 1
    print("Tulokset: " + ", ".join(f"{VERDICT_FI.get(k, k)} {v}" for k, v in counts.items()))
    if args.write_xmp:
        print(f"XMP: {xmp_stats['written']} kirjoitettu, {xmp_stats['exists']} oli jo olemassa (ei koskettu)")
    if move_kinds:
        print(f"Siirretty {moved} kuvaa {REJECT_DIR}-alikansioihin")
    print(f"Raportti: {html_path.resolve()}\nCSV:      {csv_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
