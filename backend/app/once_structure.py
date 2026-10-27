"""The structure of an ONCE audiobook, decided before anything is narrated.

Given the sections a reader chose to narrate and the book's own structure, this
builds the list of audio files the delivery is made of, in order:

1. **File 1**: "Título, De Autor" and ONCE's legal notice (page 1).
2. **File 2**: the bibliographic data (on the page the planilla gives).
3. **File 3**: the index, generated from the sections below, with the page each
   one starts on, and "Fin de índice.".
4. Then every chapter (level 1) and subchapter (level 2), one file each, with
   the book's text as the existing pipeline segments it. Notes are read
   "Nota N." … "Fin de nota N.", and the closing phrase ends the last one.

Nothing here narrates or changes how anything is narrated: it only decides
which texts make up which file. The same structure feeds the production job,
the direct download, the editor's outline and the package validation, so they
cannot drift apart.

Subchapters come from the book's own headings (DTBook ``level2``/``h2``, EPUB
``h2``–``h6``, Word "Título 2"), never from guessing; a heading deeper than the
first level below the chapter stays in its subchapter's text. Page markers
come from the book's page numbers (DTBook ``pagenum``, EPUB page breaks, a
PDF's pages, Word's "Page Number DAISY" style); the planilla supplies the pages
of files 1–3 and, for a book without page numbers, the pages of its index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import planilla as planilla_module
from app.narration_plan import SelectedChapter, is_generated

KIND_TITLE = "once_title"
KIND_BIBLIOGRAPHIC = "once_bibliographic"
KIND_INDEX = "once_index"
KIND_CHAPTER = "chapter"
KIND_SUBCHAPTER = "subchapter"
INITIAL_KINDS = (KIND_TITLE, KIND_BIBLIOGRAPHIC, KIND_INDEX)

_NOTE_ELEMENTS = {"note", "footnote", "endnote"}
_NOTE_TEXT = re.compile(r"^nota\s+(\d{1,4})\s*[.:]\s*(.*)$", re.I | re.S)
_UNNUMBERED_NOTE = re.compile(r"^nota\s*[.:]\s*(.*)$", re.I | re.S)


@dataclass
class Paragraph:
    text: str
    # text | legal | bibliographic | index_entry | note | closing
    role: str = "text"
    # Pages of the original that begin at this paragraph.
    page_breaks: list[str] = field(default_factory=list)
    note: int | None = None
    # Where the text can be corrected: its chapter and paragraph among the
    # sections the reader selected. None for texts the planilla supplies.
    source: dict | None = None
    # What the extractor recorded about it (page, element, method).
    origin: dict = field(default_factory=dict)

    def record(self) -> dict:
        return {"role": self.role, "page_breaks": self.page_breaks, "note": self.note, "source": self.source}


@dataclass
class Section:
    key: str
    kind: str
    level: int
    title: str
    title_generated: bool
    title_pages: list[str] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    title_source: dict | None = None
    # The page the section starts on: its own marker, or the page already in
    # effect when it begins. What the index says.
    page_start: str = ""
    page_end: str = ""

    @property
    def heading_spoken(self) -> bool:
        return not self.title_generated and bool(self.title.strip())

    @property
    def initial(self) -> bool:
        return self.kind in INITIAL_KINDS

    def record(self) -> dict:
        return {"key": self.key, "kind": self.kind, "level": self.level, "title_pages": self.title_pages,
                "page_start": self.page_start, "page_end": self.page_end,
                "title_source": self.title_source,
                "paragraphs": [paragraph.record() for paragraph in self.paragraphs]}


@dataclass
class OnceStructure:
    sections: list[Section]
    warnings: list[dict]
    resolved: planilla_module.Resolved

    def chapters(self) -> list[SelectedChapter]:
        """The sections as the pipeline's chapters: ``(title, paragraphs)``."""
        return [SelectedChapter(section.title, [paragraph.text for paragraph in section.paragraphs],
                                section.title_generated) for section in self.sections]

    def records(self) -> list[dict]:
        return [section.record() for section in self.sections]

    def as_dict(self) -> dict:
        return {
            "sections": [{**section.record(), "index": index, "file_number": index + 1, "title": section.title,
                          "title_generated": section.title_generated, "heading_spoken": section.heading_spoken,
                          "texts": [paragraph.text for paragraph in section.paragraphs]}
                         for index, section in enumerate(self.sections)],
            "warnings": self.warnings,
            "planilla": self.resolved.as_dict(),
        }


# --------------------------------------------------------------------------- #
# Provenance of the selected sections
# --------------------------------------------------------------------------- #

def selected_provenance(selected: list, structure) -> list[tuple[dict, list[dict]]]:
    """What the extractor recorded for each selected title and paragraph.

    The narration plan works on texts, so the records are found again by text,
    in reading order: a paragraph that occurs twice takes its occurrences in
    turn, as the production units do.
    """
    paragraph_index: dict[str, list[dict]] = {}
    title_index: dict[str, list[dict]] = {}
    if structure is not None:
        for chapter in structure.chapters:
            if chapter.title:
                title_index.setdefault(chapter.title, []).append(dict(chapter.title_source or {}))
            for number, paragraph in enumerate(chapter.paragraphs):
                paragraph_index.setdefault(paragraph, []).append(dict(chapter.source_of(number)))

    def take(index: dict[str, list[dict]], text: str) -> dict:
        found = index.get(text) or []
        if not found:
            return {}
        return found.pop(0) if len(found) > 1 else dict(found[0])

    result = []
    for chapter in selected:
        title, paragraphs = chapter[0], chapter[1]
        # A title the plan found among the paragraphs keeps that paragraph's record.
        title_origin = take(title_index, title) if title in title_index else take(paragraph_index, title)
        result.append((title_origin, [take(paragraph_index, paragraph) for paragraph in paragraphs]))
    return result


def regroup_provenance(original: list, provenance: list[tuple[dict, list[dict]]], regrouped: list
                       ) -> list[tuple[dict, list[dict]]]:
    """Provenance for chapters that regroup the same paragraphs in the same order.

    Used after the length-based navigation sections split a chapter-less book.
    """
    flat = [origin for _title, origins in provenance for origin in origins]
    texts = [paragraph for chapter in original for paragraph in chapter[1]]
    result, position = [], 0
    for chapter in regrouped:
        count = len(chapter[1])
        if [paragraph for paragraph in texts[position:position + count]] != list(chapter[1]):
            # Not a pure regrouping: fall back to no provenance rather than misattribute.
            return [({}, [{} for _ in chapter[1]]) for chapter in regrouped]
        result.append(({}, flat[position:position + count]))
        position += count
    return result


# --------------------------------------------------------------------------- #
# Building the structure
# --------------------------------------------------------------------------- #

def _strip_period(text: str) -> str:
    return text.strip().rstrip(".").strip()


def _normal(text: str) -> str:
    return planilla_module.normal_heading(text)


def _note(text: str, counter: int, plan: planilla_module.Planilla) -> tuple[str, int]:
    """ "Nota 1. …" read as the planilla says: "Nota 1. … Fin de nota 1."

    The note keeps its own number. A note whose text already ends with its
    closing words is not given them a second time.
    """
    body = text.strip()
    number = counter
    match = _NOTE_TEXT.match(body)
    if match:
        number, body = int(match.group(1)), match.group(2).strip()
    else:
        unnumbered = _UNNUMBERED_NOTE.match(body)
        if unnumbered:
            body = unnumbered.group(1).strip()
    opening = planilla_module.render(plan.note_open, n=number)
    closing = planilla_module.render(plan.note_close, n=number)
    if _normal(closing) and _normal(body).endswith(_normal(closing)):
        return f"{opening} {body}".strip(), number
    if body and body[-1] not in ".!?…»”\")":
        body += "."
    return f"{opening} {body} {closing}".strip(), number


def _body_sections(selected: list, provenance: list[tuple[dict, list[dict]]],
                   plan: planilla_module.Planilla, book_title: str) -> list[Section]:
    sections: list[Section] = []
    note_counter = 0
    for chapter_index, (chapter, (title_origin, origins)) in enumerate(zip(selected, provenance)):
        title, paragraphs = chapter[0], list(chapter[1])
        generated = is_generated(chapter) or not title.strip()
        title_level = int(title_origin.get("heading_level") or 1)
        headings = [int(origin.get("heading_level") or 0) for origin in origins
                    if origin.get("element") == "heading" and origin.get("heading_level")]
        deeper = [level for level in headings if level > title_level]
        sub_level = min(deeper) if deeper else None
        current = Section(f"s{chapter_index}", KIND_CHAPTER, 1, title, generated,
                          list(title_origin.get("page_breaks") or []), [], {"chapter": chapter_index})
        sections.append(current)
        for paragraph_index, (text, origin) in enumerate(zip(paragraphs, origins + [{}] * len(paragraphs))):
            element = origin.get("element")
            level = int(origin.get("heading_level") or 0)
            pages = list(origin.get("page_breaks") or [])
            source = {"chapter": chapter_index, "paragraph": paragraph_index}
            if element == "heading" and level and (level <= title_level or level == sub_level):
                # A heading of the chapter's own level is a sibling chapter; one
                # level below is a subchapter with its own audio file.
                is_sub = level == sub_level and level > title_level
                current = Section(f"s{chapter_index}.{paragraph_index}", KIND_SUBCHAPTER if is_sub else KIND_CHAPTER,
                                  2 if is_sub else 1, text, False, pages, [], source)
                sections.append(current)
                continue
            if element in _NOTE_ELEMENTS:
                note_counter += 1
                framed, number = _note(text, note_counter, plan)
                note_counter = max(note_counter, number)
                current.paragraphs.append(Paragraph(framed, "note", pages, number, source, origin))
                continue
            current.paragraphs.append(Paragraph(text, "text", pages, None, source, origin))
    # Pages of a PDF read page by page arrive as one untitled section per page;
    # the audiobook reads them as one section, with a page marker at each page.
    merged: list[Section] = []
    for section in sections:
        previous = merged[-1] if merged else None
        if (previous is not None and section.title_generated and not section.title.strip()
                and previous.title_generated and not previous.title.strip() and section.level == previous.level == 1):
            previous.paragraphs.extend(section.paragraphs)
            continue
        merged.append(section)
    result = []
    parent_chapter: int | None = None
    for section in merged:
        if not section.paragraphs and not section.heading_spoken:
            continue
        if not section.title.strip():
            # Navigation needs a label; the book does not say it, so it is not read.
            section.title = book_title or "Texto"
            section.title_generated = True
        chapter = (section.title_source or {}).get("chapter")
        if section.level == 2 and parent_chapter != chapter:
            # Its chapter had nothing to read and was left out: nesting it under
            # an earlier chapter would put it in the wrong place, so it is a
            # chapter itself.
            section.level, section.kind = 1, KIND_CHAPTER
        if section.level == 1:
            parent_chapter = chapter
        result.append(section)
    return result


def _assign_pages(sections: list[Section], plan: planilla_module.Planilla, warnings: list[dict]) -> None:
    """The page each section starts and ends on, in reading order."""
    in_effect = ""
    # True while the page in effect came from the planilla, not from the book.
    planned_only = False
    overrides = {_normal(heading): page for heading, page in plan.chapter_pages.items()}
    for index, section in enumerate(sections):
        if not section.initial and index and sections[index - 1].initial:
            # The pages of files 1-3 are the planilla's; the book's own pages
            # start with the book. A chapter is never given the index's page.
            in_effect, planned_only = "", False
        planned = overrides.get(_normal(section.title)) if not section.initial else None
        if section.title_pages:
            in_effect, planned_only = section.title_pages[-1], False
            section.page_start = section.title_pages[0]
            if planned and planned != section.page_start:
                warnings.append({"code": "page_conflict", "section": index,
                                 "message": f"La planilla sitúa «{section.title}» en la página {planned} y el libro "
                                            f"en la {section.page_start}; se usa la del libro."})
        elif planned and not any(paragraph.page_breaks for paragraph in section.paragraphs[:1]):
            # The book has no page number here; the planilla's index says
            # where it starts, and that is where its page marker goes.
            section.title_pages = [planned]
            section.page_start = in_effect = planned
            planned_only = True
        else:
            section.page_start = in_effect
            if in_effect and planned_only and not section.initial:
                # A page marker means "until the next one": the section is on
                # that page as far as the package is concerned, but nobody said so.
                warnings.append({"code": "page_inherited", "section": index,
                                 "message": f"«{section.title}» no tiene página propia ni en el libro ni en la "
                                            f"planilla; se considera en la página {in_effect}, la anterior."})
        for paragraph in section.paragraphs:
            if paragraph.page_breaks:
                in_effect, planned_only = paragraph.page_breaks[-1], False
        section.page_end = in_effect
        if section.initial and not section.title_pages:
            # Files 1-3 are on the page the planilla gives them, or on none
            # said: never on one inherited from the file before.
            section.page_start = section.page_end = ""


def build(selected: list, provenance: list[tuple[dict, list[dict]]], metadata: dict,
          plan: planilla_module.Planilla, *, today=None) -> OnceStructure:
    """The whole audiobook as ONCE structures it, from the selected sections."""
    resolved = planilla_module.resolve(plan, metadata, today=today)
    warnings: list[dict] = [{"code": "missing", "message": f"La planilla no indica: {item}."}
                            for item in resolved.missing]
    pair = planilla_module.title_and_author(resolved.title, resolved.author)
    values = {"title": resolved.title, "author": resolved.author, "title_and_author": pair}
    body = _body_sections(selected, provenance, plan, resolved.title)
    if not body:
        raise ValueError("No hay contenido del libro que narrar después de los archivos iniciales.")

    # The closing phrase ends the last chapter, once. If the book already says
    # it at the end it is marked rather than repeated.
    closing_text = planilla_module.render(plan.closing, **values)
    last = body[-1]
    present = [(section, paragraph) for section in body for paragraph in section.paragraphs
               if _normal(paragraph.text) == _normal(closing_text)]
    if present and present[-1][1] is (last.paragraphs[-1] if last.paragraphs else None):
        present[-1][1].role = "closing"
    elif present:
        warnings.append({"code": "closing_elsewhere",
                         "message": "La frase final ya aparece en el libro, pero no al final: revisa el texto."})
    else:
        last.paragraphs.append(Paragraph(closing_text, "closing"))

    file1 = Section("once-title", KIND_TITLE, 1, planilla_module.render(plan.title_line, **values), False,
                    [plan.file1_page] if plan.file1_page else [], [Paragraph(plan.legal_text, "legal")])
    lines = [pair, resolved.edition_line, resolved.publisher_line,
             f"ISBN {plan.isbn}" if plan.isbn else "",
             f"{plan.production_line} {resolved.production_date}".strip()]
    file2 = Section("once-bibliographic", KIND_BIBLIOGRAPHIC, 1, plan.bibliographic_heading, False,
                    [plan.file2_page] if plan.file2_page else [],
                    [Paragraph(line, "bibliographic") for line in lines if line.strip()])
    file3 = Section("once-index", KIND_INDEX, 1, plan.index_heading, False,
                    [plan.index_page] if plan.index_page else [], [])
    sections = [file1, file2, file3, *body]
    _assign_pages(sections, plan, warnings)

    # The index lists the sections as they really are, with the page each starts on.
    entries: list[Paragraph] = []
    if plan.index_page:
        entries.append(Paragraph(planilla_module.render(plan.index_self_entry, page=plan.index_page), "index_entry"))
    missing_pages = []
    for section in body:
        if section.level > plan.index_depth or not section.heading_spoken:
            continue
        heading = _strip_period(section.title)
        if section.page_start:
            entries.append(Paragraph(planilla_module.render(plan.index_entry, heading=heading,
                                                            page=section.page_start), "index_entry"))
        else:
            entries.append(Paragraph(f"{heading}.", "index_entry"))
            missing_pages.append(heading)
    entries.append(Paragraph(plan.index_end, "index_entry"))
    file3.paragraphs = entries
    if missing_pages:
        warnings.append({"code": "index_without_page",
                         "message": f"{len(missing_pages)} entradas del índice no tienen número de página: "
                                    + ", ".join(f"«{item}»" for item in missing_pages[:5])
                                    + ("…" if len(missing_pages) > 5 else "") + "."})

    for index, section in enumerate(sections):
        section.key = section.key if section.initial else f"{index + 1:03d}-{section.key}"

    _compare_with_planilla(sections, plan, warnings)
    _check_pages(sections, warnings)
    return OnceStructure(sections, warnings, resolved)


def _compare_with_planilla(sections: list[Section], plan: planilla_module.Planilla, warnings: list[dict]) -> None:
    """Report where the book differs from the files the planilla lists."""
    expected = {item["number"]: item["heading"] for item in plan.expected_files if item.get("number")}
    if not expected:
        return
    for number, heading in sorted(expected.items()):
        index = number - 1
        actual = sections[index].title if index < len(sections) else ""
        if _normal(actual) != _normal(heading):
            warnings.append({"code": "planilla_mismatch", "section": index if index < len(sections) else None,
                             "message": f"La planilla espera «{heading}» como archivo {number}; "
                                        + (f"el libro tiene «{actual}»." if actual else "el libro no llega a ese archivo.")})
    if len(sections) > max(expected):
        warnings.append({"code": "planilla_mismatch",
                         "message": f"El libro tiene {len(sections)} archivos y la planilla lista {max(expected)}."})


def _check_pages(sections: list[Section], warnings: list[dict]) -> None:
    seen: dict[str, int] = {}
    for index, section in enumerate(sections):
        for page in section.title_pages + [page for paragraph in section.paragraphs for page in paragraph.page_breaks]:
            if page in seen:
                warnings.append({"code": "duplicate_page", "section": index,
                                 "message": f"La página {page} se marca dos veces (archivos {seen[page] + 1} y "
                                            f"{index + 1})."})
            seen.setdefault(page, index)


# --------------------------------------------------------------------------- #
# From the structure to narration units and package layout
# --------------------------------------------------------------------------- #

def unit_layout(record: dict, paragraphs: list[str], heading_spoken: bool, split) -> list[dict]:
    """One layout entry per narration unit of a section, in order.

    ``split`` is the pipeline's own paragraph splitter (``make_paragraphs``),
    so a long paragraph's parts each get an entry; a page that begins at the
    paragraph begins at its first part.
    """
    entries: list[dict] = []
    if heading_spoken:
        entries.append({"role": "heading", "page_breaks": list(record.get("title_pages") or []), "note": None})
    details = record.get("paragraphs") or []
    for index, text in enumerate(paragraphs):
        detail = details[index] if index < len(details) else {}
        for part, _piece in enumerate(split(text)):
            entries.append({"role": detail.get("role") or "text",
                            "page_breaks": list(detail.get("page_breaks") or []) if part == 0 else [],
                            "note": detail.get("note"), "paragraph": index, "part": part})
    return entries
