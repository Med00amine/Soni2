"""Build a deterministic, inspectable DAISY 3 package."""

import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from lxml import etree

from app.ingestion.models import AudioMetadata, Book, Sentence, SynchronizationPoint

from .ncx import build_ncx
from .opf import build_opf
from .smil import build_smil

DTBOOK_NS = "http://www.daisy.org/z3986/2005/dtbook/"


class DaisyBuilder:
    """Materialize DTBook, SMIL, NCX, OPF, resource, and audio files."""

    def build(
        self,
        book: Book,
        synchronization: Iterable[SynchronizationPoint],
        audio: Mapping[str, AudioMetadata],
        output_dir: Path,
    ) -> Path:
        output_dir = Path(output_dir)
        content_dir = output_dir / "content"
        audio_dir = output_dir / "audio"
        smil_dir = output_dir / "smil"
        for directory in (content_dir, audio_dir, smil_dir):
            directory.mkdir(parents=True, exist_ok=True)

        points = list(synchronization)
        audio_paths: dict[str, Path] = {}
        for point in points:
            target = audio_dir / point.audio_file.name
            if point.audio_file.resolve() != target.resolve():
                shutil.copy2(point.audio_file, target)
            audio_paths[point.text_id] = target

        content_path = content_dir / "book.xml"
        content_path.write_bytes(_build_dtbook(book))
        chapter_files: list[tuple[str, str]] = []
        for chapter in book.chapters:
            smil_name = f"{chapter.order:04d}-{chapter.id}.smil"
            smil_path = smil_dir / smil_name
            smil_path.write_bytes(build_smil(book, chapter, points, audio_paths))
            chapter_files.append((chapter.id, f"smil/{smil_name}"))

        (output_dir / "book.ncx").write_bytes(build_ncx(book, chapter_files))
        (output_dir / "book.opf").write_bytes(
            build_opf(book, output_dir, [path for path in smil_dir.glob("*.smil")], audio_paths.values())
        )
        (output_dir / "book.res").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><resources xmlns="http://www.daisy.org/z3986/2005/resource/"/>',
            encoding="utf-8",
        )
        return output_dir


def _build_dtbook(book: Book) -> bytes:
    root = etree.Element(f"{{{DTBOOK_NS}}}dtbook", nsmap={None: DTBOOK_NS}, version="2005-3")
    root.set("{http://www.w3.org/XML/1998/namespace}lang", book.language or "es")
    head = etree.SubElement(root, f"{{{DTBOOK_NS}}}head")
    for name, content in book.metadata.values.items():
        etree.SubElement(head, f"{{{DTBOOK_NS}}}meta", name=name, content=content)
    body = etree.SubElement(root, f"{{{DTBOOK_NS}}}book")
    bodymatter = etree.SubElement(body, f"{{{DTBOOK_NS}}}bodymatter")
    for chapter in book.chapters:
        chapter_node = etree.SubElement(bodymatter, f"{{{DTBOOK_NS}}}level1", id=chapter.id)
        _append_title(chapter_node, chapter.title, "h1")
        _append_paragraphs(chapter_node, chapter.paragraphs)
        for section in chapter.sections:
            _append_section(chapter_node, section, 2)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _append_section(parent: etree._Element, section, level: int) -> None:
    node = etree.SubElement(parent, f"{{{DTBOOK_NS}}}level{min(level, 6)}", id=section.id)
    _append_title(node, section.title, f"h{min(level, 6)}")
    _append_paragraphs(node, section.paragraphs)
    for nested in section.sections:
        _append_section(node, nested, level + 1)


def _append_title(parent: etree._Element, title: str | None, tag: str) -> None:
    if title:
        heading = etree.SubElement(parent, f"{{{DTBOOK_NS}}}{tag}")
        heading.text = title


def _append_paragraphs(parent: etree._Element, paragraphs) -> None:
    for paragraph in paragraphs:
        node = etree.SubElement(parent, f"{{{DTBOOK_NS}}}p", id=paragraph.id)
        for index, sentence in enumerate(paragraph.sentences):
            if index:
                node.text = (node.text or "") + " "
            sent = etree.SubElement(node, f"{{{DTBOOK_NS}}}sent", id=sentence.id)
            sent.text = sentence.original_text or sentence.text
