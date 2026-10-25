"""Conservative section classification shared by preview and production."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass
class NarrationSection:
    id: str
    title: str
    category: str
    paragraphs: list[str]
    included: bool
    needs_review: bool = False
    reason: str = ""
    # The extractor invented the title ("Página 3", "Capítulo 2"): it labels
    # navigation but the book never says it, so it is not narrated.
    title_generated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "category": self.category,
                "paragraph_count": len(self.paragraphs), "included": self.included,
                "needs_review": self.needs_review, "reason": self.reason,
                "title_generated": self.title_generated}


class SelectedChapter(tuple):
    """``(title, paragraphs)`` that also knows whether its title is invented.

    A tuple so every caller that unpacks ``title, paragraphs`` keeps working.
    """

    title_generated: bool = False

    def __new__(cls, title: str, paragraphs: list[str], title_generated: bool = False):
        instance = super().__new__(cls, (title, paragraphs))
        instance.title_generated = title_generated
        return instance


def is_generated(chapter) -> bool:
    return bool(getattr(chapter, "title_generated", False))


def _norm(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)).strip(" .:-_—–")


_TOC = re.compile(r"^(indice(?: general)?|tabla de contenido|tabla de contenidos|contenido|sumario|contents|table of contents|sommaire|index)$", re.I)
_SYNOPSIS = re.compile(r"^(sinopsis|resumen|summary|synopsis|resum[eé])$", re.I)
_BACK = re.compile(r"^(bibliografia|referencias|references|glosario|glossary|indice analitico|appendix|apendice|apendices)$", re.I)
_INTRO = re.compile(r"^(prologo|prefacio|introduccion|introducción|foreword|preface|introduction|preliminares|epilogo|epilogue|afterword|nota del autor|author.?s note)$", re.I)
_CHAPTER = re.compile(
    r"^(capitulo|chapter|chapitre|parte|part|seccion|section)\s+"
    r"(?:[ivxlcdm]+|\d+|primero|primera|segundo|segunda|tercero|tercera|"
    r"cuarto|cuarta|quinto|quinta|sexto|sexta|septimo|septima|octavo|octava|"
    r"noveno|novena|decimo|decima|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s*[:\-–—]\s*.*)?\.?$", re.I,
)
_TOC_ROW = re.compile(r"(?:\.{2,}|…{2,}|\s{2,})(?:\s*(?:[ivxlcdm]+|\d{1,4})\s*)$", re.I)
_NUMBERED_PAGE = re.compile(r"^\s*(?:[ivxlcdm]{1,8}|\d{1,4})\s*[.)\-:]?\s+\S.{0,100}(?:\.{2,}|…{2,})\s*(?:[ivxlcdm]+|\d{1,4})\s*$", re.I)

# A page label such as "Página 7" carries no information about what is on the
# page, so pages are classified from their own content instead.
_PAGE_LABEL = re.compile(r"^p[aá]gina\s+\d+$", re.I)
# Wording that only appears on a copyright / legal deposit page.
_COPYRIGHT_MARKERS = (
    "isbn", "deposito legal", "depósito legal", "todos los derechos reservados",
    "reservados todos los derechos", "derechos reservados", "all rights reserved",
    "printed in", "impreso en espana", "impreso en españa", "queda prohibida",
    "sin la autorizacion escrita", "sin la autorización escrita", "copyright",
)
_EDITION_MARKER = re.compile(r"(primera|segunda|tercera|cuarta|quinta)\s+edici[oó]n|©|\(c\)\s*\d{4}", re.I)
_SENTENCE_END = re.compile(r"[.!?…]\s*$")


def _looks_like_contents(paragraphs: list[str]) -> bool:
    """True when most short lines on a page are "heading .... page number" rows."""
    candidates = [item for item in paragraphs if item.strip()]
    if len(candidates) < 3:
        return False
    rows = sum(bool(_TOC_ROW.search(item) or _NUMBERED_PAGE.match(item)) for item in candidates)
    return rows >= 3 and rows >= len(candidates) * 0.6


def _looks_like_copyright(paragraphs: list[str]) -> bool:
    """True for a legal / copyright page, which is never narrative content."""
    joined = _norm(" ".join(paragraphs))
    if not joined:
        return False
    hits = sum(marker in joined for marker in _COPYRIGHT_MARKERS)
    return hits >= 1 and bool(_EDITION_MARKER.search(" ".join(paragraphs)) or hits >= 2)


def _looks_like_title_page(paragraphs: list[str]) -> bool:
    """True for a cover/title page: a few short lines and no running prose."""
    candidates = [item.strip() for item in paragraphs if item.strip()]
    if not candidates or len(candidates) > 6:
        return False
    if any(len(item) > 90 for item in candidates):
        return False
    # Real prose ends in sentence punctuation; a title block usually does not.
    return sum(bool(_SENTENCE_END.search(item)) for item in candidates) <= len(candidates) // 3


def _classify_by_content(
    paragraphs: list[str], *, narrative_started: bool, structural_only: bool = False,
) -> tuple[str, str] | None:
    """Classify a section from its own text rather than its heading.

    Returns ``(category, reason)`` only for confident matches. Anything
    uncertain is left alone so it stays in the narration and gets flagged,
    rather than being silently dropped.

    ``structural_only`` keeps just the two strict detectors, for sections that
    already carry a meaningful heading: a heading cannot turn a contents page
    or a copyright page into narrative, but a weak title-page shape must not
    override a real "Prólogo".
    """
    if _looks_like_contents(paragraphs):
        return "toc", "Página de índice detectada por sus referencias de página; excluida por defecto."
    if _looks_like_copyright(paragraphs):
        return "front", "Página de créditos o aviso legal detectada; excluida por defecto."
    if structural_only:
        return None
    # A title block is front matter only before the narrative has begun. The
    # same shape later in a book is usually real content, such as a page of
    # dialogue with no sentence-ending punctuation.
    if not narrative_started and _looks_like_title_page(paragraphs):
        return "front", "Portada o página de título detectada; excluida por defecto."
    return None


def _is_narrative_prose(paragraphs: list[str]) -> bool:
    """True once a section carries running prose, meaning the book has begun.

    One full sentence of reasonable length is enough: after it, a later short
    page is far more likely to be dialogue than a second title page.
    """
    return any(len(item.strip()) > 40 and _SENTENCE_END.search(item.strip()) for item in paragraphs)


def build_narration_plan(chapters: list[tuple]) -> list[NarrationSection]:
    """Split obvious preliminary/index/back matter; ambiguous prose is kept and flagged.

    Each chapter is ``(title, paragraphs)`` or ``(title, paragraphs,
    title_generated)``.
    """
    raw: list[tuple[str, list[str], str | None]] = []
    generated_raw: list[bool] = []
    for chapter in chapters:
        title, paras = chapter[0], chapter[1]
        title_generated = bool(chapter[2]) if len(chapter) > 2 else is_generated(chapter)
        # Some extractors flatten a whole book into one chapter; others call
        # all text before Chapter 1 "Prólogo". Inspect paragraph headings too.
        active_title, active, active_category = title, [], _category(title)
        active_generated = title_generated
        for paragraph in paras:
            label = _category(paragraph) if len(paragraph) <= 100 else None
            is_chapter = bool(_CHAPTER.fullmatch(paragraph.strip()))
            if label or is_chapter:
                if active or active_title:
                    if active:
                        raw.append((active_title, active, active_category))
                        generated_raw.append(active_generated)
                    elif active_category == "toc":
                        raw.append((active_title, active, active_category))
                        generated_raw.append(active_generated)
                # A heading found among the paragraphs is the book's own text.
                active_title, active, active_category = paragraph, [], label or "main"
                active_generated = False
            else:
                active.append(paragraph)
        if active or active_title:
            raw.append((active_title, active, active_category))
            generated_raw.append(active_generated)

    # Detect TOC from an explicit label or a repeated page-reference pattern.
    toc_mode = False
    normalized: list[tuple[str, list[str], str | None]] = []
    generated: list[bool] = []
    for (title, paras, category), title_generated in zip(raw, generated_raw):
        if category == "toc":
            toc_mode = True
            normalized.append((title, paras, category))
            generated.append(title_generated)
            continue
        if toc_mode:
            rows = [p for p in paras if _TOC_ROW.search(p) or _NUMBERED_PAGE.match(p)]
            prose = [p for p in paras if p not in rows]
            if rows:
                normalized.append((title or "Índice", rows, "toc"))
                generated.append(title_generated or not title)
                if prose:
                    normalized.append((title, prose, _category(title) or "main"))
                    generated.append(title_generated)
                continue
            # stop treating later narrative as TOC at an explicit chapter or prose
            if category == "main" or _CHAPTER.fullmatch(title.strip()):
                toc_mode = False
        normalized.append((title, paras, category))
        generated.append(title_generated)

    plan: list[NarrationSection] = []
    narrative_started = False
    for index, (title, paras, category) in enumerate(normalized):
        content_reason = ""
        uninformative = category is None or (category == "main" and _PAGE_LABEL.match(title.strip()))
        if uninformative:
            # Page-per-chapter parsers (PDF) give labels like "Página 3" that say
            # nothing, so the page's own text decides. Without this, title,
            # copyright and contents pages were narrated as if they were the book.
            detected = _classify_by_content(paras, narrative_started=narrative_started)
        elif category in {"intro", "main"}:
            # Some parsers invent a heading for everything before chapter one and
            # call it "Prólogo". A heading cannot turn a copyright page or a
            # contents list into narrative, so those two are still detected.
            detected = _classify_by_content(paras, narrative_started=narrative_started, structural_only=True)
        else:
            detected = None
        if detected:
            category, content_reason = detected
        category = category or "main"
        if category in {"main", "intro"} and _is_narrative_prose(paras):
            narrative_started = True
        if category == "toc":
            included, review = False, False
            reason = content_reason or "Índice detectado; excluido de la narración por defecto."
        elif category in {"synopsis", "back", "front"}:
            included, review = False, False
            reason = content_reason or "Material preliminar o complementario; puedes incluirlo."
        elif category == "main":
            included, review = True, not bool(title.strip()) or (index == 0 and not _CHAPTER.fullmatch(title.strip()))
            reason = "Estructura poco clara: revisa si este contenido debe narrarse." if review else "Contenido narrativo incluido."
        else:
            included, review, reason = True, False, "Material introductorio narrativo conservado."
        if not paras and category != "toc":
            continue
        plan.append(NarrationSection(f"section-{index + 1}", title or f"Sección {index + 1}", category, paras,
                                     included, review, reason,
                                     title_generated=generated[index] or not title.strip()))

    # Narration must begin at the narrative. If the policy excluded every
    # section, nothing could be produced, so keep the first section with real
    # prose and flag it instead of returning an unusable empty plan.
    if plan and not any(section.included for section in plan):
        fallback = next((section for section in plan if len(" ".join(section.paragraphs)) > 200), None)
        if fallback is not None:
            fallback.included = True
            fallback.needs_review = True
            fallback.reason = ("No se identificó ninguna sección narrativa con seguridad; "
                               "revisa dónde debe empezar la narración.")
    return plan


def _category(title: str) -> str | None:
    value = _norm(title)
    if _TOC.fullmatch(value): return "toc"
    if _SYNOPSIS.fullmatch(value): return "synopsis"
    if _BACK.fullmatch(value): return "back"
    if _INTRO.fullmatch(value): return "intro"
    if re.fullmatch(r"(portada|cubierta|titulo|title page|copyright|derechos de autor|dedicatoria|agradecimientos|agradecimiento|acknowledgements|dedication)", value): return "front"
    if _CHAPTER.fullmatch(value): return "main"
    return None


def apply_narration_selection(plan: list[NarrationSection], selected_ids: set[str] | None) -> list[SelectedChapter]:
    chosen = []
    for section in plan:
        include = section.included if selected_ids is None else section.id in selected_ids
        if include and section.paragraphs:
            title = section.title
            # PDF page labels are navigation metadata; don't make the TTS say them.
            if re.fullmatch(r"P[aá]gina\s+\d+", title, re.I): title = ""
            chosen.append(SelectedChapter(title, section.paragraphs, section.title_generated or not title))
    return chosen
