"""ONCE: the table of contents with its page numbers, whatever the book calls it.

The book's own table of contents ("Índice", "Sumario", "Contenido"…) gives the
title of the audiobook's index file and the page each chapter starts on when
the text itself has no page markers. Nothing is invented: an entry without a
page has none, a book without pages is reported as such, and the book's own
page markers win over its table of contents.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import daisy_dtd, once_structure, once_validation, planilla
from app import toc as toc_module
from app.book_structure import extract_book_structure
from app.daisy_export import build_daisy_package, make_paragraphs
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

MP3 = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 40
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
SHEET = {"index_page": "3", "file2_page": "2", "production_date": "noviembre de 2026"}


def _dtbook(toc_title: str, rows: list[str], pages: dict[str, str] | None = None) -> bytes:
    pages = pages or {}
    chapters = [("Capítulo 1. La llegada", "Clara llegó a la isla."), ("Capítulo 2. La tormenta", "Llovió tres días."),
                ("Capítulo 3. El relevo", "Llegó el nuevo farero.")]
    body = "".join(
        f"<level1>{f'<pagenum page=\"normal\">{pages[title]}</pagenum>' if title in pages else ''}"
        f"<h1>{title}</h1><p>{text}</p></level1>" for title, text in chapters)
    toc = "".join(f"<li>{row}</li>" for row in rows)
    return (f'<?xml version="1.0" encoding="UTF-8"?><dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" '
            f'version="2005-3"><head><meta name="dc:Title" content="El faro"/>'
            f'<meta name="dc:Creator" content="Ana Pérez"/></head><book><frontmatter>'
            f'<level1 class="toc"><h1>{toc_title}</h1><list type="pl">{toc}</list></level1></frontmatter>'
            f'<bodymatter>{body}</bodymatter></book></dtbook>').encode("utf-8")


ROWS = ["Capítulo 1. La llegada ........ 4", "Capítulo 2. La tormenta ........ 6", "Capítulo 3. El relevo ........ 9"]


def _build(source: bytes, name: str = "faro.xml", sheet: dict | None = None):
    structure = extract_book_structure(name, source)
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": structure.title, "author": structure.author},
                                 planilla.from_dict(sheet or SHEET), toc=toc_module.detect(plan))
    return plan, built


def _index(built) -> list[str]:
    return [paragraph.text for paragraph in built.sections[2].paragraphs]


# --------------------------------------------------------------------------- #
# Reading a row
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("row, label, page", [
    ("Capítulo 1. La llegada ........ 4", "Capítulo 1. La llegada", "4"),
    ("Prólogo ……… ix", "Prólogo", "ix"),
    ("Introducción\t12", "Introducción", "12"),
    ("La tormenta 27", "La tormenta", "27"),
    ("Capítulo 1", "Capítulo 1", ""),            # the number belongs to the label
    ("Parte 2", "Parte 2", ""),
    ("Capítulo IV", "Capítulo IV", ""),
    ("Epílogo civil", "Epílogo civil", ""),        # letters that are not a numeral
    ("Apéndice ...... xxxx", "Apéndice ...... xxxx", ""),  # not a valid roman numeral
    ("Notas", "Notas", ""),
])
def test_each_row_gives_its_page_only_when_it_really_gives_one(row, label, page) -> None:
    entry = toc_module.parse_entry(row)
    assert (entry.label, entry.page) == (label.strip(" ."), page)


# --------------------------------------------------------------------------- #
# The title, whatever the book calls it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title, heading, self_entry, end", [
    ("Sumario", "Sumario.", "Sumario página 3.", "Fin de sumario."),
    ("Contenido", "Contenido.", "Contenido página 3.", "Fin de contenido."),
    ("Tabla de contenidos", "Tabla de contenidos.", "Tabla de contenidos página 3.", "Fin de tabla de contenidos."),
    ("Índice", "Índice.", "Índice página 3.", "Fin de índice."),
])
def test_the_index_file_keeps_the_books_own_title(title, heading, self_entry, end) -> None:
    plan, built = _build(_dtbook(title, ROWS))
    toc = toc_module.detect(plan)
    assert toc is not None and toc.title == title
    assert built.sections[2].title == heading
    assert _index(built) == [self_entry, "Capítulo 1. La llegada. Página 4.", "Capítulo 2. La tormenta. Página 6.",
                             "Capítulo 3. El relevo. Página 9.", end]
    # The table of contents itself is not narrated a second time.
    assert all(section.category == "toc" and not section.included for section in plan if section.title == title)
    assert built.resolved.planilla.index_heading == heading


def test_a_planilla_can_keep_indice_or_give_its_own_wording() -> None:
    _plan, built = _build(_dtbook("Sumario", ROWS), sheet={**SHEET, "index_title_from_book": False})
    assert built.sections[2].title == "Índice."
    _plan, built = _build(_dtbook("Sumario", ROWS), sheet={**SHEET, "index_heading": "Índice general."})
    assert built.sections[2].title == "Índice general."
    assert _index(built)[-1] == "Fin de sumario."


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def test_the_chapters_start_on_the_pages_the_table_of_contents_gives(tmp_path) -> None:
    _plan, built = _build(_dtbook("Sumario", ROWS))
    assert [section.title_pages for section in built.sections[3:]] == [["4"], ["6"], ["9"]]
    assert not [warning for warning in built.warnings if warning["code"] in {"no_source_pages", "index_without_page"}]
    package = _package(built, tmp_path)
    ncx = ET.parse(next(package.glob("*.ncx"))).getroot()
    pages = [(target.get("value"), target.find(f"{NCX}navLabel/{NCX}text").text) for target in ncx.iter(f"{NCX}pageTarget")]
    # The planilla's pages of files 1-3, then the book's own: navigation between pages works.
    assert pages == [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("6", "6"), ("9", "9")]
    checks = {check["id"]: check["status"] for check in once_validation.verify(package, _records(built),
                                                                                built.resolved.as_dict())}
    assert checks["once_pages"] == "passed" and checks["once_index"] == "passed", checks


def test_the_books_own_page_markers_win_over_its_table_of_contents() -> None:
    _plan, built = _build(_dtbook("Sumario", ROWS, pages={"Capítulo 2. La tormenta": "7"}))
    assert built.sections[4].page_start == "7"
    conflict = [warning for warning in built.warnings if warning["code"] == "toc_page_conflict"]
    assert len(conflict) == 1 and "página 6" in conflict[0]["message"] and "en la 7" in conflict[0]["message"]


def test_roman_front_pages_and_pages_out_of_order() -> None:
    rows = ["Prólogo ........ ix", "Capítulo 1. La llegada ........ 4", "Capítulo 2. La tormenta ........ 2",
            "Capítulo 3. El relevo ........ 9"]
    source = _dtbook("Índice", rows).replace(
        b"<bodymatter>", "<bodymatter><level1><h1>Prólogo</h1><p>Antes de todo.</p></level1>".encode("utf-8"))
    _plan, built = _build(source)
    assert [section.page_start for section in built.sections[3:]] == ["ix", "4", "2", "9"]
    order = [warning for warning in built.warnings if warning["code"] == "page_order"]
    assert order and "Capítulo 2. La tormenta (2)" in order[0]["message"]


def test_entries_that_match_no_chapter_are_reported_not_forced() -> None:
    rows = ROWS + ["Agradecimientos ........ 12"]
    _plan, built = _build(_dtbook("Sumario", rows))
    unmatched = [warning for warning in built.warnings if warning["code"] == "toc_unmatched"]
    assert unmatched and "Agradecimientos" in unmatched[0]["message"]


def test_a_book_without_any_page_numbers_is_reported_as_such() -> None:
    rows = ["Capítulo 1. La llegada", "Capítulo 2. La tormenta", "Capítulo 3. El relevo"]
    _plan, built = _build(_dtbook("Sumario", rows))
    codes = [warning["code"] for warning in built.warnings]
    assert "no_source_pages" in codes and "index_without_page" in codes
    assert _index(built)[1:4] == ["Capítulo 1. La llegada.", "Capítulo 2. La tormenta.", "Capítulo 3. El relevo."]


def test_a_plain_text_book_with_a_contents_page() -> None:
    text = ("Contenido\n\nCapítulo 1 ..... 5\nCapítulo 2 ..... 8\n\n"
            "Capítulo 1\n\nClara llegó a la isla una tarde de otoño, con el mar en calma.\n\n"
            "Capítulo 2\n\nLlovió tres días seguidos sobre el faro y la casa.\n").encode("utf-8")
    plan, built = _build(text, "libro.txt")
    assert toc_module.detect(plan).title == "Contenido"
    assert built.sections[2].title == "Contenido."
    assert [section.page_start for section in built.sections[3:]] == ["5", "8"]


def _package(built, tmp_path: Path) -> Path:
    chapters, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        chapters.append((section.title, ([section.title] if section.heading_spoken else [])
                         + [part for text in texts for part in make_paragraphs(text)]))
        layout.append({"kind": section.kind, "level": section.level,
                       "units": once_structure.unit_layout(record, texts, section.heading_spoken, make_paragraphs)})
    package = build_daisy_package(built.resolved.title, [text for _t, body in chapters for text in body],
                                  [MP3 for _t, body in chapters for _ in body], tmp_path / "pkg",
                                  chapters=chapters, author=built.resolved.author, layout=layout)
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    return package


def _records(built) -> list[dict]:
    return [{"title": section.title, "paragraphs": [paragraph.text for paragraph in section.paragraphs],
             "title_generated": section.title_generated, "once": record}
            for section, record in zip(built.sections, built.records())]
