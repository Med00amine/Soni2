"""ONCE: an unpaginated photo insert is read at the end, its photographs numbered.

Only on evidence: a PDF run of photograph pages without page numbers between
two consecutive printed pages, or an EPUB document of images the book calls an
insert. Anything less is a candidate that a person confirms in the planilla;
an ordinary illustration is never moved. The photographs' images travel into
the package as they are; nothing describes a photograph, only the book's own
caption is read.
"""

from __future__ import annotations

import asyncio
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import daisy_dtd, daisy_timing, main, once_structure, once_validation, photo_inserts, planilla
from app import jobs as job_store
from app.audio_cache import AudioCache
from app.book_structure import extract_book_structure
from app.daisy_export import build_daisy_package, make_paragraphs
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

MP3 = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 40
DTBOOK_NS = "{http://www.daisy.org/z3986/2005/dtbook/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
SHEET = {"file2_page": "2", "index_page": "3", "production_date": "noviembre de 2026"}
TEXT = ("La barca dejó a Clara en el muelle de la isla cuando el sol ya rozaba el agua. Traía una maleta, "
        "un cuaderno de tapas azules y la carta del farero anterior, que la esperaba desde hacía semanas.")


def _photo(shade: int) -> bytes:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 300), 0)
    pixmap.clear_with(shade)
    return pixmap.tobytes("png")


def _pdf(*, numbered_insert: bool = False, gap_in_numbers: bool = False, illustration: bool = False) -> bytes:
    """Text pages printed 1, 2, 3; two photograph pages; then page 4 (or 7)."""
    pdf = fitz.open()
    printed = ["1", "2", "3", None, None, "7" if gap_in_numbers else "4", "5" if not gap_in_numbers else "8"]
    for index, label in enumerate(printed):
        page = pdf.new_page(width=595, height=842)
        if label is None:
            # A page of photographs: two pictures, one caption under the first page's photo.
            if index == 3:
                page.insert_image(fitz.Rect(60, 80, 535, 440), stream=_photo(120))
                page.insert_text((60, 470), "El faro en 1910.", fontsize=10)
            else:
                page.insert_image(fitz.Rect(60, 60, 535, 380), stream=_photo(80))
                page.insert_image(fitz.Rect(60, 420, 535, 760), stream=_photo(200))
            if numbered_insert:
                page.insert_text((290, 820), str(10 + index), fontsize=10)
            continue
        page.insert_text((60, 100), f"Página de texto {label}. " + TEXT[:60], fontsize=11)
        page.insert_text((60, 120), TEXT[60:130], fontsize=11)
        page.insert_text((60, 140), TEXT[130:], fontsize=11)
        if illustration and label == "2":
            page.insert_image(fitz.Rect(400, 600, 520, 690), stream=_photo(150))
        page.insert_text((290, 820), label, fontsize=10)
    return pdf.tobytes()


def _build(source: bytes, name: str, sheet: dict | None = None):
    structure = extract_book_structure(name, source)
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": "El faro", "author": "Ana Pérez"},
                                 planilla.from_dict(sheet or SHEET), photo_inserts=structure.photo_inserts)
    return structure, built


def _package(built, tmp_path: Path, filename: str, content: bytes, layout_name: str = "nested"):
    chapters, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        chapters.append((section.title, ([section.title] if section.heading_spoken else [])
                         + [part for text in texts for part in make_paragraphs(text)]))
        layout.append({"kind": section.kind, "level": section.level,
                       "units": once_structure.unit_layout(record, texts, section.heading_spoken, make_paragraphs)})
    photos = [paragraph.photo for section in built.sections for paragraph in section.paragraphs
              if paragraph.role == "photo"]
    images = main.photo_images(filename, content, photos)
    package = build_daisy_package("El faro", [text for _t, body in chapters for text in body],
                                  [MP3 for _t, body in chapters for _ in body], tmp_path / layout_name,
                                  chapters=chapters, author="Ana Pérez", layout=layout, images=images,
                                  package_layout_name=layout_name)
    records = [{"title": section.title, "paragraphs": [paragraph.text for paragraph in section.paragraphs],
                "title_generated": section.title_generated, "once": record}
               for section, record in zip(built.sections, built.records())]
    return package, records


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_an_unpaginated_run_of_photographs_between_consecutive_pages_is_an_insert() -> None:
    structure = extract_book_structure("libro.pdf", _pdf())
    assert len(structure.photo_inserts) == 1
    insert = structure.photo_inserts[0]
    assert insert.strong and insert.location["pdf_pages"] == [4, 5]
    assert (insert.location["after_page"], insert.location["before_page"]) == ("3", "4")
    assert [photo.caption for photo in insert.photos] == ["El faro en 1910.", "", ""]
    # Its pages are photographs, not text to recover: nothing blocks the book.
    issues = structure.extraction.issues
    assert not [issue for issue in issues if issue["severity"] == "blocking"]
    assert any(issue["code"] == "photo_insert" for issue in issues)
    # The caption is not read inside the chapter it interrupted.
    assert not any("1910" in paragraph for chapter in structure.chapters for paragraph in chapter.paragraphs)


@pytest.mark.parametrize("variant", [{"numbered_insert": True}, {"gap_in_numbers": True}])
def test_without_that_evidence_it_is_only_a_candidate(variant) -> None:
    structure = extract_book_structure("libro.pdf", _pdf(**variant))
    assert [insert.evidence for insert in structure.photo_inserts] == ["candidate"]
    assert any(issue["code"] == "photo_insert_candidate" for issue in structure.extraction.issues)


def test_an_illustration_inside_a_text_page_is_never_an_insert() -> None:
    structure = extract_book_structure("libro.pdf", _pdf(illustration=True))
    assert all(insert.location["pdf_pages"] == [4, 5] for insert in structure.photo_inserts)


# --------------------------------------------------------------------------- #
# What is read, and the package
# --------------------------------------------------------------------------- #

def test_the_photographs_are_read_at_the_end_numbered_with_their_captions_only() -> None:
    _structure, built = _build(_pdf(), "libro.pdf")
    last = built.sections[-1]
    assert last.kind == once_structure.KIND_PHOTOS and last.title == "Fotografías."
    assert [(paragraph.role, paragraph.text) for paragraph in last.paragraphs] == [
        ("photo", "Fotografía 1."), ("photo_caption", "El faro en 1910."), ("photo", "Fotografía 2."),
        ("photo", "Fotografía 3."), ("closing", "Aquí finaliza la grabación de El faro, De Ana Pérez.")]
    # Where the insert was, the listener is told where the photographs went.
    references = [(section.title, paragraph.text) for section in built.sections for paragraph in section.paragraphs
                  if paragraph.role == "photo_reference"]
    assert references == [(references[0][0],
                           "Encarte de fotografías: las fotografías 1 a 3 se leen al final del libro.")]
    # The insert has no page: its index entry says none, and that is not a missing page.
    index = [paragraph.text for paragraph in built.sections[2].paragraphs]
    assert "Fotografías." in index
    assert not [warning for warning in built.warnings if warning["code"] == "index_without_page"]


@pytest.mark.parametrize("layout_name", ["nested", "flat"])
def test_the_package_carries_each_photograph_numbered_with_navigation(tmp_path, layout_name) -> None:
    content = _pdf()
    _structure, built = _build(content, "libro.pdf")
    package, records = _package(built, tmp_path, "libro.pdf", content, layout_name)
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    assert daisy_timing.summary(daisy_timing.verify(package))["status"] == "passed"
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    groups = list(dtbook.iter(f"{DTBOOK_NS}imggroup"))
    assert [group.get("id") for group in groups] == ["photo-1", "photo-2", "photo-3"]
    first = {child.tag.rsplit("}", 1)[-1]: child for child in groups[0]}
    # The alternative text is the photograph's number, never a description.
    assert first["img"].get("alt") == "Fotografía 1"
    assert first["prodnote"].text == "Fotografía 1." and first["caption"].text == "El faro en 1910."
    image = package / first["img"].get("src")
    assert image.is_file() and image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    ncx = ET.parse(next(package.glob("*.ncx"))).getroot()
    photos = [target.find(f"{NCX}navLabel/{NCX}text").text for target in ncx.iter(f"{NCX}navTarget")
              if target.get("class") == "photo"]
    assert photos == ["Fotografía 1", "Fotografía 2", "Fotografía 3"]
    checks = {check["id"]: check for check in once_validation.verify(package, records, built.resolved.as_dict())}
    assert checks["once_photos"]["status"] == "passed", checks["once_photos"]
    assert checks["once_closing"]["status"] == "passed" and checks["once_index"]["status"] in {"passed", "warning"}


def test_a_candidate_is_moved_only_when_the_planilla_confirms_it() -> None:
    content = _pdf(numbered_insert=True)
    _structure, built = _build(content, "libro.pdf")
    assert built.sections[-1].kind != once_structure.KIND_PHOTOS
    assert any(warning["code"] == "photo_insert_candidate" for warning in built.warnings)
    _structure, built = _build(content, "libro.pdf", {**SHEET, "photo_insert_pages": ["4-5"]})
    assert built.sections[-1].kind == once_structure.KIND_PHOTOS


def test_the_planilla_can_leave_inserts_out() -> None:
    _structure, built = _build(_pdf(), "libro.pdf", {**SHEET, "photo_inserts": "off"})
    assert all(section.kind != once_structure.KIND_PHOTOS for section in built.sections)
    assert any(warning["code"] == "photo_insert_off" for warning in built.warnings)


def _epub(tmp_path: Path, insert_name: str, *, with_hint: bool) -> bytes:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("fotos")
    book.set_title("El faro")
    book.set_language("es")
    one = epub.EpubHtml(title="Uno", file_name="uno.xhtml", lang="es")
    one.content = f"<html><body><h1>Capítulo 1</h1><p>{TEXT}</p></body></html>"
    insert = epub.EpubHtml(title="Fotos", file_name=insert_name, lang="es")
    hint = ' class="encarte"' if with_hint else ""
    insert.content = (f"<html><body><section{hint}>"
                      '<figure><img src="images/f1.png" alt="Un faro blanco al atardecer"/>'
                      "<figcaption>El faro en 1910.</figcaption></figure>"
                      '<figure><img src="images/f2.png" alt=""/></figure></section></body></html>')
    two = epub.EpubHtml(title="Dos", file_name="dos.xhtml", lang="es")
    two.content = f"<html><body><h1>Capítulo 2</h1><p>{TEXT}</p></body></html>"
    for index, shade in ((1, 90), (2, 170)):
        book.add_item(epub.EpubItem(uid=f"f{index}", file_name=f"images/f{index}.png", media_type="image/png",
                                    content=_photo(shade)))
    for item in (one, insert, two):
        book.add_item(item)
    book.toc = (epub.Link("uno.xhtml", "Uno", "uno"), epub.Link("dos.xhtml", "Dos", "dos"))
    book.spine = [one, insert, two]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    target = tmp_path / "fotos.epub"
    epub.write_epub(str(target), book)
    return target.read_bytes()


def test_an_epub_insert_the_book_names_is_read_at_the_end_with_its_images(tmp_path) -> None:
    content = _epub(tmp_path, "laminas.xhtml", with_hint=True)
    structure, built = _build(content, "fotos.epub")
    assert [insert.evidence for insert in structure.photo_inserts] == ["strong"]
    # Not a chapter of its own, and its caption is not read where it was.
    assert not any("1910" in paragraph for chapter in structure.chapters for paragraph in chapter.paragraphs)
    last = built.sections[-1]
    texts = [paragraph.text for paragraph in last.paragraphs]
    assert texts[:3] == ["Fotografía 1.", "El faro en 1910.", "Fotografía 2."]
    # The book's alt text is a description: it is not read.
    assert not any("atardecer" in paragraph.text for section in built.sections for paragraph in section.paragraphs)
    reference = [section.title for section in built.sections for paragraph in section.paragraphs
                 if paragraph.role == "photo_reference"]
    assert reference == ["Capítulo 1"]  # after the document before the insert
    package, records = _package(built, tmp_path, "fotos.epub", content)
    images = sorted(path.name for path in (package / "images").iterdir())
    assert images == ["photo-001.png", "photo-002.png"]
    checks = {check["id"]: check["status"] for check in once_validation.verify(package, records,
                                                                                built.resolved.as_dict())}
    assert checks["once_photos"] == "passed"


def test_an_epub_document_of_images_the_book_does_not_name_stays_where_it_is(tmp_path) -> None:
    content = _epub(tmp_path, "doc2.xhtml", with_hint=False)
    structure, built = _build(content, "fotos.epub")
    assert [insert.evidence for insert in structure.photo_inserts] == ["candidate"]
    assert all(section.kind != once_structure.KIND_PHOTOS for section in built.sections)
    _structure, built = _build(content, "fotos.epub", {**SHEET, "photo_insert_documents": ["doc2.xhtml"]})
    assert built.sections[-1].kind == once_structure.KIND_PHOTOS


def test_a_production_job_delivers_the_photographs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "asr_secondary_provider": "none", "quality_review_enabled": True}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    client = TestClient(main.app)
    response = client.post("/api/books/jobs", data={"planilla_json": json.dumps(SHEET),
                                                    "metadata_json": json.dumps({"title": "El faro",
                                                                                 "author": "Ana Pérez"})},
                           files={"file": ("libro.pdf", io.BytesIO(_pdf()), "application/pdf")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    job_dir = Path(job["source_path"]).parent
    chain = json.loads((job_dir / "completeness.json").read_text(encoding="utf-8"))
    statuses = {check["id"]: check["status"] for check in chain["checks"]}
    assert statuses["once_photos"] == "passed" and statuses["units_to_dtbook"] == "passed", statuses
    with zipfile.ZipFile(job_dir / f"{job_id}-once-delivery.zip") as archive:
        names = archive.namelist()
    assert {"DAISY 3/images/photo-001.png", "DAISY 3/images/photo-003.png"} <= set(names)
    roles = [(unit.get("provenance") or {}).get("role") for unit in job["sentences"]]
    assert roles.count("photo") == 3 and roles.count("photo_reference") == 1
