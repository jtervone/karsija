"""File actions: XMP sidecars and moving rejects aside. Nothing is ever deleted."""

from __future__ import annotations

import shutil
from pathlib import Path

from karsija.analysis import Shot

REJECT_DIR = "_karsitut"

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

