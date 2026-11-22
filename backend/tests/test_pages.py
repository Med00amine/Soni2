"""Every book keeps the pages of its original (ONCE: «el libro no tiene páginas»).

Pages come from the source (DTBook ``pagenum``, EPUB page breaks, Word's "Page
Number DAISY" style, a PDF's printed page numbers) and reach the package
(DTBook ``pagenum``, a silent SMIL ``par``, the NCX ``pageList`` with
``dtb:totalPageCount`` and ``dtb:maxPageNumber``) for every book, with a
planilla or without one. None is lost on the way: not a page that begins in
the middle of a paragraph, not a page with nothing to read, not the page of a
paragraph the reader removed.
"""

from __future__ import annotations

import asyncio
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import book_edits, main, once_structure, zedval_rules
from app import jobs as job_store
from app.audio_cache import AudioCache
from app.book_structure import extract_book_structure, pdf_page_labels
from app.daisy_export import make_paragraphs
from app.narration_plan import SelectedChapter

CORPUS = Path(__file__).parent / "corpus"
DEMO = CORPUS / "el-faro-de-la-isla-almendra.xml"
DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"


def _pdf(pages: list[list[tuple[float, str, float]]], labels: list[dict] | None = None) -> bytes:
    """A PDF with these lines, ``(y, text, size)``, on each page; ``[]`` is a blank page."""
    pdf = fitz.open()
    font = fitz.Font("tiro")
    for rows in pages:
        page = pdf.new_page(width=397, height=595)
        if rows:
            writer = fitz.TextWriter(page.rect)
            for y, text, size in rows:
                writer.append((51, y), text, font=font, fontsize=size)
            writer.write_text(page)
    if labels:
        pdf.set_page_labels(labels)
    data = pdf.tobytes()
    pdf.close()
    return data


def _pages_in_order(structure) -> list[tuple[str, list[str]]]:
    """(text, pages that begin at it) for every title and paragraph, in reading order."""
    found = []
    for chapter in structure.chapters:
        if chapter.title_source and not chapter.title_generated:
            found.append((chapter.title, list(chapter.title_source.get("page_breaks") or [])))
        for number, paragraph in enumerate(chapter.paragraphs):
            found.append((paragraph, list(chapter.source_of(number).get("page_breaks") or [])))
    return found


# --------------------------------------------------------------------------- #
# PDF: printed numbers, pages inside paragraphs, pages with nothing to read
# --------------------------------------------------------------------------- #

def test_a_page_that_begins_inside_a_paragraph_is_marked_at_that_paragraph() -> None:
    book = _pdf([
        [(120, "Capítulo 1", 15), (140, "El arte de hablar", 15),
         (200, "La audiodescripción es un servicio de accesibilidad.", 10),
         (300, "Este segundo párrafo continúa en la página siguien-", 10),
         (313, "te porque es largo y no termina aquí", 10), (560, "19", 10)],
        [(70, "sino en la otra página, y entonces acaba.", 10), (560, "20", 10)],
        [(120, "Capítulo 2", 15), (140, "Mirar con los sentidos", 15),
         (200, "Un capítulo nuevo empieza aquí.", 10), (560, "21", 10)],
    ])
    structure = extract_book_structure("libro.pdf", book)
    pages = _pages_in_order(structure)
    # The printed numbers, not the PDF's 1, 2, 3; and page 20, which begins
    # in the middle of the second paragraph, is marked at that paragraph.
    assert [page for _text, breaks in pages for page in breaks] == ["19", "20", "21"]
    continued = next(breaks for text, breaks in pages if "sino en la otra página" in text)
    assert continued == ["20"]
    assert any(issue["code"] == "pagination" and "impresos" in issue["message"]
               for issue in structure.extraction.issues)


def test_a_page_with_nothing_to_read_keeps_its_marker_before_the_next_text() -> None:
    book = _pdf([
        [(120, "Capítulo 1", 15), (200, "Primer párrafo del libro.", 10)],
        [],
        [(200, "Segundo párrafo, después de una página en blanco.", 10)],
    ])
    pages = _pages_in_order(extract_book_structure("libro.pdf", book))
    assert pages[-1] == ("Segundo párrafo, después de una página en blanco.", ["2", "3"])
    assert [page for _text, breaks in pages for page in breaks] == ["1", "2", "3"]


def test_a_pdfs_own_page_labels_come_first() -> None:
    book = _pdf([
        [(120, "Prólogo", 15), (200, "Unas palabras antes del libro.", 10)],
        [(200, "Más palabras antes del libro.", 10)],
        [(120, "Capítulo 1", 15), (200, "Empieza la historia.", 10)],
    ], labels=[{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
               {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1}])
    pages = _pages_in_order(extract_book_structure("libro.pdf", book))
    assert [page for _text, breaks in pages for page in breaks] == ["i", "ii", "1"]


class _NoLabels:
    def get_page_labels(self) -> list:
        return []


def _profiles(texts: list[str]) -> list[dict]:
    return [{"page": number, "_text": text} for number, text in enumerate(texts, start=1)]


def test_printed_numbers_are_read_past_a_chapter_number_at_the_top_of_a_page() -> None:
    texts = ["Cubierta", "", "Texto\n1", "Texto\n2", "Texto\n3", "12\nCapítulo doce\nTexto\n4", "Texto\n5", "Texto\n6"]
    labels, method = pdf_page_labels(_NoLabels(), _profiles(texts))
    assert method == "printed"
    # The cover and the page before page 1 have no number; "12" is a chapter number.
    assert labels == {3: "1", 4: "2", 5: "3", 6: "4", 7: "5", 8: "6"}


def test_an_unnumbered_insert_does_not_take_the_numbers_of_the_pages_after_it() -> None:
    texts = [f"Texto\n{n}" for n in range(1, 5)] + ["", ""] + [f"Texto\n{n}" for n in range(5, 9)]
    labels, _method = pdf_page_labels(_NoLabels(), _profiles(texts))
    assert labels == {1: "1", 2: "2", 3: "3", 4: "4", 7: "5", 8: "6", 9: "7", 10: "8"}
    assert len(set(labels.values())) == len(labels)


def test_a_pdf_that_prints_no_numbers_falls_back_to_its_own() -> None:
    labels, method = pdf_page_labels(_NoLabels(), _profiles(["Uno", "Dos", "Tres"]))
    assert method == "pdf_index" and labels == {1: "1", 2: "2", 3: "3"}


# --------------------------------------------------------------------------- #
# Edits and headings that are not read
# --------------------------------------------------------------------------- #

def _chapters() -> list[SelectedChapter]:
    return [SelectedChapter("Capítulo 1", ["Uno.", "Dos.", "Tres."]),
            SelectedChapter("Capítulo 2", ["Cuatro."]),
            SelectedChapter("Capítulo 3", ["Cinco."])]


PROVENANCE = [({"page_breaks": ["7"]}, [{}, {"page_breaks": ["8"]}, {}]),
              ({"page_breaks": ["9"]}, [{"page_breaks": ["10"]}]),
              ({"page_breaks": ["11"]}, [{}])]


def test_a_removed_paragraph_hands_its_page_to_the_next_one() -> None:
    edits = book_edits.BookEdits()
    edits.slots["chapter0:1"] = ""
    chapters, provenance = book_edits.apply(_chapters(), edits, PROVENANCE)
    assert chapters[0][1] == ["Uno.", "Tres."]
    assert [origin.get("page_breaks") for origin in provenance[0][1]] == [None, ["8"]]


def test_a_removed_section_hands_its_pages_to_the_next_heading_read() -> None:
    edits = book_edits.BookEdits()
    edits.slots["chapter1:0"] = ""
    edits.deleted_titles = {"chapter1"}
    chapters, provenance = book_edits.apply(_chapters(), edits, PROVENANCE)
    assert [chapter[0] for chapter in chapters] == ["Capítulo 1", "Capítulo 3"]
    # Pages 9 and 10 came before chapter 3's heading, so they begin at it, in order.
    assert provenance[1][0]["page_breaks"] == ["9", "10", "11"]


def test_the_pages_of_a_section_left_out_begin_at_the_next_text_read() -> None:
    """A title page the narration leaves out still has its page in the printed copy."""
    source = ("""<?xml version="1.0" encoding="UTF-8"?>
<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" version="2005-3"><head>
<meta name="dc:Title" content="El faro"/></head><book><bodymatter>
<level1><pagenum page="normal" id="p1">1</pagenum><h1>Portada</h1><p>El faro. Ana Pérez.</p></level1>
<level1><pagenum page="normal" id="p2">2</pagenum><h1>Capítulo 1</h1>
<p>Empieza la historia, y sigue un buen rato junto al mar.</p>
<pagenum page="normal" id="p3">3</pagenum><p>Y termina en la página tres.</p></level1>
</bodymatter></book></dtbook>""").encode("utf-8")
    structure = extract_book_structure("libro.xml", source)
    chapters, _once = main.prepare_book(structure, {"title": "El faro", "author": ""}, narration_plan_json="",
                                        create_sections=False, sheet=None)
    assert [chapter[0] for chapter in chapters] == ["Capítulo 1"]  # the cover is left out by default
    assert chapters[0].title_pages == ["1", "2"] and chapters[0].page_breaks == [[], ["3"]]


def test_the_pages_of_a_heading_that_is_not_read_begin_at_its_first_text() -> None:
    record = {"title_pages": ["5"], "paragraphs": [{"page_breaks": ["6"]}, {}]}
    entries = once_structure.unit_layout(record, ["Uno.", "Dos."], False, make_paragraphs)
    assert [entry["page_breaks"] for entry in entries] == [["5", "6"], []]


def test_a_page_that_begins_at_a_long_paragraph_begins_at_its_first_part() -> None:
    long_paragraph = " ".join(["Una frase larga del libro que sigue y sigue."] * 30)
    chapter = SelectedChapter("Capítulo 1", [long_paragraph])
    chapter.title_pages, chapter.page_breaks = [], [["12"]]
    layout = main.plain_chapter_layout(chapter, [make_paragraphs(long_paragraph)])
    assert len(layout["units"]) > 1
    assert [unit["page_breaks"] for unit in layout["units"]] == [["12"]] + [[]] * (len(layout["units"]) - 1)


def test_a_page_is_marked_once_even_when_two_texts_claim_it(tmp_path) -> None:
    """A planilla's initial files take the first pages; the source's own front matter, if read too, repeats them."""
    from app.daisy_export import build_daisy_package

    frames = bytes([0xFF, 0xFB, 0x90, 0x64]) + b"\x00" * 413
    chapters = [("Título", ["Título", "Aviso legal."]), ("Portada", ["Portada", "Texto de la portada."]),
                ("Capítulo 1", ["Capítulo 1", "Empieza."])]
    layout = [{"kind": "chapter", "level": 1, "units": [{"role": "heading", "page_breaks": ["1"]},
                                                        {"role": "text", "page_breaks": []}]},
              {"kind": "chapter", "level": 1, "units": [{"role": "heading", "page_breaks": ["1", "2"]},
                                                        {"role": "text", "page_breaks": []}]},
              {"kind": "chapter", "level": 1, "units": [{"role": "heading", "page_breaks": ["3"]},
                                                        {"role": "text", "page_breaks": []}]}]
    package = build_daisy_package("Libro", [text for _t, body in chapters for text in body],
                                  [frames * 30] * 6, tmp_path / "pkg", chapters=chapters, layout=layout,
                                  package_layout_name="flat")
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    assert [page.text for page in dtbook.iter(f"{DTBOOK}pagenum")] == ["1", "2", "3"]
    ncx = ET.parse(next(package.glob("*.ncx"))).getroot()
    assert [target.get("value") for target in ncx.iter(f"{NCX}pageTarget")] == ["1", "2", "3"]
    assert not zedval_rules.summary(zedval_rules.verify(package))["requirements_failed"]


# --------------------------------------------------------------------------- #
# End to end: a book produced without a planilla keeps its pages
# --------------------------------------------------------------------------- #

@pytest.fixture()
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "quality_review_enabled": True}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    return TestClient(main.app)


def _package_pages(names: dict[str, bytes]) -> tuple[list[str], list[str], dict[str, str]]:
    dtbook = next(data for name, data in names.items() if name.startswith("DAISY 3/") and name.endswith(".xml"))
    ncx = next(data for name, data in names.items() if name.endswith(".ncx"))
    pagenums = [element.text for element in ET.fromstring(dtbook).iter(f"{DTBOOK}pagenum")]
    ncx_root = ET.fromstring(ncx)
    targets = [target.findtext(f"{NCX}navLabel/{NCX}text") for target in ncx_root.iter(f"{NCX}pageTarget")]
    meta = {element.get("name"): element.get("content") for element in ncx_root.iter(f"{NCX}meta")}
    return pagenums, targets, meta


def _unzip(path: Path, root: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        archive.extractall(root)
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def test_a_production_without_a_planilla_keeps_the_pages_of_its_source(offline, tmp_path) -> None:
    response = offline.post("/api/books/jobs", files={"file": (DEMO.name, io.BytesIO(DEMO.read_bytes()),
                                                               "application/xml")})
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job.get("error")
    files = _unzip(Path(job["output_path"]), tmp_path / "unzipped")
    pagenums, targets, meta = _package_pages(files)
    assert pagenums == targets == ["4", "5", "6", "7"]
    assert meta["dtb:totalPageCount"] == "4" and meta["dtb:maxPageNumber"] == "7"
    checks = zedval_rules.verify(tmp_path / "unzipped" / "DAISY 3")
    assert not zedval_rules.summary(checks)["requirements_failed"], checks


def test_a_direct_download_without_a_planilla_keeps_the_pages_of_its_source(offline, tmp_path) -> None:
    response = offline.post("/api/books/package",
                            files={"file": (DEMO.name, io.BytesIO(DEMO.read_bytes()), "application/xml")})
    assert response.status_code == 200, response.text
    archive = tmp_path / "direct.zip"
    archive.write_bytes(response.content)
    files = _unzip(archive, tmp_path / "unzipped")
    pagenums, targets, meta = _package_pages(files)
    assert pagenums == targets == ["4", "5", "6", "7"]
    assert meta["dtb:totalPageCount"] == "4"
    checks = zedval_rules.verify(tmp_path / "unzipped" / "DAISY 3")
    assert not zedval_rules.summary(checks)["requirements_failed"], checks


# --------------------------------------------------------------------------- #
# Each page is announced where it begins: «Página 5.»
# --------------------------------------------------------------------------- #

def test_each_page_that_begins_at_a_unit_is_said_before_its_text() -> None:
    assert main.announce_pages("Empieza.", ["5"]) == "Página 5. Empieza."
    assert main.announce_pages("Empieza.", ["5", "6"]) == "Página 5. Página 6. Empieza."
    assert main.announce_pages("Sigue.", []) == main.announce_pages("Sigue.", None) == "Sigue."


def test_a_production_announces_each_page_at_the_unit_where_it_begins(offline) -> None:
    response = offline.post("/api/books/jobs", files={"file": (DEMO.name, io.BytesIO(DEMO.read_bytes()),
                                                               "application/xml")})
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job.get("error")
    announced = []
    for unit in job["sentences"]:
        pages = (unit.get("provenance") or {}).get("page_breaks") or []
        said = "".join(f"Página {page}. " for page in pages)
        # The voice says it, speech review expects it, and the book's text is unchanged.
        assert unit["spoken_text"].startswith(said) and unit["synthesis_text"].startswith(said)
        assert (unit["spoken_text"].startswith("Página ")) == bool(pages)
        assert not unit["text"].startswith("Página ")
        announced += pages
    assert announced == ["4", "5", "6", "7"]


def test_a_direct_download_says_each_page_right_after_its_marker(offline, tmp_path, monkeypatch) -> None:
    # Without ffmpeg a chapter is the plain join of the mock clips, which carry the text they say.
    monkeypatch.setattr(main.filesystem, "which", lambda _name: None)
    response = offline.post("/api/books/package",
                            files={"file": (DEMO.name, io.BytesIO(DEMO.read_bytes()), "application/xml")})
    assert response.status_code == 200, response.text
    archive = tmp_path / "direct.zip"
    archive.write_bytes(response.content)
    files = _unzip(archive, tmp_path / "unzipped")
    dtbook = ET.fromstring(next(data for name, data in files.items()
                                if name.startswith("DAISY 3/") and name.endswith(".xml")))
    audio = b"".join(data for name, data in sorted(files.items())
                     if name.startswith("DAISY 3/") and name.endswith(".mp3"))
    elements = list(dtbook.iter())
    pages = [(index, element.text) for index, element in enumerate(elements) if element.tag == f"{DTBOOK}pagenum"]
    assert [page for _index, page in pages] == ["4", "5", "6", "7"]
    for index, page in pages:
        # The text the page marker stands before is the one whose audio opens with «Página N.».
        following = next("".join(element.itertext()).strip() for element in elements[index + 1:]
                         if element.tag != f"{DTBOOK}pagenum" and "".join(element.itertext()).strip())
        marker = f"Página {page}. ".encode("utf-8")
        assert audio.count(marker) == 1
        spoken = audio[audio.index(marker):][:600].decode("utf-8", "ignore")
        while spoken.startswith("Página "):
            spoken = spoken.split(". ", 1)[1]
        assert spoken.startswith(following.split()[0]), (page, following[:40], spoken[:40])
