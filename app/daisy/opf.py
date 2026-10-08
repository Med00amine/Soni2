"""DAISY OPF package manifest generation."""

from collections.abc import Iterable
from pathlib import Path

from lxml import etree

from app.ingestion.models import Book

OPF_NS = "http://openebook.org/namespaces/oeb-package/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"


def build_opf(book: Book, output_dir: Path, smil_paths: Iterable[Path], audio_paths: Iterable[Path]) -> bytes:
    root = etree.Element(
        f"{{{OPF_NS}}}package",
        nsmap={None: OPF_NS, "dc": DC_NS},
        attrib={"unique-identifier": "uid", "version": "1.2"},
    )
    metadata = etree.SubElement(root, f"{{{OPF_NS}}}metadata")
    etree.SubElement(metadata, f"{{{DC_NS}}}Title").text = book.title or book.id
    etree.SubElement(metadata, f"{{{DC_NS}}}Language").text = book.language or "es"
    identifier = etree.SubElement(metadata, f"{{{DC_NS}}}Identifier", id="uid")
    identifier.text = book.id
    if book.author:
        etree.SubElement(metadata, f"{{{DC_NS}}}Creator").text = book.author
    manifest = etree.SubElement(root, f"{{{OPF_NS}}}manifest")
    etree.SubElement(manifest, f"{{{OPF_NS}}}item", id="dtbook", href="content/book.xml", **{"media-type": "application/x-dtbook+xml"})
    etree.SubElement(manifest, f"{{{OPF_NS}}}item", id="ncx", href="book.ncx", **{"media-type": "application/x-dtbncx+xml"})
    etree.SubElement(manifest, f"{{{OPF_NS}}}item", id="resource", href="book.res", **{"media-type": "application/x-dtbresource+xml"})
    for index, path in enumerate(sorted(smil_paths), start=1):
        etree.SubElement(manifest, f"{{{OPF_NS}}}item", id=f"smil-{index}", href=f"smil/{path.name}", **{"media-type": "application/smil"})
    for index, path in enumerate(sorted(audio_paths), start=1):
        etree.SubElement(manifest, f"{{{OPF_NS}}}item", id=f"audio-{index}", href=f"audio/{path.name}", **{"media-type": "audio/wav"})
    spine = etree.SubElement(root, f"{{{OPF_NS}}}spine", toc="ncx")
    for index, _ in enumerate(sorted(smil_paths), start=1):
        etree.SubElement(spine, f"{{{OPF_NS}}}itemref", idref=f"smil-{index}")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
