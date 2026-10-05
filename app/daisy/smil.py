"""DAISY SMIL 3 generation."""

from pathlib import Path

from lxml import etree

from app.ingestion.models import Book, Chapter, SynchronizationPoint

SMIL_NS = "http://www.w3.org/2001/SMIL20/Language"


def build_smil(
    book: Book,
    chapter: Chapter,
    points: list[SynchronizationPoint],
    audio_paths: dict[str, Path],
) -> bytes:
    root = etree.Element(f"{{{SMIL_NS}}}smil", nsmap={None: SMIL_NS})
    body = etree.SubElement(root, f"{{{SMIL_NS}}}body", id=chapter.id)
    seq = etree.SubElement(body, f"{{{SMIL_NS}}}seq", id=f"seq-{chapter.id}")
    ids = {sentence.id for sentence in _chapter_sentences(chapter)}
    for point in points:
        if point.text_id not in ids:
            continue
        par = etree.SubElement(seq, f"{{{SMIL_NS}}}par", id=f"par-{point.text_id}")
        etree.SubElement(par, f"{{{SMIL_NS}}}text", src=f"../content/book.xml#{point.text_id}")
        etree.SubElement(
            par,
            f"{{{SMIL_NS}}}audio",
            src=f"../audio/{audio_paths[point.text_id].name}",
            clipBegin=_format_time(point.clip_begin),
            clipEnd=_format_time(point.clip_end),
        )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _chapter_sentences(chapter: Chapter):
    for paragraph in chapter.paragraphs:
        yield from paragraph.sentences
    for section in chapter.sections:
        yield from _section_sentences(section)


def _section_sentences(section):
    for paragraph in section.paragraphs:
        yield from paragraph.sentences
    for nested in section.sections:
        yield from _section_sentences(nested)


def _format_time(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
