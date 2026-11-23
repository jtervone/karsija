"""File actions: XMP sidecars and moving rejects aside. Nothing is ever deleted."""

from __future__ import annotations

import errno
import io
import os
import re
import shutil
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import quoteattr

from karsija.analysis import Shot

REJECT_DIR = "_karsitut"

RATING = {"best": 5, "keep": 3, "similar": 1, "blurry": -1}
LABEL = {"best": "Green", "keep": "", "similar": "Yellow", "blurry": "Red"}
OWN_LABELS = {v for v in LABEL.values() if v}      # the only labels an empty label removes
# values karsija may replace when the user's own are kept: what it writes itself, 0 (unrated) or none
OWN_RATING_VALUES = {"", "0", *(str(v) for v in RATING.values())}
OWN_LABEL_VALUES = {"", *OWN_LABELS}

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
# ElementTree keeps its prefix map in a module global, so prefixes are registered and used under a lock. The
# registrations stay for the whole process; every sidecar re-registers the prefixes it declares before it is written.
_NS_LOCK = threading.Lock()


# a new sidecar gets the mode open() would give it; read once, os.umask can only be read by setting it
_UMASK = os.umask(0o022)
os.umask(_UMASK)


class XmpError(Exception):
    """A sidecar could not be read as XMP; it was left untouched."""


class UserValuesKept(Exception):
    """A sidecar holds a rating or label karsija does not write; with ``keep_user_values`` nothing was written."""

    def __init__(self, sidecar: Path) -> None:
        super().__init__(str(sidecar))
        self.sidecar = sidecar


class _UserValues(Exception):
    """Internal: _merge found a value of the user's own."""


class XmpWriteError(OSError):
    """Writing a sidecar failed. ``unrestored``: sidecars already written that could not be put back."""

    def __init__(self, error: OSError, restored: list[Path], unrestored: list[Path]) -> None:
        super().__init__(error.errno, str(error))
        self.restored, self.unrestored = restored, unrestored


def xmp_values(verdict: str, user_verdict: str | None = None, user_rating: int | None = None) -> tuple[int, str] | None:
    """Rating and label for a shot: the user's reject, else the user's rating, else from the verdict.

    A user keep turns a rejected verdict into ``keep``. A user rating changes only the rating, the label still
    follows the verdict (it marks karsija's judgement, the stars are the user's). None when there is nothing to
    write (an ``error`` shot without user decisions).
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


def sidecars(path: str | Path, files: Iterable[str | Path] = ()) -> list[Path]:
    """Existing sidecars of an image (darktable ``name.ext.xmp``, digiKam/Lightroom ``name.xmp``), else ``name.xmp``.

    ``files`` are the other files of the same shot (the JPEG of a RAW+JPEG pair); their darktable sidecars count too.
    """
    p = Path(path)
    others = [Path(f) for f in files]
    candidates = [p.with_name(p.name + ".xmp"), p.with_suffix(".xmp"), *(f.with_name(f.name + ".xmp") for f in others)]
    return [c for c in dict.fromkeys(candidates) if c.exists()] or [p.with_suffix(".xmp")]


def xmp_set(path: str | Path, rating: int, label: str, files: Iterable[str | Path] = (),
            keep_user_values: bool = False) -> dict[Path, str | None]:
    """Write ``xmp:Rating`` and ``xmp:Label`` into every sidecar of the image ``path`` (and of ``files``).

    Existing sidecars are merged into: only Rating and Label change, everything else and the text around
    the root element stay. An empty ``label`` removes ``xmp:Label`` only when it is one karsija writes
    (OWN_LABELS); a label of the user's own stays. ``name.xmp`` is created only when there is no sidecar
    at all. Returns the previous content of each sidecar that changed, None for a created one; a sidecar
    that already has the values is not rewritten.

    Nothing is written when a sidecar is not valid XMP (XmpError), cannot be read or is read-only
    (OSError), or, with ``keep_user_values``, holds a rating or label karsija does not write itself
    (UserValuesKept). If a write fails, the sidecars already written are put back and XmpWriteError
    tells which could not be.
    """
    changes = xmp_changes(path, rating, label, files, keep_user_values)
    old = {sidecar: prev for sidecar, (prev, _) in changes.items()}
    new = {sidecar: text for sidecar, (_, text) in changes.items()}
    written: list[Path] = []
    try:
        for sidecar, text in new.items():
            _write_atomic(sidecar, text)
            written.append(sidecar)
    except OSError as e:
        restored, unrestored = [], []
        for sidecar in written:
            prev = old[sidecar]
            try:
                if prev is None:
                    sidecar.unlink(missing_ok=True)
                else:
                    _write_atomic(sidecar, prev)
                restored.append(sidecar)
            except OSError:
                unrestored.append(sidecar)
        raise XmpWriteError(e, restored, unrestored) from e
    return old


def xmp_changes(path: str | Path, rating: int, label: str, files: Iterable[str | Path] = (),
                keep_user_values: bool = False) -> dict[Path, tuple[str | None, str]]:
    """What ``xmp_set`` would write, without writing: ``sidecar -> (previous content or None, new content)`` for
    each sidecar that would change. Raises as ``xmp_set`` does before it writes anything."""
    out: dict[Path, tuple[str | None, str]] = {}
    for sidecar in sidecars(path, files):
        prev = _read(sidecar) if sidecar.exists() else None
        if prev is not None and not os.access(sidecar, os.W_OK):
            # os.replace would only need a writable folder; a read-only sidecar is the user's way to protect it
            raise PermissionError(errno.EACCES, "sivutiedosto on kirjoitussuojattu", str(sidecar))
        try:
            text = _merge(NEW_XMP if prev is None else prev, rating, label, keep_user_values)
        except XmpError as e:
            raise XmpError(f"{sidecar}: {e}") from None
        except _UserValues:
            raise UserValuesKept(sidecar) from None
        if text != prev:
            out[sidecar] = (prev, text)
    return out


def write_atomic(path: Path, text: str) -> None:
    """Replace (or create) ``path`` with ``text`` at once: a reader sees the old file or the new one."""
    _write_atomic(path, text)


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise XmpError(f"{path}: ei UTF-8-tekstiä ({e})") from None


def _merge(text: str, rating: int, label: str, keep_user_values: bool = False) -> str:
    start = 1 if text.startswith("\ufeff") else 0
    while m := _PROLOG_ITEM.match(text, start):
        start = m.end()
    start += len(text[start:]) - len(text[start:].lstrip())
    name = _ROOT_NAME.match(text, start)
    end = text.rfind("</" + name.group(1)) if name else -1
    end = text.find(">", end) + 1 if end >= 0 else 0
    if not name or end <= start:
        raise XmpError("x:xmpmeta- tai rdf:RDF-elementtiä ei löydy, tai se on tyhjä")
    body = text[start:end]
    try:
        namespaces = [ns for _, ns in ET.iterparse(io.StringIO(body), events=("start-ns",))]
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        parser.feed(body)
        root = parser.close()
    except ET.ParseError as e:
        raise XmpError(f"virheellinen XML ({e})") from None

    descriptions = _descriptions(root)
    if keep_user_values and not (_values(descriptions, "Rating") <= OWN_RATING_VALUES
                                 and _values(descriptions, "Label") <= OWN_LABEL_VALUES):
        raise _UserValues
    _set_property(descriptions, "Rating", str(rating))
    _set_property(descriptions, "Label", label or None, removable=OWN_LABELS)

    with _NS_LOCK:
        for prefix, uri in [*BASE_NAMESPACES.items(), *namespaces]:
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                ET.register_namespace(prefix, uri)
        out = ET.tostring(root, encoding="unicode")
    # ElementTree declares only the namespaces in use, at the root; keep the unused ones too
    declared = {prefix for _, (prefix, _) in ET.iterparse(io.StringIO(out), events=("start-ns",))}
    missing = "".join(f" xmlns:{p}={quoteattr(u)}" for p, u in dict(namespaces).items()
                      if p and p not in declared and not re.fullmatch(r"ns\d+", p))
    if missing:
        at = _ROOT_NAME.match(out).end()  # type: ignore[union-attr]
        out = out[:at] + missing + out[at:]
    try:
        ET.fromstring(out)                  # never write what the next reader cannot parse
    except ET.ParseError as e:
        raise XmpError(f"yhdistetty XMP ei ole kelvollista XML:ää ({e})") from None
    if "\r\n" in body and "\n" not in body.replace("\r\n", ""):
        # expat turns CRLF into LF. When every newline was CRLF, turning them all back is exact (newlines
        # inside attribute values are escaped); a file with mixed endings keeps LF in the rewritten part.
        out = out.replace("\n", "\r\n")
    return text[:start] + out + text[end:]


def _descriptions(root: ET.Element) -> list[ET.Element]:
    """The top-level rdf:Description elements (not those nested in a property value), one added if none."""
    rdf_tag = f"{{{RDF_NS}}}RDF"
    rdf = root if root.tag == rdf_tag else root.find(rdf_tag)
    if rdf is None:
        raise XmpError("rdf:RDF puuttuu")
    return rdf.findall(f"{{{RDF_NS}}}Description") or [
        ET.SubElement(rdf, f"{{{RDF_NS}}}Description", {f"{{{RDF_NS}}}about": ""})]


def _values(descriptions: list[ET.Element], name: str) -> set[str]:
    """Values of xmp:name, as an attribute or a child element, in the given descriptions."""
    tag = f"{{{XMP_NS}}}{name}"
    out = {d.attrib[tag].strip() for d in descriptions if tag in d.attrib}
    return out | {(c.text or "").strip() for d in descriptions for c in d.findall(tag)}


def _set_property(descriptions: list[ET.Element], name: str, value: str | None,
                  removable: Iterable[str] | None = None) -> None:
    """Set xmp:name wherever it is, as an attribute or a child element; add it as an attribute.

    None removes it, but only where its value is in ``removable`` (when given).
    """
    tag = f"{{{XMP_NS}}}{name}"
    removable_values = set() if removable is None else {"", *removable}
    found = False
    for desc in descriptions:
        if tag in desc.attrib:
            found = True
            if value is not None:
                desc.set(tag, value)
            elif removable is None or desc.attrib[tag].strip() in removable_values:
                del desc.attrib[tag]
        for child in desc.findall(tag):
            found = True
            if value is not None:
                child.text = value
                continue
            if removable is not None and (child.text or "").strip() not in removable_values:
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
    path = path.resolve()           # a symlinked sidecar: replace the file it points to, keep the link
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o666 & ~_UMASK)
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

