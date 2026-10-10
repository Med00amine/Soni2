"""Extract chapter structure and source bibliographic metadata."""

from __future__ import annotations

import io
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import ebooklib
import fitz
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import epub


@dataclass
class BookChapter:
    title: str
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class BookStructure:
    title: str
    author: str = ""
    edition: str = ""
    publication_year: str = ""
    publisher: str = ""
    chapters: list[BookChapter] = field(default_factory=list)

    def as_text(self) -> str:
        blocks: list[str] = []
        for chapter in self.chapters:
            if chapter.title:
                blocks.append(chapter.title)
            blocks.extend(chapter.paragraphs)
        return "\n\n".join(blocks)


def extract_book_structure(filename: str, content: bytes, fallback_text: str = "") -> BookStructure:
    """Preserve chapter boundaries where the source format exposes them."""
    path = Path(filename)
    title = path.stem or "Libro"
    result = BookStructure(title=title)

    if path.suffix.lower() == ".xml":
        return _from_xml(filename, content, result, fallback_text)
    if path.suffix.lower() == ".epub":
        return _from_epub(filename, content, result, fallback_text)
    if path.suffix.lower() == ".docx":
        return _from_docx(content, result, fallback_text)
    if path.suffix.lower() == ".pdf":
        return _from_pdf(content, result, fallback_text)
    if path.suffix.lower() == ".txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("El archivo de texto no usa una codificacion compatible.") from exc
        result.chapters = _split_text_chapters(result.title, text)
        return result

    result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _from_xml(filename: str, content: bytes, result: BookStructure, fallback_text: str) -> BookStructure:
    root = ET.fromstring(content)
    metadata: dict[str, str] = {}
    for node in root.iter():
        local = _local_name(node.tag).lower()
        name = (node.get("name") or "").strip().lower()
        value = (node.get("content") or " ".join("".join(node.itertext()).split())).strip()
        if name and value:
            metadata[name.removeprefix("dc:").removeprefix("ncc:")] = value
        elif local in {"title", "creator", "author", "edition", "publisher", "date"} and value:
            metadata.setdefault(local, value)

    result.title = metadata.get("title", result.title)
    result.author = metadata.get("creator", metadata.get("author", ""))
    result.edition = metadata.get("sourceedition", metadata.get("edition", ""))
    result.publication_year = metadata.get("sourcedate", metadata.get("date", ""))
    result.publisher = metadata.get("sourcepublisher", metadata.get("publisher", ""))

    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = list(root.iter())
    dtbook_levels = [node for node in nodes if _local_name(node.tag).lower() == "level1"]
    candidates = dtbook_levels or [node for node in nodes if _local_name(node.tag).lower() == "chapter"]
    top_level = [node for node in candidates if not any(parent in candidates for parent in _ancestors(node, parents))]
    if not top_level:
        top_level = [root]

    for index, chapter in enumerate(top_level, 1):
        blocks = [node for node in chapter.iter() if _local_name(node.tag).lower() in {"p", "para", "paragraph"}]
        heading = next(
            (node for node in chapter if _local_name(node.tag).lower() in {"h1", "h2", "h3", "h4", "h5", "h6", "title"}),
            None,
        )
        if heading is None and _local_name(chapter.tag).lower() == "chapter":
            heading = next((node for node in chapter if _local_name(node.tag).lower() == "title"), None)
        chapter_title = _text(heading) if heading is not None else f"Capítulo {index}"
        paragraphs = [_text(node) for node in blocks if _text(node)]
        if not paragraphs:
            paragraphs = _paragraphs(_text(chapter))
            if heading is not None and paragraphs and paragraphs[0] == chapter_title:
                paragraphs.pop(0)
        result.chapters.append(BookChapter(chapter_title, paragraphs))

    if not result.chapters or not any(chapter.paragraphs for chapter in result.chapters):
        result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _from_epub(filename: str, content: bytes, result: BookStructure, fallback_text: str) -> BookStructure:
    try:
        with tempfile.TemporaryDirectory(prefix="soni2-structure-") as temp_dir:
            source = Path(temp_dir) / (Path(filename).name or "book.epub")
            source.write_bytes(content)
            book = epub.read_epub(str(source))
    except Exception as exc:
        raise ValueError("No hemos podido abrir este EPUB. Comprueba que no este protegido con DRM y que sea un EPUB valido.") from exc

    result.title = _epub_metadata(book, "title") or result.title
    result.author = _epub_metadata(book, "creator")
    result.edition = _epub_metadata(book, "sourceedition")
    result.publication_year = _epub_metadata(book, "date")
    result.publisher = _epub_metadata(book, "publisher")
    for index, (item_id, _linear) in enumerate(book.spine, 1):
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for unwanted in soup(["script", "style", "nav", "header", "footer"]):
            unwanted.decompose()
        heading = soup.find(["h1", "h2", "h3"])
        chapter_title = " ".join(heading.get_text(" ", strip=True).split()) if heading else f"Capítulo {index}"
        paragraphs = [
            " ".join(node.get_text(" ", strip=True).split())
            for node in soup.find_all(["p", "li", "blockquote"])
        ]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph]
        if not paragraphs:
            paragraphs = _paragraphs(soup.get_text("\n\n", strip=True))
        result.chapters.append(BookChapter(chapter_title, paragraphs))
    if not result.chapters:
        result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _from_docx(content: bytes, result: BookStructure, fallback_text: str) -> BookStructure:
    document = Document(io.BytesIO(content))
    result.title = document.core_properties.title or result.title
    result.author = document.core_properties.author or ""
    chapters: list[BookChapter] = []
    active = BookChapter(result.title)
    for paragraph in document.paragraphs:
        text = " ".join(paragraph.text.split())
        if not text:
            continue
        if paragraph.style and paragraph.style.name.lower() in {"heading 1", "título 1", "titulo 1"}:
            if active.paragraphs:
                chapters.append(active)
            active = BookChapter(text)
        else:
            active.paragraphs.extend(_paragraphs(text))
    if active.paragraphs or not chapters:
        chapters.append(active)
    result.chapters = chapters
    if not any(chapter.paragraphs for chapter in result.chapters):
        result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _from_pdf(content: bytes, result: BookStructure, fallback_text: str) -> BookStructure:
    with fitz.open(stream=content, filetype="pdf") as pdf:
        result.chapters = [
            BookChapter(f"Página {index}", _paragraphs(page.get_text("text")))
            for index, page in enumerate(pdf, 1)
            if page.get_text("text").strip()
        ]
    if not result.chapters:
        result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _epub_metadata(book, name: str) -> str:
    values = book.get_metadata("DC", name) or []
    return " ".join(str(values[0][0]).split()) if values else ""


def _paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", block).strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


_EXPLICIT_CHAPTER_HEADING = re.compile(
    r"^(?:"
    r"(?:cap[ií]tulo|chapter|chapitre|capitolo|kapitel|parte|part|secci[oó]n|section)"
    r"\s+(?:[ivxlcdm]+|\d+|[a-záéíóúñ]+)(?:\s*[:.\-–—]\s*.*|\s+.*)?"
    r")$",
    re.IGNORECASE,
)


def _split_text_chapters(book_title: str, text: str) -> list[BookChapter]:
    """Split plain text only at explicit, standalone chapter/part headings."""
    blocks: list[tuple[str, bool]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [re.sub(r"\s+", " ", line).strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        # A heading must be an entire paragraph, never merely words at the
        # start of a prose paragraph. That avoids inventing chapter breaks.
        paragraph = " ".join(lines)
        is_heading = len(lines) == 1 and bool(_EXPLICIT_CHAPTER_HEADING.fullmatch(lines[0]))
        blocks.append((paragraph, is_heading))

    heading_indexes = [index for index, (_, is_heading) in enumerate(blocks) if is_heading]
    if len(heading_indexes) < 2:
        return [BookChapter(book_title, [paragraph for paragraph, _ in blocks])]

    chapters: list[BookChapter] = []
    preface = [paragraph for paragraph, is_heading in blocks[:heading_indexes[0]] if not is_heading]
    if preface:
        chapters.append(BookChapter("Prólogo", preface))
    active_title = ""
    active_paragraphs: list[str] = []
    for paragraph, is_heading in blocks[heading_indexes[0]:]:
        if is_heading:
            if active_title:
                chapters.append(BookChapter(active_title, active_paragraphs))
            active_title = paragraph
            active_paragraphs = []
        else:
            active_paragraphs.append(paragraph)
    if active_title:
        chapters.append(BookChapter(active_title, active_paragraphs))

    # Don't claim headings are chapters when the file only contains heading
    # labels with no readable chapter content.
    if len(chapters) < 2 or not any(chapter.paragraphs for chapter in chapters):
        return [BookChapter(book_title, [paragraph for paragraph, _ in blocks])]
    return chapters


def _text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ancestors(node: ET.Element, parents: dict[ET.Element, ET.Element]):
    current = parents.get(node)
    while current is not None:
        yield current
        current = parents.get(current)
