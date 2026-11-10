"""The book's own table of contents: its title, its entries and the pages it gives.

A book calls its table of contents "Índice", "Sumario", "Contenido", "Tabla de
contenidos"… The narration plan already finds it (by its title, or by its rows
of "heading …… page"), and excludes it from the narration by default. This
module reads it instead of discarding it:

* its **title as the book writes it**, so the audiobook's index file can say
  "Sumario." where the book says sumario, rather than always "Índice.";
* its **entries** and the **page** each one gives, arabic or roman, so a book
  without page markers in its text still gets the start page of each chapter
  from its own table of contents.

Nothing is invented: an entry without a page number has none, a number that
belongs to the label ("Capítulo 1") is not a page, and entries whose pages run
backwards are reported, not corrected.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from app.spanish_speech import roman_to_int

# What a table of contents is called, as its own heading.
TOC_TITLE = re.compile(
    r"^(?:[íi]ndice(?:\s+general)?|sumario|contenidos?|tabla\s+de\s+contenidos?|table\s+of\s+contents|contents"
    r"|sommaire)\s*[.:]?$", re.IGNORECASE)
# A page reference at the end of a row, after leaders or a clear gap.
_LEADERS = re.compile(r"^(?P<label>.*?\S)\s*(?:\.{2,}|…+|·{2,}|_{2,}|-{3,}|\t)\s*(?P<page>\d{1,4}|[ivxlcdm]{1,8})\s*$",
                      re.IGNORECASE)
_GAP = re.compile(r"^(?P<label>.*?\S)\s{2,}(?P<page>\d{1,4}|[ivxlcdm]{1,8})\s*$", re.IGNORECASE)
# One space before an arabic number: still a page, unless the number belongs to the label.
_SPACE = re.compile(r"^(?P<label>.*?\S)\s(?P<page>\d{1,4})\s*$")
_LABEL_NUMBERED = re.compile(r"(?:cap[ií]tulo|parte|libro|secci[oó]n|tomo|volumen|anexo|ap[eé]ndice|lecci[oó]n|tema"
                             r"|chapter|part|book|section)$", re.IGNORECASE)


@dataclass
class TocEntry:
    label: str
    # The page as the book prints it ("12", "ix"), or "" when it gives none.
    page: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceToc:
    """A book's own table of contents."""

    # As the book writes it, "" when the book gives it no title.
    title: str
    entries: list[TocEntry] = field(default_factory=list)
    # The narration-plan sections it was read from.
    section_ids: list[str] = field(default_factory=list)
    # Entries whose page runs backwards: reported, never corrected.
    out_of_order: list[str] = field(default_factory=list)

    @property
    def paged(self) -> list[TocEntry]:
        return [entry for entry in self.entries if entry.page]

    def as_dict(self) -> dict:
        return {"title": self.title, "entries": [entry.as_dict() for entry in self.entries],
                "section_ids": self.section_ids, "out_of_order": self.out_of_order}


def _valid_page(label: str) -> bool:
    return label.isdigit() or roman_to_int(label.upper()) is not None


def parse_entry(line: str) -> TocEntry:
    """One row of a table of contents: its label and the page it gives, if any."""
    text = line.strip()
    for pattern in (_LEADERS, _GAP):
        match = pattern.match(text)
        if match and _valid_page(match.group("page")):
            return TocEntry(_clean_label(match.group("label")), match.group("page").lower())
    match = _SPACE.match(" ".join(text.split()))
    if match and not _LABEL_NUMBERED.search(match.group("label").strip()):
        return TocEntry(_clean_label(match.group("label")), match.group("page"))
    return TocEntry(_clean_label(text), "")


def _clean_label(label: str) -> str:
    return " ".join(label.split()).strip(" .·…_-\t")


def _page_value(page: str) -> tuple[int, int] | None:
    """Order of a page label: roman front matter before arabic pages."""
    if page.isdigit():
        return 1, int(page)
    value = roman_to_int(page.upper())
    return (0, value) if value is not None else None


def detect(plan: list) -> SourceToc | None:
    """The table of contents among the narration plan's sections, or None.

    ``plan`` is the narration plan (``NarrationSection`` objects); the ones it
    classified as ``toc`` are read, consecutive ones as one table of contents.
    """
    sections = [section for section in plan if getattr(section, "category", "") == "toc"]
    if not sections:
        return None
    title = ""
    entries: list[TocEntry] = []
    for section in sections:
        if not title and not section.title_generated and TOC_TITLE.match(section.title.strip()):
            title = section.title.strip().rstrip(".:").strip()
        for paragraph in section.paragraphs:
            for line in paragraph.splitlines() or [paragraph]:
                if not line.strip():
                    continue
                if TOC_TITLE.match(line.strip()):
                    title = title or line.strip().rstrip(".:").strip()
                    continue
                entry = parse_entry(line)
                if entry.label:
                    entries.append(entry)
    toc = SourceToc(title, entries, [section.id for section in sections])
    previous = None
    for entry in toc.paged:
        value = _page_value(entry.page)
        if value is not None and previous is not None and value < previous:
            toc.out_of_order.append(f"{entry.label} ({entry.page})")
        previous = value if value is not None else previous
    return toc


def normal(text: str) -> str:
    """For matching an entry with a heading: accents, case and punctuation do not count."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(character for character in folded if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^\w\s]", " ", letters).split())


def match_sections(toc: SourceToc, headings: list[str]) -> dict[int, TocEntry]:
    """Which entry gives the page of which heading, by position in ``headings``.

    Entries and headings are both in the book's order, so they are matched in
    order: an entry matches a heading that says the same words, or that ends
    with them ("La llegada" for "Capítulo 1. La llegada") or begins with them
    ("Capítulo 1" for "Capítulo 1. La llegada"), when no other heading does.
    An entry nothing matches is left out, never forced onto a heading.
    """
    normalized = [normal(heading) for heading in headings]
    result: dict[int, TocEntry] = {}
    start = 0
    for entry in toc.entries:
        label = normal(entry.label)
        if not label:
            continue
        exact = [index for index in range(start, len(normalized)) if normalized[index] == label]
        partial = [index for index in range(start, len(normalized)) if index not in result and len(label) >= 5
                   and (normalized[index].endswith(" " + label) or normalized[index].startswith(label + " "))]
        candidates = exact or (partial if len(partial) == 1 else [])
        if candidates:
            result[candidates[0]] = entry
            start = candidates[0] + 1
    return result
