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
    *,
    chapters: list[tuple[str, list[str]]] | None = None,
    author: str = "",
    edition: str = "",
    publication_year: str = "",
    publisher: str = "",
    front_matter: list[tuple[str, str, bytes, str]] | None = None,
) -> Path:
    chapters = chapters or [(title, paragraphs)]
    paragraphs = [paragraph for _, body in chapters for paragraph in body]
    if not paragraphs or len(paragraphs) != len(audio_segments):
        raise ValueError("Text and audio segments do not match.")

    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("DAISY output directory must be empty before package generation.")
    (output_dir / "audio").mkdir(parents=True, exist_ok=True)
    (output_dir / "smil").mkdir(parents=True, exist_ok=True)
    book_id = f"soni2-{uuid.uuid4()}"
    front_matter = front_matter or []
    front_names: list[str] = []
    for _label, _text, _audio, filename in front_matter:
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".mp3" or filename in front_names:
            raise ValueError("Front-matter audio filenames must be unique MP3 filenames without directories.")
        front_names.append(filename)
    audio_names = [f"segment-{index:04d}.mp3" for index in range(1, len(paragraphs) + 1)]
    chapter_audio_names = [f"chapter-{index:04d}.mp3" for index in range(1, len(chapters) + 1)]
    if set(front_names).intersection(audio_names):
        raise ValueError("Front-matter audio filenames cannot reuse chapter-segment filenames.")
    for name, audio in zip(audio_names, audio_segments, strict=True):
        (output_dir / "audio" / name).write_bytes(audio)
    segment_offset = 0
    for name, (_chapter_title, chapter_body) in zip(chapter_audio_names, chapters, strict=True):
        count = len(chapter_body)
        (output_dir / "audio" / name).write_bytes(b"".join(audio_segments[segment_offset:segment_offset + count]))
        segment_offset += count
    for filename, (_label, _text, audio, _configured_name) in zip(front_names, front_matter, strict=True):
        (output_dir / "audio" / filename).write_bytes(audio)
    (output_dir / "book.mp3").write_bytes(b"".join(item[2] for item in front_matter) + b"".join(audio_segments))

    ET.register_namespace("", DTBOOK_NS)
    dtbook = ET.Element(
        _q(DTBOOK_NS, "dtbook"),
        {"version": "2005-3", _q(XML_NS, "lang"): "es-ES"},
    )
    head = ET.SubElement(dtbook, _q(DTBOOK_NS, "head"))
    ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": "dc:Title", "content": title})
    book = ET.SubElement(dtbook, _q(DTBOOK_NS, "book"))
    bodymatter = ET.SubElement(book, _q(DTBOOK_NS, "bodymatter"))
    if author:
        ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": "dc:Creator", "content": author})
    smil_names: list[str] = []
    chapter_targets: list[tuple[str, str, str]] = []
    page_targets: list[tuple[int, str]] = []
    for index, (label, text, _audio, filename) in enumerate(front_matter, start=1):
        front_id = f"frontmatter-{index}"
        smil_name = f"frontmatter-{index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": front_id, "class": "frontmatter"})
        ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": f"{front_id}-title"}).text = label
        paragraph_id = f"{front_id}-text"
        par_id = f"{front_id}-par"
        ET.SubElement(level, _q(DTBOOK_NS, "p"),
                      {"id": paragraph_id, "smilref": f"smil/{smil_name}#{par_id}"}).text = text
        ET.register_namespace("", SMIL_NS)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        smil_head = ET.SubElement(smil, _q(SMIL_NS, "head"))
        ET.SubElement(smil_head, _q(SMIL_NS, "meta"), {"name": "dc:Title", "content": label})
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"), {"id": f"seq-{front_id}"})
        par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
        ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../book.xml#{paragraph_id}"})
        ET.SubElement(par, _q(SMIL_NS, "audio"), {"src": f"../audio/{filename}"})
        _write_xml(output_dir / "smil" / smil_name, smil,
                   '<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-1//EN" "http://www.daisy.org/z3986/2005/dtbsmil-2005-1.dtd">')
        chapter_targets.append((label, f"smil/{smil_name}#{par_id}", "frontmatter"))
    audio_index = 0
    for chapter_index, (chapter_title, chapter_paragraphs) in enumerate(chapters, start=1):
        if not chapter_paragraphs:
            continue
        chapter_id = f"chapter-{chapter_index}"
        smil_name = f"chapter-{chapter_index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": chapter_id})
        ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": f"{chapter_id}-title"}).text = chapter_title

        ET.register_namespace("", SMIL_NS)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        smil_head = ET.SubElement(smil, _q(SMIL_NS, "head"))
        ET.SubElement(smil_head, _q(SMIL_NS, "meta"), {"name": "dc:Title", "content": chapter_title})
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"), {"id": f"seq-{chapter_id}"})
        first_par_id = ""
        for paragraph in chapter_paragraphs:
            audio_name = audio_names[audio_index]
            audio_index += 1
            paragraph_id = f"p-{audio_index:04d}"
            par_id = f"par-{audio_index:04d}"
            if not first_par_id:
                first_par_id = par_id
            ET.SubElement(level, _q(DTBOOK_NS, "p"),
                          {"id": paragraph_id, "smilref": f"smil/{smil_name}#{par_id}"}).text = paragraph
            par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../book.xml#{paragraph_id}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {"src": f"../audio/{audio_name}"})
        chapter_targets.append((chapter_title, f"smil/{smil_name}#{first_par_id}", "chapter"))
        page_match = re.fullmatch(r"P[aá]gina\s+(\d+)", chapter_title.strip(), flags=re.IGNORECASE)
        if page_match:
            page_targets.append((int(page_match.group(1)), f"smil/{smil_name}#{first_par_id}"))
        _write_xml(output_dir / "smil" / smil_name, smil,
                   '<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-1//EN" "http://www.daisy.org/z3986/2005/dtbsmil-2005-1.dtd">')

    _write_xml(
        output_dir / "book.xml",
        dtbook,
        '<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">',
    )
    ET.register_namespace("", NCX_NS)
    ncx = ET.Element(_q(NCX_NS, "ncx"), {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"})
    ncx_head = ET.SubElement(ncx, _q(NCX_NS, "head"))
    for name, content in (
        ("dtb:uid", book_id),
        ("dtb:depth", "1"),
        ("dtb:totalPageCount", str(len(page_targets))),
        ("dtb:maxPageNumber", str(max((number for number, _ in page_targets), default=0))),
    ):
        ET.SubElement(ncx_head, _q(NCX_NS, "meta"), {"name": name, "content": content})
    doc_title = ET.SubElement(ncx, _q(NCX_NS, "docTitle"))
    ET.SubElement(doc_title, _q(NCX_NS, "text")).text = title
    nav_map = ET.SubElement(ncx, _q(NCX_NS, "navMap"))
    for order, (chapter_title, target, nav_class) in enumerate(chapter_targets, start=1):
        nav_point = ET.SubElement(nav_map, _q(NCX_NS, "navPoint"),
                                  {"id": f"nav-{order}", "playOrder": str(order), "class": nav_class})
        nav_label = ET.SubElement(nav_point, _q(NCX_NS, "navLabel"))
        ET.SubElement(nav_label, _q(NCX_NS, "text")).text = chapter_title
        ET.SubElement(nav_point, _q(NCX_NS, "content"), {"src": target})
    if page_targets:
        page_list = ET.SubElement(ncx, _q(NCX_NS, "pageList"))
        page_label = ET.SubElement(page_list, _q(NCX_NS, "navLabel"))
        ET.SubElement(page_label, _q(NCX_NS, "text")).text = "Páginas"
        for order, (page_number, target) in enumerate(page_targets, start=len(chapter_targets) + 1):
            page_target = ET.SubElement(page_list, _q(NCX_NS, "pageTarget"), {
                "id": f"page-{page_number}", "value": str(page_number),
                "type": "normal", "playOrder": str(order),
            })
            page_nav_label = ET.SubElement(page_target, _q(NCX_NS, "navLabel"))
            ET.SubElement(page_nav_label, _q(NCX_NS, "text")).text = str(page_number)
            ET.SubElement(page_target, _q(NCX_NS, "content"), {"src": target})
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
    if author:
        ET.SubElement(dc_metadata, _q(DC_NS, "Creator")).text = author
    if edition:
        ET.SubElement(dc_metadata, _q(DC_NS, "Description")).text = f"Edición: {edition}"
    if publication_year:
        ET.SubElement(dc_metadata, _q(DC_NS, "Date")).text = publication_year
    if publisher:
        ET.SubElement(dc_metadata, _q(DC_NS, "Publisher")).text = publisher
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
        ("full-audio", "book.mp3", "audio/mpeg"),
    ]
    items.extend((f"smil-{index:04d}", f"smil/{name}", "application/smil") for index, name in enumerate(smil_names, start=1))
    items.extend((f"audio-{index:04d}", f"audio/{name}", "audio/mpeg") for index, name in enumerate(audio_names, start=1))
    items.extend((f"chapter-audio-{index:04d}", f"audio/{name}", "audio/mpeg") for index, name in enumerate(chapter_audio_names, start=1))
    items.extend((f"front-audio-{index:04d}", f"audio/{name}", "audio/mpeg") for index, name in enumerate(front_names, start=1))
    for item_id, href, media_type in items:
        ET.SubElement(manifest, _q(OPF_NS, "item"), {"id": item_id, "href": href, "media-type": media_type})
    spine = ET.SubElement(package, _q(OPF_NS, "spine"), {"toc": "ncx"})
    for index, _name in enumerate(smil_names, start=1):
        ET.SubElement(spine, _q(OPF_NS, "itemref"), {"idref": f"smil-{index:04d}"})
    _write_xml(
        output_dir / "book.opf",
        package,
        '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN" "oebpkg12.dtd">',
    )
    return output_dir
