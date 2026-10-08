from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


DTBOOK_NS = "http://www.daisy.org/z3986/2005/dtbook/"
SMIL_NS = "http://www.w3.org/2001/SMIL20/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
RESOURCE_NS = "http://www.daisy.org/z3986/2005/resource/"
OPF_NS = "http://openebook.org/namespaces/oeb-package/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _write_xml(path: Path, root: ET.Element, doctype: str | None = None) -> None:
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    prefix = '<?xml version="1.0" encoding="UTF-8"?>\n'
    if doctype:
        prefix += f"{doctype}\n"
    path.write_text(prefix + body + "\n", encoding="utf-8")


def _group_long_paragraph(text: str, max_chars: int = 800) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]
    if not sentences:
        return []

    groups: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            boundary = sentence.rfind(" ", 0, max_chars + 1)
            if boundary < max_chars // 2:
                boundary = max_chars
            groups.append(sentence[:boundary].strip())
            sentence = sentence[boundary:].strip()
        if current and len(current) + len(sentence) + 1 > max_chars:
            groups.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        groups.append(current)
    return groups


def make_paragraphs(text: str) -> list[str]:
    blocks = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", text)]
    paragraphs = [part for block in blocks if block for part in _group_long_paragraph(block)]
    if not paragraphs and text.strip():
        paragraphs = _group_long_paragraph(re.sub(r"\s+", " ", text).strip())
    return paragraphs


def build_daisy_package(
    title: str,
    paragraphs: list[str],
    audio_segments: list[bytes],
    output_dir: Path,
) -> Path:
    if not paragraphs or len(paragraphs) != len(audio_segments):
        raise ValueError("Text and audio segments do not match.")

    (output_dir / "audio").mkdir(parents=True, exist_ok=True)
    (output_dir / "smil").mkdir(parents=True, exist_ok=True)
    book_id = f"soni2-{uuid.uuid4()}"
    audio_names = [f"segment-{index:04d}.mp3" for index in range(1, len(paragraphs) + 1)]
    for name, audio in zip(audio_names, audio_segments, strict=True):
        (output_dir / "audio" / name).write_bytes(audio)
    (output_dir / "book.mp3").write_bytes(b"".join(audio_segments))

    ET.register_namespace("", DTBOOK_NS)
    dtbook = ET.Element(
        _q(DTBOOK_NS, "dtbook"),
        {"version": "2005-3", _q(XML_NS, "lang"): "es-ES"},
    )
    head = ET.SubElement(dtbook, _q(DTBOOK_NS, "head"))
    ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": "dc:Title", "content": title})
    book = ET.SubElement(dtbook, _q(DTBOOK_NS, "book"))
    bodymatter = ET.SubElement(book, _q(DTBOOK_NS, "bodymatter"))
    level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": "chapter-1"})
    ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": "chapter-title"}).text = title

    ET.register_namespace("", SMIL_NS)
    smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
    smil_head = ET.SubElement(smil, _q(SMIL_NS, "head"))
    ET.SubElement(smil_head, _q(SMIL_NS, "meta"), {"name": "dc:Title", "content": title})
    smil_body = ET.SubElement(smil, _q(SMIL_NS, "body"))
    sequence = ET.SubElement(smil_body, _q(SMIL_NS, "seq"), {"id": "seq-book"})

    for index, (paragraph, audio_name) in enumerate(zip(paragraphs, audio_names, strict=True), start=1):
        paragraph_id = f"p-{index:04d}"
        par_id = f"par-{index:04d}"
        ET.SubElement(
            level,
            _q(DTBOOK_NS, "p"),
            {"id": paragraph_id, "smilref": f"smil/book.smil#{par_id}"},
        ).text = paragraph
        par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
        ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../book.xml#{paragraph_id}"})
        ET.SubElement(par, _q(SMIL_NS, "audio"), {"src": f"../audio/{audio_name}"})

    _write_xml(
        output_dir / "book.xml",
        dtbook,
        '<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">',
    )
    _write_xml(
        output_dir / "smil" / "book.smil",
        smil,
        '<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-1//EN" "http://www.daisy.org/z3986/2005/dtbsmil-2005-1.dtd">',
    )

    ET.register_namespace("", NCX_NS)
    ncx = ET.Element(_q(NCX_NS, "ncx"), {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"})
    ncx_head = ET.SubElement(ncx, _q(NCX_NS, "head"))
    for name, content in (
        ("dtb:uid", book_id),
        ("dtb:depth", "1"),
        ("dtb:totalPageCount", "0"),
        ("dtb:maxPageNumber", "0"),
    ):
        ET.SubElement(ncx_head, _q(NCX_NS, "meta"), {"name": name, "content": content})
    doc_title = ET.SubElement(ncx, _q(NCX_NS, "docTitle"))
    ET.SubElement(doc_title, _q(NCX_NS, "text")).text = title
    nav_map = ET.SubElement(ncx, _q(NCX_NS, "navMap"))
    nav_point = ET.SubElement(
        nav_map,
        _q(NCX_NS, "navPoint"),
        {"id": "chapter-1", "playOrder": "1", "class": "chapter"},
    )
    nav_label = ET.SubElement(nav_point, _q(NCX_NS, "navLabel"))
    ET.SubElement(nav_label, _q(NCX_NS, "text")).text = title
    ET.SubElement(nav_point, _q(NCX_NS, "content"), {"src": "smil/book.smil#par-0001"})
    _write_xml(
        output_dir / "book.ncx",
        ncx,
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
    )

    ET.register_namespace("", RESOURCE_NS)
    resources = ET.Element(
        _q(RESOURCE_NS, "resources"),
        {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"},
    )
    scope = ET.SubElement(resources, _q(RESOURCE_NS, "scope"), {"nsuri": NCX_NS})
    node_set = ET.SubElement(
        scope,
        _q(RESOURCE_NS, "nodeSet"),
        {"id": "chapter-labels", "select": "//navPoint[@class='chapter']"},
    )
    resource = ET.SubElement(node_set, _q(RESOURCE_NS, "resource"), {"id": "chapter-label-es", _q(XML_NS, "lang"): "es"})
    ET.SubElement(resource, _q(RESOURCE_NS, "text")).text = "Capítulo"
    _write_xml(
        output_dir / "book.res",
        resources,
        '<!DOCTYPE resources PUBLIC "-//NISO//DTD resource 2005-1//EN" "http://www.daisy.org/z3986/2005/resource-2005-1.dtd">',
    )

    ET.register_namespace("", OPF_NS)
    ET.register_namespace("dc", DC_NS)
    package = ET.Element(_q(OPF_NS, "package"), {"unique-identifier": "uid"})
    metadata = ET.SubElement(package, _q(OPF_NS, "metadata"))
    dc_metadata = ET.SubElement(
        metadata,
        _q(OPF_NS, "dc-metadata"),
        {"xmlns:dc": DC_NS, "xmlns:oebpackage": OPF_NS},
    )
    ET.SubElement(dc_metadata, _q(DC_NS, "Title")).text = title
    ET.SubElement(dc_metadata, _q(DC_NS, "Identifier"), {"id": "uid", "scheme": "DTB"}).text = book_id
    ET.SubElement(dc_metadata, _q(DC_NS, "Language")).text = "es"
    ET.SubElement(dc_metadata, _q(DC_NS, "Format")).text = "ANSI/NISO Z39.86-2005"
    x_metadata = ET.SubElement(metadata, _q(OPF_NS, "x-metadata"))
    for name, content in (
        ("dtb:producer", "Soni2"),
        ("dtb:multimediaContent", "audio,text"),
        ("dtb:multimediaType", "audioFullText"),
        ("dtb:audioFormat", "MP3"),
    ):
        ET.SubElement(x_metadata, _q(OPF_NS, "meta"), {"name": name, "content": content})
    manifest = ET.SubElement(package, _q(OPF_NS, "manifest"))
    items = [
        ("dtbook", "book.xml", "application/x-dtbook+xml"),
        ("ncx", "book.ncx", "application/x-dtbncx+xml"),
        ("resource", "book.res", "application/x-dtbresource+xml"),
        ("smil", "smil/book.smil", "application/smil"),
        ("full-audio", "book.mp3", "audio/mpeg"),
    ]
    items.extend((f"audio-{index:04d}", f"audio/{name}", "audio/mpeg") for index, name in enumerate(audio_names, start=1))
    for item_id, href, media_type in items:
        ET.SubElement(manifest, _q(OPF_NS, "item"), {"id": item_id, "href": href, "media-type": media_type})
    spine = ET.SubElement(package, _q(OPF_NS, "spine"), {"toc": "ncx"})
    ET.SubElement(spine, _q(OPF_NS, "itemref"), {"idref": "smil"})
    _write_xml(
        output_dir / "book.opf",
        package,
        '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN" "oebpkg12.dtd">',
    )
    return output_dir
