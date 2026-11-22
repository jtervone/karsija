"""Command line interface: `karsija scan ROOT`."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import html
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

from karsija.actions import REJECT_DIR, move_rejects, write_xmp
from karsija.analysis import Shot, analyse_file, group_shots, judge
from karsija.library import JPEG_EXT, collect_shots, find_event_dirs, load_image
from karsija.store import Store


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
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
        thumb_of = dict(zip((s.key for s in all_shots), ex.map(lambda s: make_thumb(s, thumbs), all_shots), strict=True))

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
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="karsija", description="Kuvien karsinta-apu.")
    sub = ap.add_subparsers(dest="command", required=True)

    sc = sub.add_parser("scan", help="analysoi kirjasto ja kirjoita raportti",
                        description="Etsi epäterävät kuvat ja valitse samankaltaisista paras.")
    sc.add_argument("root", type=Path, help="kuvakirjaston juuri (sisältää YYYY-kansiot)")
    sc.add_argument("--only", default="",
                    help="käsittele vain kansiot joiden polku sisältää tämän (esim. 2025 tai 2025-06)")
    sc.add_argument("--report", type=Path, default=Path("karsintaraportti"), help="raporttikansio")
    sc.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    sc.add_argument("--gap", type=float, default=3.0, help="max sekunnit peräkkäisten kuvien välillä samassa ryhmässä")
    sc.add_argument("--hamming", type=int, default=40, help="max dHash-ero (0-256) samankaltaisille")
    sc.add_argument("--abs-blur", type=float, default=10.0, help="tätä pienempi terävyys = epäterävä")
    sc.add_argument("--rel-blur", type=float, default=0.25, help="alle tämä osuus kansion mediaanista = epäterävä")
    sc.add_argument("--sim-keep", type=float, default=0.0,
                    help="pidä ryhmän jäsen myös, jos terävyys ≥ tämä × paras (esim. 0.95). Oletus: vain paras pidetään")
    sc.add_argument("--write-xmp", action="store_true", help="kirjoita arvio XMP-sivutiedostoon (ei ylikirjoita)")
    sc.add_argument("--move", default="", help="siirrä _karsitut-alikansioon: blurry,similar")
    sc.set_defaults(func=scan)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


def scan(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Kansiota ei löydy: {root}", file=sys.stderr)
        return 1
    events = [e for e in find_event_dirs(root) if args.only in str(e.relative_to(root))]
    if not events:
        print("Ei YYYY/YYYY-MM-DD* -kansioita.", file=sys.stderr)
        return 1

    args.report.mkdir(parents=True, exist_ok=True)
    store = Store(args.report / "karsija.sqlite")

    jobs: list[tuple[Path, int, str, list[str], str]] = []
    results: dict[Path, list[Shot]] = {}
    for ev in events:
        results[ev] = []
        event_id = store.event_id(ev.relative_to(root).as_posix())
        for primary, files in collect_shots(ev):
            sig = Store.signature(files)
            hit = store.get(primary, sig)
            if hit:
                results[ev].append(hit)
            else:
                jobs.append((ev, event_id, primary, files, sig))

    cached = sum(len(v) for v in results.values())
    print(f"{len(events)} kansiota, {cached + len(jobs)} kuvaa ({cached} välimuistista, {len(jobs)} analysoitavana)")

    t0 = time.time()
    with cf.ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(analyse_file, p, f): (ev, eid, sig) for ev, eid, p, f, sig in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            ev, eid, sig = futs[fut]
            shot = fut.result()
            results[ev].append(shot)
            if not shot.error:
                store.put(eid, shot, sig)
            if i % 50 == 0 or i == len(jobs):
                store.commit()
                rate = i / max(time.time() - t0, 1e-6)
                print(f"\r  {i}/{len(jobs)}  ({rate:.1f} kuvaa/s)", end="", flush=True)
    if jobs:
        print()
    store.commit()

    move_kinds = {k.strip() for k in args.move.split(",") if k.strip()}
    if move_kinds - {"blurry", "similar"}:
        print("--move hyväksyy vain arvot blurry,similar", file=sys.stderr)
        store.close()
        return 1

    xmp_stats = {"written": 0, "exists": 0, "skip": 0}
    moved = 0
    for shots in results.values():
        group_shots(shots, args.gap, args.hamming, 0.9)
        judge(shots, args.abs_blur, args.rel_blur, args.sim_keep)
        store.save_verdicts(shots)
        if args.write_xmp:
            for s in shots:
                xmp_stats[write_xmp(s)] += 1
        if move_kinds:
            moved += sum(move_rejects(s, move_kinds) for s in shots)
    store.commit()
    store.close()

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
