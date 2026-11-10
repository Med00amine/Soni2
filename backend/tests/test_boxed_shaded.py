"""ONCE: text in a box or on a shaded background is announced where it begins and ends.

"Texto en recuadro." … "Fin de texto en recuadro."; "Texto sombreado." …
"Fin de texto sombreado.". The box comes from the source's own formatting
(DTBook sidebar, HTML aside, class or inline style, Word borders, shading and
text boxes, rectangles drawn in a PDF); several paragraphs in one box are one
block, announced once; the book's text is never changed; a "box" that is the
whole chapter is page layout and is reported, not announced.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import fitz
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fastapi.testclient import TestClient

from app import daisy_dtd, main, once_structure, once_validation, planilla
from app import jobs as job_store
from app.audio_cache import AudioCache
from app.book_structure import extract_book_structure
from app.daisy_export import build_daisy_package, make_paragraphs
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

MP3 = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 40
DTBOOK_NS = "{http://www.daisy.org/z3986/2005/dtbook/}"

DTBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" version="2005-3">
<head><meta name="dc:Title" content="El faro"/><meta name="dc:Creator" content="Ana Pérez"/></head>
<book><bodymatter>
<level1><h1>Capítulo 1. La isla</h1>
<p>La barca llegó al muelle al anochecer.</p>
<sidebar render="optional"><hd>Los faros de la costa</hd><p>El primer faro se encendió en 1850.</p>
<p>Funcionaba con aceite de ballena.</p></sidebar>
<p>Clara subió por el camino de piedras.</p>
<div class="texto-sombreado"><p>Recuerda: la luz nunca se apaga.</p></div>
<p>Al final del camino estaba la puerta.</p>
<sidebar render="optional"><p>Una sola línea en recuadro.</p></sidebar>
</level1>
</bodymatter></book></dtbook>
"""


def _paragraphs(structure) -> list[tuple[str, dict]]:
    return [(text, chapter.source_of(index)) for chapter in structure.chapters
            for index, text in enumerate(chapter.paragraphs)]


def _once(source: bytes, name: str, sheet: dict | None = None):
    structure = extract_book_structure(name, source)
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": structure.title, "author": structure.author},
                                 planilla.from_dict(sheet or {"production_date": "noviembre de 2026"}))
    return structure, built


def _read_order(built) -> list[tuple[str, str]]:
    return [(paragraph.role, paragraph.text) for section in built.sections if not section.initial
            for paragraph in section.paragraphs]


# --------------------------------------------------------------------------- #
# Detection, one format at a time
# --------------------------------------------------------------------------- #

def test_dtbook_sidebars_and_shaded_classes_are_frames() -> None:
    structure = extract_book_structure("faro.xml", DTBOOK.encode("utf-8"))
    frames = [(text, (source.get("frame") or {}).get("kind")) for text, source in _paragraphs(structure)]
    assert frames == [
        ("La barca llegó al muelle al anochecer.", None),
        ("Los faros de la costa", "boxed"), ("El primer faro se encendió en 1850.", "boxed"),
        ("Funcionaba con aceite de ballena.", "boxed"),
        ("Clara subió por el camino de piedras.", None),
        ("Recuerda: la luz nunca se apaga.", "shaded"),
        ("Al final del camino estaba la puerta.", None),
        ("Una sola línea en recuadro.", "boxed"),
    ]
    sources = dict(_paragraphs(structure))
    # The box's own heading is read with it, not taken for a new chapter.
    assert sources["Los faros de la costa"]["element"] == "p" and sources["Los faros de la costa"]["frame_heading"]
    # Several paragraphs of one box share its identity; two boxes do not.
    ids = [(source.get("frame") or {}).get("id") for _text, source in _paragraphs(structure)]
    assert ids[1] == ids[2] == ids[3] and ids[1] != ids[7] and ids[5] not in {ids[1], ids[7]}
    codes = [issue["code"] for issue in structure.extraction.issues]
    assert codes.count("frame_boxed") == 2 and codes.count("frame_shaded") == 1


def test_html_aside_classes_and_inline_styles_are_frames(tmp_path) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("frames")
    book.set_title("Recuadros")
    book.set_language("es")
    chapter = epub.EpubHtml(title="Uno", file_name="uno.xhtml", lang="es")
    chapter.content = (
        "<html><body><h1>Capítulo 1</h1><p>Texto normal del capítulo.</p>"
        "<aside><p>Dentro de un aside.</p></aside>"
        "<p>Más texto normal.</p>"
        "<div style=\"background-color: #e0e0e0\"><p>Con fondo gris.</p><p>Segundo párrafo gris.</p></div>"
        "<div style=\"border: 1px solid black\"><p>Con borde.</p></div>"
        "<div style=\"background: white\"><p>Fondo blanco: no es sombreado.</p></div>"
        "<p class=\"recuadro\">Párrafo con clase recuadro.</p>"
        "<p class=\"boxer-intro\">Una clase que solo contiene la palabra.</p>"
        "</body></html>")
    book.add_item(chapter)
    book.toc = (epub.Link("uno.xhtml", "Uno", "uno"),)
    book.spine = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    target = tmp_path / "frames.epub"
    epub.write_epub(str(target), book)
    structure = extract_book_structure("frames.epub", target.read_bytes())
    kinds = {text: (source.get("frame") or {}).get("kind") for text, source in _paragraphs(structure)}
    assert kinds == {
        "Texto normal del capítulo.": None, "Dentro de un aside.": "boxed", "Más texto normal.": None,
        "Con fondo gris.": "shaded", "Segundo párrafo gris.": "shaded", "Con borde.": "boxed",
        "Fondo blanco: no es sombreado.": None, "Párrafo con clase recuadro.": "boxed",
        "Una clase que solo contiene la palabra.": None,
    }


def _bordered(paragraph, sides=("top", "left", "bottom", "right")) -> None:
    properties = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    for side in sides:
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        borders.append(element)
    properties.append(borders)


def _shaded(paragraph, fill="D9D9D9") -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:fill"), fill)
    paragraph._p.get_or_add_pPr().append(shading)


def test_word_borders_shading_and_text_boxes_are_frames() -> None:
    document = Document()
    document.add_heading("Capítulo 1", level=1)
    document.add_paragraph("Texto normal.")
    for text in ("Primer párrafo del recuadro.", "Segundo párrafo del recuadro."):
        _bordered(document.add_paragraph(text))
    _bordered(document.add_paragraph("Solo una raya debajo: no es recuadro."), sides=("bottom",))
    _shaded(document.add_paragraph("Párrafo sombreado."))
    _shaded(document.add_paragraph("Fondo blanco explícito."), fill="FFFFFF")
    anchor = document.add_paragraph("Párrafo con un cuadro de texto.")
    # A text box: drawn once, plus the VML copy Word keeps for old readers.
    run = anchor.add_run()
    from lxml import etree

    fallback = etree.SubElement(run._r, "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback")
    for parent in (run._r, fallback):
        content = OxmlElement("w:txbxContent")
        inner = OxmlElement("w:p")
        inner_run = OxmlElement("w:r")
        inner_text = OxmlElement("w:t")
        inner_text.text = "Texto dentro del cuadro."
        inner_run.append(inner_text)
        inner.append(inner_run)
        content.append(inner)
        holder = OxmlElement("w:pict")
        holder.append(content)
        parent.insert(0, holder) if parent is run._r else parent.append(holder)
    buffer = io.BytesIO()
    document.save(buffer)
    structure = extract_book_structure("libro.docx", buffer.getvalue())
    pairs = [(text, (source.get("frame") or {}).get("kind")) for text, source in _paragraphs(structure)]
    assert pairs == [
        ("Texto normal.", None),
        ("Primer párrafo del recuadro.", "boxed"), ("Segundo párrafo del recuadro.", "boxed"),
        ("Solo una raya debajo: no es recuadro.", None),
        ("Párrafo sombreado.", "shaded"),
        ("Fondo blanco explícito.", None),
        ("Párrafo con un cuadro de texto.", None),
        ("Texto dentro del cuadro.", "boxed"),
    ]
    ids = [(source.get("frame") or {}).get("id") for _text, source in _paragraphs(structure)]
    assert ids[1] == ids[2] and ids[7] not in {ids[1], ids[4]}


def test_pdf_rectangles_drawn_around_or_behind_text_are_frames() -> None:
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((72, 90), "Capítulo 1. La isla", fontsize=20)
    body = [
        (150, "La barca llegó al muelle cuando caía la tarde sobre la isla."),
        (250, "Este texto está dentro de un recuadro con borde negro."),
        (350, "Este texto está sobre un fondo gris claro sombreado."),
        (450, "Y este vuelve a ser un párrafo normal del capítulo."),
    ]
    for top, line in body:
        page.insert_text((72, top), line, fontsize=11)
    page.draw_rect(fitz.Rect(60, 230, 540, 262), color=(0, 0, 0), width=1)
    page.draw_rect(fitz.Rect(60, 330, 540, 362), color=None, fill=(0.85, 0.85, 0.85), overlay=False)
    page.draw_rect(fitz.Rect(0, 0, 595, 842), color=None, fill=(1, 1, 1), overlay=False)  # page background
    page.draw_line((72, 470), (520, 470))  # a rule, not a box
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    kinds = [(text[:30], (source.get("frame") or {}).get("kind")) for text, source in _paragraphs(structure)]
    assert kinds == [("La barca llegó al muelle cuand", None), ("Este texto está dentro de un r", "boxed"),
                     ("Este texto está sobre un fondo", "shaded"), ("Y este vuelve a ser un párrafo", None)]


def test_a_box_around_the_whole_chapter_is_reported_not_announced() -> None:
    source = DTBOOK.replace('<level1><h1>Capítulo 1. La isla</h1>',
                           '<level1><h1>Capítulo 1. La isla</h1><div class="box">').replace(
        "</level1>", "</div></level1>")
    structure = extract_book_structure("faro.xml", source.encode("utf-8"))
    # The inner boxes stay; the wrapper around everything is page layout.
    kinds = [(source.get("frame") or {}).get("kind") for _text, source in _paragraphs(structure)]
    assert kinds.count(None) == 3
    assert any(issue["code"] == "frame_rejected" for issue in structure.extraction.issues)


# --------------------------------------------------------------------------- #
# What is read, and the package
# --------------------------------------------------------------------------- #

def test_each_block_is_announced_once_where_it_begins_and_where_it_ends() -> None:
    _structure, built = _once(DTBOOK.encode("utf-8"), "faro.xml")
    order = _read_order(built)
    assert order[:13] == [
        ("text", "La barca llegó al muelle al anochecer."),
        ("frame_open", "Texto en recuadro."),
        ("text", "Los faros de la costa"), ("text", "El primer faro se encendió en 1850."),
        ("text", "Funcionaba con aceite de ballena."),
        ("frame_close", "Fin de texto en recuadro."),
        ("text", "Clara subió por el camino de piedras."),
        ("frame_open", "Texto sombreado."), ("text", "Recuerda: la luz nunca se apaga."),
        ("frame_close", "Fin de texto sombreado."),
        ("text", "Al final del camino estaba la puerta."),
        ("frame_open", "Texto en recuadro."), ("text", "Una sola línea en recuadro."),
    ]
    assert order[13] == ("frame_close", "Fin de texto en recuadro.")
    # The book's own words are unchanged: the announcements are separate, generated texts.
    assert all(paragraph.source is None for section in built.sections for paragraph in section.paragraphs
               if paragraph.role in once_structure.FRAME_ROLES)


def test_the_wording_is_a_planilla_setting() -> None:
    _structure, built = _once(DTBOOK.encode("utf-8"), "faro.xml",
                              {"boxed_open": "Comienza el recuadro.", "boxed_close": "Termina el recuadro."})
    texts = [text for role, text in _read_order(built) if role in once_structure.FRAME_ROLES]
    assert texts[:2] == ["Comienza el recuadro.", "Termina el recuadro."]


def test_without_a_planilla_nothing_is_announced() -> None:
    from app.main import prepare_book

    structure = extract_book_structure("faro.xml", DTBOOK.encode("utf-8"))
    chapters, once = prepare_book(structure, {"title": "El faro", "author": ""}, narration_plan_json="",
                                  create_sections=False, sheet=None)
    assert once is None
    assert not any("recuadro" in text.casefold() and text.startswith("Texto") for _title, body in chapters
                   for text in body)


def _package(built, tmp_path: Path) -> tuple[Path, list[dict]]:
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
    records = [{"title": section.title, "paragraphs": [paragraph.text for paragraph in section.paragraphs],
                "title_generated": section.title_generated, "once": record}
               for section, record in zip(built.sections, built.records())]
    return package, records


def test_the_package_has_one_sidebar_per_block_with_its_announcements(tmp_path) -> None:
    import xml.etree.ElementTree as ET

    _structure, built = _once(DTBOOK.encode("utf-8"), "faro.xml")
    package, records = _package(built, tmp_path)
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    sidebars = list(dtbook.iter(f"{DTBOOK_NS}sidebar"))
    assert [sidebar.get("class") for sidebar in sidebars] == ["boxed", "shaded", "boxed"]
    first = [(child.tag.rsplit("}", 1)[-1], "".join(child.itertext())) for child in sidebars[0]]
    assert first == [("prodnote", "Texto en recuadro."), ("p", "Los faros de la costa"),
                     ("p", "El primer faro se encendió en 1850."), ("p", "Funcionaba con aceite de ballena."),
                     ("prodnote", "Fin de texto en recuadro.")]
    checks = {check["id"]: check for check in once_validation.verify(package, records, built.resolved.as_dict())}
    assert checks["once_frames"]["status"] == "passed", checks["once_frames"]
    assert "2 recuadros y 1 sombreados" in checks["once_frames"]["detail"]
    # The book has no page numbers: its index says so instead of inventing page 1.
    assert checks["once_index"]["status"] == "warning"
    assert "sin número de página" in checks["once_index"]["detail"]
    assert {check["status"] for key, check in checks.items() if key != "once_index"} == {"passed"}


def test_a_box_missing_its_end_is_caught(tmp_path) -> None:
    _structure, built = _once(DTBOOK.encode("utf-8"), "faro.xml")
    package, records = _package(built, tmp_path)
    dtbook = next(package.glob("*.xml"))
    text = dtbook.read_text(encoding="utf-8")
    dtbook.write_text(text.replace("Fin de texto sombreado.", "Fin del sombreado."), encoding="utf-8")
    checks = {check["id"]: check for check in once_validation.verify(package, records, built.resolved.as_dict())}
    assert checks["once_frames"]["status"] == "failed"
    assert "no termina con «Fin de texto sombreado.»" in checks["once_frames"]["detail"]


def test_a_production_job_reads_the_announcements_and_they_cannot_be_rewritten(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "asr_secondary_provider": "none", "quality_review_enabled": True}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    client = TestClient(main.app)
    response = client.post("/api/books/jobs", data={"planilla_json": json.dumps({"production_date": "noviembre"})},
                           files={"file": ("faro.xml", io.BytesIO(DTBOOK.encode("utf-8")), "application/xml")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    chain = json.loads((Path(job["source_path"]).parent / "completeness.json").read_text(encoding="utf-8"))
    statuses = {check["id"]: check["status"] for check in chain["checks"]}
    assert statuses["once_frames"] == "passed" and statuses["units_to_dtbook"] == "passed"
    announcements = [unit for unit in job["sentences"]
                     if (unit.get("provenance") or {}).get("role") in once_structure.FRAME_ROLES]
    assert [unit["text"] for unit in announcements] == [
        "Texto en recuadro.", "Fin de texto en recuadro.", "Texto sombreado.", "Fin de texto sombreado.",
        "Texto en recuadro.", "Fin de texto en recuadro."]
    structure = client.get(f"/api/jobs/{job_id}/structure").json()
    editable = {unit["text"]: unit["editable"] for section in structure["sections"] for unit in section["units"]}
    assert editable["Texto en recuadro."] is False and editable["Clara subió por el camino de piedras."] is True
    # Correcting an announcement from the review queue is refused, like the planilla's texts.
    refused = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": announcements[0]["sentence_id"], "decision": "correct", "corrected_text": "Otra cosa."})
    assert refused.status_code == 422
