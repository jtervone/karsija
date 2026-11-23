from __future__ import annotations

import io
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from karsija.actions import REJECT_DIR, XMP_NS, XmpError, move_rejects, xmp_set, xmp_values
from karsija.analysis import Shot

FIXTURES = Path(__file__).parent / "fixtures"
RATING, LABEL = f"{{{XMP_NS}}}Rating", f"{{{XMP_NS}}}Label"

ELEMENT_FORM = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <!-- written by hand -->
  <rdf:Description rdf:about="" xmlns:xap="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <xap:Rating>2</xap:Rating>
   <xap:Label>Blue</xap:Label>
   <dc:creator><rdf:Seq><rdf:li>Aino</rdf:li></rdf:Seq></dc:creator>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="r"?>
"""


def make_pair(tmp_path: Path, verdict: str) -> Shot:
    raw, jpg = tmp_path / "a.nef", tmp_path / "a.jpg"
    raw.write_bytes(b"raw")
    jpg.write_bytes(b"jpeg")
    return Shot(key=str(raw), files=[str(jpg), str(raw)], verdict=verdict)


def wrapper(text: str) -> tuple[str, str]:
    """Text before and after the x:xmpmeta element (xpacket, XML declaration, padding)."""
    return text[:text.index("<x:xmpmeta")], text[text.rindex("</x:xmpmeta>"):]


def root_text(text: str) -> str:
    return text[text.index("<x:xmpmeta"):text.rindex("</x:xmpmeta>") + len("</x:xmpmeta>")]


def namespaces(text: str) -> set[tuple[str, str]]:
    return {ns for _, ns in ET.iterparse(io.StringIO(root_text(text)), events=("start-ns",))}


def parse(text: str) -> ET.Element:
    return ET.fromstring(root_text(text))


def without_rating_label(el: ET.Element) -> tuple[object, ...]:
    """Comparable form of an element tree, xmp:Rating and xmp:Label left out, whitespace ignored."""
    attrs = sorted((k, v) for k, v in el.attrib.items() if k not in (RATING, LABEL))
    children = tuple(without_rating_label(c) for c in el if c.tag not in (RATING, LABEL))
    return el.tag, attrs, (el.text or "").strip(), children


def rating_label(text: str) -> list[tuple[str | None, str | None]]:
    out = []
    for desc in parse(text).iter("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description"):
        rating = desc.get(RATING) or desc.findtext(RATING)
        label = desc.get(LABEL) or desc.findtext(LABEL)
        if rating or label:
            out.append((rating, label))
    return out


@pytest.mark.parametrize(("fixture", "image"), [("darktable.nef.xmp", "darktable.nef"), ("digikam.xmp", "digikam.nef")])
def test_xmp_set_keeps_everything_else(tmp_path: Path, fixture: str, image: str) -> None:
    sidecar = tmp_path / fixture
    shutil.copy(FIXTURES / fixture, sidecar)
    sidecar.chmod(0o640)
    before = sidecar.read_text(encoding="utf-8")

    assert xmp_set(tmp_path / image, 5, "Green") == {sidecar: before}
    after = sidecar.read_text(encoding="utf-8")
    assert rating_label(after) == [("5", "Green")]
    assert without_rating_label(parse(after)) == without_rating_label(parse(before))
    assert wrapper(after) == wrapper(before)
    assert namespaces(after) == namespaces(before)
    assert after.count("<rdf:li") == before.count("<rdf:li")
    assert 'darktable:multi_name="&lt;kirkkaus&gt; &amp; varjot"' in after or "darktable" not in fixture
    assert sidecar.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == [fixture]      # no temp file left behind

    # an empty label removes xmp:Label, the rest stays
    xmp_set(tmp_path / image, -1, "Red")
    xmp_set(tmp_path / image, 3, "")
    final = sidecar.read_text(encoding="utf-8")
    assert rating_label(final) == [("3", None)]
    assert without_rating_label(parse(final)) == without_rating_label(parse(before))


def test_xmp_set_element_form(tmp_path: Path) -> None:
    sidecar = tmp_path / "a.xmp"
    sidecar.write_text(ELEMENT_FORM, encoding="utf-8")
    xmp_set(tmp_path / "a.cr3", 1, "Yellow")
    after = sidecar.read_text(encoding="utf-8")
    assert "<xap:Rating>1</xap:Rating>" in after and "<xap:Label>Yellow</xap:Label>" in after
    assert "xmp:" not in after and "<!-- written by hand -->" in after      # the file's own prefix is kept
    assert without_rating_label(parse(after)) == without_rating_label(parse(ELEMENT_FORM))
    assert wrapper(after) == wrapper(ELEMENT_FORM)

    xmp_set(tmp_path / "a.cr3", 3, "")
    after = sidecar.read_text(encoding="utf-8")
    assert rating_label(after) == [("3", None)]
    assert "   <xap:Rating>3</xap:Rating>\n   <dc:creator>" in after         # indentation survives the removal


def test_xmp_set_adds_missing_properties(tmp_path: Path) -> None:
    sidecar = tmp_path / "a.xmp"
    sidecar.write_text(ELEMENT_FORM.replace("   <xap:Rating>2</xap:Rating>\n   <xap:Label>Blue</xap:Label>\n", ""),
                       encoding="utf-8")
    xmp_set(tmp_path / "a.nef", 5, "Green")
    after = sidecar.read_text(encoding="utf-8")
    assert rating_label(after) == [("5", "Green")]
    assert 'xap:Rating="5"' in after


def test_xmp_set_creates_and_updates_both_styles(tmp_path: Path) -> None:
    created = tmp_path / "a.xmp"
    assert xmp_set(tmp_path / "a.nef", 5, "Green") == {created: None}
    text = created.read_text(encoding="utf-8")
    assert text.startswith('<?xpacket begin="﻿"') and text.endswith('<?xpacket end="w"?>\n')
    assert rating_label(text) == [("5", "Green")]
    assert 'xmlns:xmp="http://ns.adobe.com/xap/1.0/"' in text
    assert created.stat().st_mode & 0o777 == 0o644

    shutil.copy(FIXTURES / "darktable.nef.xmp", tmp_path / "a.nef.xmp")
    darktable = (tmp_path / "a.nef.xmp").read_text(encoding="utf-8")
    assert xmp_set(tmp_path / "a.nef", 1, "Yellow") == {tmp_path / "a.nef.xmp": darktable, created: text}
    for name in ("a.xmp", "a.nef.xmp"):
        assert rating_label((tmp_path / name).read_text(encoding="utf-8")) == [("1", "Yellow")]


@pytest.mark.parametrize("content", ["<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF>", "not xml at all", "",
                                     "<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", b"\xff\xfe<x"])
def test_xmp_set_malformed_left_untouched(tmp_path: Path, content: str | bytes) -> None:
    good, bad = tmp_path / "a.xmp", tmp_path / "a.nef.xmp"
    shutil.copy(FIXTURES / "digikam.xmp", good)
    data = content if isinstance(content, bytes) else content.encode()
    bad.write_bytes(data)
    good_before = good.read_bytes()
    with pytest.raises(XmpError, match="a.nef.xmp"):
        xmp_set(tmp_path / "a.nef", 5, "Green")
    assert bad.read_bytes() == data and good.read_bytes() == good_before     # neither sidecar is written
    assert sorted(os.listdir(tmp_path)) == ["a.nef.xmp", "a.xmp"]


def test_xmp_values() -> None:
    assert xmp_values("best") == (5, "Green")
    assert xmp_values("keep") == (3, "")
    assert xmp_values("similar") == (1, "Yellow")
    assert xmp_values("blurry") == (-1, "Red")
    assert xmp_values("error") is None
    assert xmp_values("best", "reject", 4) == (-1, "Red")
    assert xmp_values("blurry", "keep") == (3, "")
    assert xmp_values("best", "keep") == (5, "Green")
    assert xmp_values("similar", None, 4) == (4, "Yellow")
    assert xmp_values("best", None, 0) == (0, "Green")
    assert xmp_values("error", "keep", 2) == (2, "")


def test_move_rejects(tmp_path: Path) -> None:
    shot = make_pair(tmp_path, "blurry")
    (tmp_path / "a.nef.xmp").write_text("darktable")
    (tmp_path / "a.xmp").write_text("digikam")
    assert not move_rejects(shot, {"similar"})
    assert move_rejects(shot, {"blurry", "similar"})
    assert sorted(p.name for p in tmp_path.iterdir()) == [REJECT_DIR]
    assert sorted(p.name for p in (tmp_path / REJECT_DIR).iterdir()) == ["a.jpg", "a.nef", "a.nef.xmp", "a.xmp"]
