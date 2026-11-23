"""File actions: XMP sidecars and moving rejects aside. Nothing is ever deleted."""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from karsija.analysis import Shot

REJECT_DIR = "_karsitut"

RATING = {"best": 5, "keep": 3, "similar": 1, "blurry": -1}
LABEL = {"best": "Green", "keep": "", "similar": "Yellow", "blurry": "Red"}

X_NS = "adobe:ns:meta/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS = "http://ns.adobe.com/xap/1.0/"
BASE_NAMESPACES = {"x": X_NS, "rdf": RDF_NS, "xmp": XMP_NS}

NEW_XMP = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="{X_NS}">
 <rdf:RDF xmlns:rdf="{RDF_NS}">
  <rdf:Description rdf:about=""
    xmlns:xmp="{XMP_NS}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

# the XML declaration, processing instructions (xpacket), comments and a doctype before the root element
_PROLOG_ITEM = re.compile(r"\s*(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>]*>)", re.DOTALL)
_ROOT_NAME = re.compile(r"<([^\s/>]+)")
# ElementTree keeps its prefix map in a module global, so prefixes are registered and used under a lock
_NS_LOCK = threading.Lock()


class XmpError(Exception):
    """A sidecar could not be read as XMP; it was left untouched."""


def xmp_values(verdict: str, user_verdict: str | None = None, user_rating: int | None = None) -> tuple[int, str] | None:
    """Rating and label for a shot: the user's reject, else the user's rating, else from the verdict.

    A user keep turns a rejected verdict into ``keep``. None when there is nothing to write
    (an ``error`` shot without user decisions).
    """
    if user_verdict == "reject":
        return RATING["blurry"], LABEL["blurry"]
    if user_verdict == "keep" and verdict != "best":
        verdict = "keep"
    if user_rating is not None:
        return user_rating, LABEL.get(verdict, "")
    if verdict not in RATING:
        return None
    return RATING[verdict], LABEL[verdict]


def sidecars(path: str | Path) -> list[Path]:
    """Existing sidecars of an image (darktable ``name.ext.xmp``, digiKam/Lightroom ``name.xmp``), else ``name.xmp``."""
    p = Path(path)
    candidates = list(dict.fromkeys([p.with_name(p.name + ".xmp"), p.with_suffix(".xmp")]))
    return [c for c in candidates if c.exists()] or [p.with_suffix(".xmp")]


def xmp_set(path: str | Path, rating: int, label: str) -> dict[Path, str | None]:
    """Write ``xmp:Rating`` and ``xmp:Label`` into every sidecar of the image ``path``.

    Existing sidecars are merged into: only Rating and Label change (an empty ``label`` removes
    ``xmp:Label``), everything else and the text around the root element stay. Without a sidecar
    ``name.xmp`` is created. Returns each sidecar's previous content, None for a created one.
    Raises XmpError, before anything is written, if a sidecar is not valid XMP.
    """
    old: dict[Path, str | None] = {}
    new: dict[Path, str] = {}
    for sidecar in sidecars(path):
        prev = _read(sidecar) if sidecar.exists() else None
        try:
            new[sidecar] = _merge(NEW_XMP if prev is None else prev, rating, label)
        except XmpError as e:
            raise XmpError(f"{sidecar}: {e}") from None
        old[sidecar] = prev
    for sidecar, text in new.items():
        _write_atomic(sidecar, text)
    return old


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise XmpError(f"{path}: ei UTF-8-tekstiä ({e})") from None


def _merge(text: str, rating: int, label: str) -> str:
    start = 1 if text.startswith("\ufeff") else 0
    while m := _PROLOG_ITEM.match(text, start):
        start = m.end()
    start += len(text[start:]) - len(text[start:].lstrip())
    name = _ROOT_NAME.match(text, start)
    end = text.rfind("</" + name.group(1)) if name else -1
    end = text.find(">", end) + 1 if end >= 0 else 0
    if not name or end <= start:
        raise XmpError("XMP-juurielementtiä ei löydy")
    body = text[start:end]
    try:
        namespaces = [ns for _, ns in ET.iterparse(io.StringIO(body), events=("start-ns",))]
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        parser.feed(body)
        root = parser.close()
    except ET.ParseError as e:
        raise XmpError(f"virheellinen XML ({e})") from None

    descriptions = _descriptions(root)
    _set_property(descriptions, "Rating", str(rating))
    _set_property(descriptions, "Label", label or None)

    with _NS_LOCK:
        for prefix, uri in [*BASE_NAMESPACES.items(), *namespaces]:
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                ET.register_namespace(prefix, uri)
        out = ET.tostring(root, encoding="unicode")
    # ElementTree declares only the namespaces in use, at the root; keep the unused ones too
    declared = {prefix for _, (prefix, _) in ET.iterparse(io.StringIO(out), events=("start-ns",))}
    missing = "".join(f' xmlns:{p}="{u}"' for p, u in dict(namespaces).items()
                      if p and p not in declared and not re.fullmatch(r"ns\d+", p))
    if missing:
        at = _ROOT_NAME.match(out).end()  # type: ignore[union-attr]
        out = out[:at] + missing + out[at:]
    return text[:start] + out + text[end:]


def _descriptions(root: ET.Element) -> list[ET.Element]:
    """The top-level rdf:Description elements (not those nested in a property value), one added if none."""
    rdf_tag = f"{{{RDF_NS}}}RDF"
    rdf = root if root.tag == rdf_tag else root.find(rdf_tag)
    if rdf is None:
        raise XmpError("rdf:RDF puuttuu")
    return rdf.findall(f"{{{RDF_NS}}}Description") or [
        ET.SubElement(rdf, f"{{{RDF_NS}}}Description", {f"{{{RDF_NS}}}about": ""})]


def _set_property(descriptions: list[ET.Element], name: str, value: str | None) -> None:
    """Set (None: remove) xmp:name wherever it is, as an attribute or a child element; add it as an attribute."""
    tag = f"{{{XMP_NS}}}{name}"
    found = False
    for desc in descriptions:
        if tag in desc.attrib:
            found = True
            if value is None:
                del desc.attrib[tag]
            else:
                desc.set(tag, value)
        for child in desc.findall(tag):
            found = True
            if value is not None:
                child.text = value
                continue
            i = list(desc).index(child)
            if i:
                desc[i - 1].tail = child.tail       # the next sibling (or closing tag) keeps its indentation
            else:
                desc.text = child.tail
            desc.remove(child)
    if not found and value is not None:
        descriptions[0].set(tag, value)


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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

