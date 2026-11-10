"""ONCE: a note is announced, read, and its end announced; its reference keeps its place.

"Nota 1. … Fin de nota 1." comes from explicit note markup only (tested in
test_once_structure, test_extraction_quality, test_pdf_layout). Here: notes
are not confused with ordinary paragraphs or bibliography entries, and the
note reference the book has in its text is kept in the DTBook, at its place,
pointing at its note, without being read aloud or changing the narration.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from app import completeness, daisy_dtd, once_structure, once_validation, planilla
from app.book_structure import extract_book_structure
from app.daisy_export import build_daisy_package, make_paragraphs
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

CORPUS = Path(__file__).parent / "corpus"
MP3 = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 40
DTBOOK_NS = "{http://www.daisy.org/z3986/2005/dtbook/}"
SHEET = {"file2_page": "2", "index_page": "3", "production_date": "noviembre de 2026"}


def _build(source: bytes, name: str, sheet: dict | None = None):
    structure = extract_book_structure(name, source)
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": structure.title, "author": structure.author},
                                 planilla.from_dict(sheet or SHEET))
    return structure, built


def _package(built, tmp_path: Path, edit=None) -> tuple[Path, list[dict]]:
    chapters, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        if edit:
            texts = [edit(text) for text in texts]
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


def test_the_reference_keeps_its_place_and_points_at_its_note(tmp_path) -> None:
    _structure, built = _build((CORPUS / "el-faro-de-la-isla-almendra.xml").read_bytes(), "almendra.xml")
    package, records = _package(built, tmp_path)
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    refs = list(dtbook.iter(f"{DTBOOK_NS}noteref"))
    assert len(refs) == 1 and refs[0].text == "1"
    notes = {note.get("id"): note for note in dtbook.iter(f"{DTBOOK_NS}note")}
    assert refs[0].get("idref").lstrip("#") in notes
    note_text = completeness.reading_text(notes[refs[0].get("idref").lstrip("#")])
    assert "Nota 1." in note_text and "Fin de nota 1." in note_text
    # Exactly where the book had it: right after "apagarse", before the full stop.
    paragraph = next(element for element in dtbook.iter(f"{DTBOOK_NS}p") if refs[0] in list(element))
    assert paragraph.text.endswith("antes de apagarse") and refs[0].tail.startswith(". Clara subió")
    # It is a mark, not something read: the text compared with the narration has no "1" there.
    assert "apagarse. Clara" in completeness.reading_text(paragraph)
    checks = {check["id"]: check["status"] for check in once_validation.verify(package, records,
                                                                                built.resolved.as_dict())}
    assert checks["once_notes"] == "passed"


def test_the_narration_does_not_change(tmp_path) -> None:
    structure, built = _build((CORPUS / "el-faro-de-la-isla-almendra.xml").read_bytes(), "almendra.xml")
    texts = [paragraph.text for section in built.sections for paragraph in section.paragraphs]
    sentence = next(text for text in texts if "antes de apagarse" in text)
    # The reference is not in the narrated text: no "apagarse1", no "uno" read aloud.
    assert "antes de apagarse. Clara" in sentence


def test_a_reference_in_an_edited_paragraph_is_left_out_not_misplaced(tmp_path) -> None:
    _structure, built = _build((CORPUS / "el-faro-de-la-isla-almendra.xml").read_bytes(), "almendra.xml")
    package, _records = _package(built, tmp_path,
                                 edit=lambda text: text.replace("antes de apagarse", "antes de que se apagara"))
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    assert not list(dtbook.iter(f"{DTBOOK_NS}noteref"))
    validate_daisy_package(package)


def test_a_reference_in_the_second_part_of_a_long_paragraph(tmp_path) -> None:
    long_start = " ".join(["Una frase larga que se repite para llenar el párrafo."] * 16)
    source = ('<?xml version="1.0" encoding="UTF-8"?><dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" '
              'version="2005-3"><head><meta name="dc:Title" content="Notas"/></head><book><bodymatter>'
              f'<level1><h1>Capítulo 1</h1><p>{long_start} Y al final, la llamada<noteref idref="n7">7</noteref> '
              'cierra el párrafo.</p><note id="n7"><p>7. Una nota al final.</p></note></level1>'
              '</bodymatter></book></dtbook>').encode("utf-8")
    _structure, built = _build(source, "notas.xml")
    body = built.sections[3]
    paragraph = body.paragraphs[0]
    assert len(make_paragraphs(paragraph.text)) == 2
    package, _records = _package(built, tmp_path)
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    refs = list(dtbook.iter(f"{DTBOOK_NS}noteref"))
    assert [ref.text for ref in refs] == ["7"]
    holder = next(element for element in dtbook.iter(f"{DTBOOK_NS}p") if refs[0] in list(element))
    assert holder.text.endswith("Y al final, la llamada") and refs[0].tail == " cierra el párrafo."
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"


def test_ordinary_paragraphs_and_bibliography_entries_are_not_notes() -> None:
    source = ('<?xml version="1.0" encoding="UTF-8"?><dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" '
              'version="2005-3"><head><meta name="dc:Title" content="Notas"/></head><book><bodymatter>'
              '<level1><h1>Capítulo 1</h1><p>Nota: el faro se encendía al anochecer.</p>'
              '<p>1. Primer punto de una lista numerada del texto.</p>'
              '<p>Una llamada<noteref idref="n1">1</noteref> real.</p>'
              '<note id="n1" class="footnote"><p>1. La única nota.</p></note></level1>'
              '<level1><h1>Bibliografía</h1><p>1. Smith, J. (2004). The Lighthouse. Londres.</p></level1>'
              '</bodymatter></book></dtbook>').encode("utf-8")
    structure = extract_book_structure("notas.xml", source)
    elements = [(text, chapter.source_of(index).get("element")) for chapter in structure.chapters
                for index, text in enumerate(chapter.paragraphs)]
    notes = [text for text, element in elements if element == "note"]
    # Only the one marked as a note is a note; a paragraph that says "Nota:" and
    # a numbered line or bibliography entry stay what they are.
    assert notes == ["Nota 1. La única nota."]
    assert ("Nota: el faro se encendía al anochecer.", "p") in elements
    assert ("1. Smith, J. (2004). The Lighthouse. Londres.", "p") in elements
    _structure, built = _build(source, "notas.xml", {**SHEET})
    roles = [(paragraph.role, paragraph.text) for section in built.sections for paragraph in section.paragraphs]
    assert ("note", "Nota 1. La única nota. Fin de nota 1.") in roles
    assert ("text", "Nota: el faro se encendía al anochecer.") in roles


def test_footnotes_and_endnotes_of_an_epub_are_both_notes(tmp_path) -> None:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("notes")
    book.set_title("Notas")
    book.set_language("es")
    chapter = epub.EpubHtml(title="Uno", file_name="uno.xhtml", lang="es")
    chapter.content = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><h1>Capítulo 1</h1>'
        '<p>Primera llamada<a epub:type="noteref" href="#f1">1</a> y segunda<a epub:type="noteref" href="#e2">2</a>.</p>'
        '<aside epub:type="footnote" id="f1"><p>1. Nota al pie.</p></aside>'
        '<aside epub:type="endnote" id="e2"><p>2. Nota final.</p></aside></body></html>')
    book.add_item(chapter)
    book.toc = (epub.Link("uno.xhtml", "Uno", "uno"),)
    book.spine = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    target = tmp_path / "notes.epub"
    epub.write_epub(str(target), book)
    structure = extract_book_structure("notes.epub", target.read_bytes())
    chapter = structure.chapters[0]
    sources = [chapter.source_of(index) for index in range(len(chapter.paragraphs))]
    assert [text for text, source in zip(chapter.paragraphs, sources) if source.get("element") == "note"] == [
        "Nota 1. Nota al pie.", "Nota 2. Nota final."]
    # The notes are notes, not boxes, even though they are <aside>.
    assert not any(source.get("frame") for source in sources)
    calling = next(source for text, source in zip(chapter.paragraphs, sources) if text.startswith("Primera"))
    assert [(ref["label"], ref["at"]) for ref in calling["note_refs"]] == [("1", 15), ("2", 25)]
