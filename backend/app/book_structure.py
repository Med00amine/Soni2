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


def extract_book_structure(
    filename: str, content: bytes, fallback_text: str = "", *, pages: str | None = None,
) -> BookStructure:
    """Preserve chapter boundaries where the source format exposes them.

    ``pages`` ("20-114") limits a PDF to that page range, as numbered by a PDF
    viewer; other formats have no pages and ignore it.
    """
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
        return _from_pdf(content, result, fallback_text, pages)
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
    extracted: list[tuple[str, list[str], bool]] = []
    for index, (item_id, _linear) in enumerate(book.spine, 1):
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for unwanted in soup(["script", "style", "nav", "header", "footer"]):
            unwanted.decompose()
        notes = _epub_notes(soup)
        heading = soup.find(["h1", "h2", "h3"])
        chapter_title = _node_text(heading) if heading else f"Capítulo {index}"
        paragraphs = [_node_text(node) for node in _epub_blocks(soup, heading)]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph] + notes
        if not paragraphs:
            paragraphs = _paragraphs(soup.get_text("\n\n", strip=True))
        if not paragraphs:
            continue
        item_name = item.get_name()
        item_stem = Path(item_name).stem.casefold()
        if item_stem in {"cover", "coverpage", "cover-page", "titlepage", "title-page", "portada", "cubierta", "couverture", "capa", "frontcover"}:
            continue
        item_marker = re.search(r"(?:cap|chapter|chapitre|capitolo)[-_ ]*\d+", f"{item_id} {item_stem}", flags=re.IGNORECASE)
        explicit_chapter = bool(_EXPLICIT_CHAPTER_HEADING.fullmatch(chapter_title) or item_marker)
        extracted.append((chapter_title, paragraphs, explicit_chapter))
    # Keep spine order and meaningful prologues/front matter. The narration
    # policy classifies contents pages; the parser must not silently truncate.
    result.chapters = [BookChapter(title, paragraphs) for title, paragraphs, _ in extracted]
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


def _from_pdf(content: bytes, result: BookStructure, fallback_text: str, pages: str | None = None) -> BookStructure:
    with fitz.open(stream=content, filetype="pdf") as pdf:
        span = parse_page_range(pages, pdf.page_count)
        metadata = pdf.metadata or {}
        result.title = (metadata.get("title") or "").strip() or result.title
        result.author = (metadata.get("author") or "").strip()
        result.chapters = _pdf_layout_chapters(pdf, span)
        if not result.chapters:
            first, last = span or (1, pdf.page_count)
            result.chapters = [
                BookChapter(f"Página {index}", _pdf_paragraphs(pdf[index - 1].get_text("text")))
                for index in range(first, last + 1)
                if pdf[index - 1].get_text("text").strip()
            ]
    if not result.chapters:
        result.chapters = [BookChapter(result.title, _paragraphs(fallback_text))]
    return result


def _pdf_paragraphs(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    has_index_heading = any(re.fullmatch(r"(?:índice(?: general)?|tabla de contenidos?|contenido|sumario|contents|index)", line, re.IGNORECASE) for line in lines)
    has_leaders = sum(bool(re.search(r"(?:\.{2,}|…{2,})\s*(?:[ivxlcdm]+|\d{1,4})\s*$", line, re.IGNORECASE)) for line in lines) >= 2
    if lines and (has_index_heading or has_leaders):
        return lines
    return _paragraphs(text)


_NOTE_TYPES = {"footnote", "endnote", "rearnote", "note"}
_BLOCK_TAGS = ["h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"]


def _epub_semantics(node) -> set[str]:
    values = f"{node.get('epub:type', '')} {node.get('role', '')}".replace("doc-", " ")
    return set(values.split())


def _epub_notes(soup) -> list[str]:
    """Take the notes out of the flow and their reference marks out of the text.

    A note reference read aloud became "... antes de que llegara. uno"; the
    note itself is read at the end of the chapter, as "Nota 1. ...".
    """
    for mark in soup.find_all(lambda node: "noteref" in _epub_semantics(node)):
        mark.decompose()
    notes = []
    for note in soup.find_all(lambda node: _epub_semantics(node) & _NOTE_TYPES):
        text = _node_text(note)
        note.decompose()
        match = re.match(r"^\s*(\d{1,3})[.)\s]+(.*)$", text, flags=re.S)
        if match:
            notes.append(f"Nota {match.group(1)}. {match.group(2).strip()}")
        elif text:
            notes.append(f"Nota. {text}")
    return notes


def _epub_blocks(soup, title_heading) -> list:
    """Paragraphs and sub-headings in reading order, each once.

    Sub-headings (h2 and below) used to be dropped, so a listener never heard
    them; a paragraph inside a quotation used to be read twice.
    """
    blocks = []
    for node in soup.find_all(_BLOCK_TAGS):
        if node is title_heading:
            continue
        if any(parent.name in {"p", "li", "blockquote"} for parent in node.parents):
            continue
        blocks.append(node)
    return blocks


def _node_text(node) -> str:
    # Inline markup (<i>, <b>, a <span> in the middle of a word) must not split
    # words, so the text is joined as is and only its white space normalized.
    for line_break in node.find_all("br"):
        line_break.replace_with(" ")
    return " ".join(node.get_text("").split())


def _epub_metadata(book, name: str) -> str:
    values = book.get_metadata("DC", name) or []
    return " ".join(str(values[0][0]).split()) if values else ""


def _paragraphs(text: str) -> list[str]:
    """Split into paragraphs, rejoining words a line break split in two.

    Books hyphenate a word that does not fit on a line. Collapsing the
    whitespace on its own left "distribui- dores", which a voice reads as two
    broken pieces and a listener hears as a stutter. Every format goes through
    here, so EPUB, DOCX, XML and plain text get the same repair the
    layout-aware PDF reader already did.

    The soft hyphen (U+00AD) is an invisible "you may break here" mark and is
    simply removed.
    """
    blocks = re.split(r"\n\s*\n", text.replace("­", ""))
    return [joined for block in blocks if block.strip()
            if (joined := _join_lines(block.splitlines()))]


_EXPLICIT_CHAPTER_HEADING = re.compile(
    r"^(?:"
    r"(?:cap[ií]tulo|chapter|chapitre|capitolo|kapitel|parte|part|secci[oó]n|section)"
    r"\s+(?:[ivxlcdm]+|\d+|primero|primera|segundo|segunda|tercero|tercera|"
    r"cuarto|cuarta|quinto|quinta|sexto|sexta|séptimo|séptima|octavo|octava|"
    r"noveno|novena|décimo|décima|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*[:\-–—]\s*.*)?\.?"
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
        # Preserve line boundaries in contents lists so the narration planner
        # can recognize leaders/page references instead of seeing one merged blob.
        structural = len(lines) > 1 and any(
            _EXPLICIT_CHAPTER_HEADING.fullmatch(line)
            or re.fullmatch(r"(?:indice(?: general)?|tabla de contenidos?|contenido|sumario|contents|index)", line, re.IGNORECASE)
            or re.search(r"(?:\.{2,}|…{2,})\s*(?:[ivxlcdm]+|\d{1,4})\s*$", line, re.IGNORECASE)
            for line in lines
        )
        if structural:
            blocks.extend((line, bool(_EXPLICIT_CHAPTER_HEADING.fullmatch(line))) for line in lines)
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


# --------------------------------------------------------------------------- #
# PDF: read the page layout, not just its text
# --------------------------------------------------------------------------- #
#
# get_text("text") gives one undivided run of lines per page. Narrated as is,
# that reads the running header and the page number aloud on every page,
# splits words hyphenated at line ends ("distribui- dores", which a voice says
# as two broken pieces and sounds like a stutter), and cuts every sentence that
# continues on the next page. The layout says which line is which: running
# heads and page numbers sit in the margins, chapter titles are set larger than
# the body, footnotes smaller and at the foot of the page.

_SENTENCE_END = re.compile(r"[.!?…:;»”\")\]]\s*$")
_FOOTNOTE_START = re.compile(r"^\s*(\d{1,3})[\s\t.)]+(.*)$", re.S)


def parse_page_range(value: str | None, page_count: int) -> tuple[int, int] | None:
    """``"20-114"`` -> (20, 114), clamped to the document; blank -> None."""
    text = (value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)\s*(?:[-–—a]|hasta)?\s*(\d+)?", text, flags=re.I)
    if not match:
        raise ValueError("Escribe las páginas como «20-114».")
    first = int(match.group(1))
    last = int(match.group(2) or match.group(1))
    if first < 1 or last < first:
        raise ValueError("El rango de páginas no es válido: la primera página debe ser menor que la última.")
    if first > page_count:
        raise ValueError(f"El documento solo tiene {page_count} páginas.")
    return first, min(last, page_count)


def _line_text(spans: list[dict], footnote_numbers: set[str]) -> str:
    """A line's text without the footnote markers glued to the word before them."""
    parts: list[str] = []
    for index, span in enumerate(spans):
        text = span["text"]
        marker = text.strip()
        previous = parts[-1] if parts else ""
        if (marker in footnote_numbers and index > 0 and previous and not previous[-1].isspace()
                and not previous[-1].isdigit()):
            continue
        parts.append(text)
    return "".join(parts)


def _join_lines(lines: list[str]) -> str:
    """Join a block's lines, undoing the hyphenation of words split at a line end."""
    text = ""
    for line in (value.strip() for value in lines):
        if not line:
            continue
        if text.endswith("-") and len(text) > 1 and text[-2].isalpha():
            # "discapa-" + "cidad" is one word; "Covid-" + "19" keeps its hyphen.
            text = text[:-1] + line if line[:1].islower() else text + line
        else:
            text = f"{text} {line}" if text else line
    return re.sub(r"\s+", " ", text).strip()


def _heading_text(lines: list[str]) -> str:
    """"Capítulo 1" over "El arte de hablar..." reads as "Capítulo 1. El arte de hablar..."."""
    parts = [line.strip() for line in lines if line.strip()]
    if len(parts) > 1 and _EXPLICIT_CHAPTER_HEADING.fullmatch(parts[0]):
        return f"{parts[0]}. {_join_lines(parts[1:])}"
    return _join_lines(parts)


def _pdf_layout_chapters(pdf, pages: tuple[int, int] | None) -> list[BookChapter]:
    first, last = pages or (1, pdf.page_count)
    blocks = []  # (page, kind-independent data)
    sizes: dict[float, int] = {}
    for number in range(first, last + 1):
        page = pdf[number - 1]
        height = page.rect.height
        for block in page.get_text("dict", sort=True)["blocks"]:
            if block.get("type") != 0 or not block.get("lines"):
                continue
            lines = [line["spans"] for line in block["lines"] if line["spans"]]
            text_size = [(span["size"], len(span["text"].strip())) for spans in lines for span in spans]
            chars = sum(count for _size, count in text_size)
            if not chars:
                continue
            size = max(text_size, key=lambda item: item[1])[0]
            for value, count in text_size:
                sizes[round(value * 2) / 2] = sizes.get(round(value * 2) / 2, 0) + count
            blocks.append({"page": number, "top": block["bbox"][1] / height, "bottom": block["bbox"][3] / height,
                           "size": size, "lines": lines})
    if not blocks or not sizes:
        return []
    body = max(sizes, key=sizes.get)
    if not any(block["size"] >= body + 2.5 for block in blocks):
        return []  # no headings to go by: keep the page-by-page reading

    # Footnote numbers per page, so their markers can be taken out of the text.
    for block in blocks:
        raw = "".join(span["text"] for span in block["lines"][0])
        match = _FOOTNOTE_START.match(raw)
        block["kind"] = "body"
        if block["top"] < 0.09 and block["size"] < body:
            block["kind"] = "margin"  # running head
        elif block["top"] > 0.9 and re.fullmatch(r"[\divxlc\s]+", "".join(
                span["text"] for spans in block["lines"] for span in spans), re.I):
            block["kind"] = "margin"  # page number
        elif block["size"] <= body - 1.25 and block["top"] > 0.6 and match:
            block["kind"] = "footnote"
            block["number"] = match.group(1)
        elif block["size"] >= body + 2.5:
            block["kind"] = "heading"
    notes_on_page: dict[int, set[str]] = {}
    for block in blocks:
        if block["kind"] == "footnote":
            notes_on_page.setdefault(block["page"], set()).add(block["number"])

    chapters: list[BookChapter] = []
    notes: list[str] = []
    current = BookChapter("")
    pending_heading: list[str] = []

    def close_chapter() -> None:
        nonlocal current, notes
        if notes:
            current.paragraphs.extend(notes)
            notes = []
        if current.paragraphs or current.title:
            chapters.append(current)

    for index, block in enumerate(blocks):
        numbers = notes_on_page.get(block["page"], set())
        lines = [_line_text(spans, numbers) for spans in block["lines"]]
        if block["kind"] == "margin":
            continue
        if block["kind"] == "heading":
            pending_heading.extend(lines)
            following = blocks[index + 1] if index + 1 < len(blocks) else None
            if following and following["kind"] == "heading" and following["page"] == block["page"]:
                continue  # a title set over several blocks
            close_chapter()
            current = BookChapter(_heading_text(pending_heading))
            pending_heading = []
            continue
        if block["kind"] == "footnote":
            text = _join_lines(lines)
            match = _FOOTNOTE_START.match(text)
            if match:
                notes.append(f"Nota {match.group(1)}. {match.group(2).strip()}")
            continue
        text = _join_lines(lines)
        if not text:
            continue
        previous = current.paragraphs[-1] if current.paragraphs else ""
        continues = (previous and block is _first_body_on_page(blocks, block["page"])
                     and not _SENTENCE_END.search(previous))
        if continues:
            # The paragraph carries on from the previous page.
            if previous.endswith("-") and text[:1].islower():
                current.paragraphs[-1] = previous[:-1] + text
            else:
                current.paragraphs[-1] = f"{previous} {text}"
        else:
            current.paragraphs.append(text)
    close_chapter()
    return [chapter for chapter in chapters if chapter.paragraphs]


def _first_body_on_page(blocks: list[dict], page: int) -> dict | None:
    return next((block for block in blocks if block["page"] == page and block["kind"] == "body"), None)
