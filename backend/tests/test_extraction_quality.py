"""Extraction must lose nothing silently: every drop has a reason, every doubt is reported."""

from __future__ import annotations

import io
from pathlib import Path

import fitz
import pytest

from app import book_structure, extraction_qa, ocr
from app.book_structure import extract_book_structure

CORPUS = Path(__file__).parent / "corpus"


def _all_text(structure) -> str:
    return "\n".join(chapter.title + "\n" + "\n".join(chapter.paragraphs) for chapter in structure.chapters)


# --------------------------------------------------------------------------- #
# DTBook
# --------------------------------------------------------------------------- #

def test_the_dtbook_corpus_is_extracted_completely() -> None:
    structure = extract_book_structure("libro.xml", (CORPUS / "el-faro-de-cabo-gris.xml").read_bytes())
    report = structure.extraction.as_dict()
    assert report["method"] == "dtbook"
    assert report["coverage"]["unaccounted_words"] == 0 and report["coverage"]["ratio"] == 1.0
    assert report["status"] == "passed"
    assert [chapter.title for chapter in structure.chapters] == [
        "Capítulo 1. La llegada", "Capítulo 2. El cuaderno de registro", "Capítulo 3. La visita"]
    assert not any(chapter.title_generated for chapter in structure.chapters)


def test_list_items_and_poem_lines_are_read() -> None:
    # The parser at the start of this upgrade read only <p>: the whole list
    # and the whole poem (48 of 548 words) disappeared without a trace.
    text = _all_text(extract_book_structure("libro.xml", (CORPUS / "el-faro-de-cabo-gris.xml").read_bytes()))
    assert "dos bidones de queroseno para el generador;" in text
    assert "pilas para la radio de onda corta;" in text
    assert "Quien guarda la luz del faro" in text and "que nadie lee del todo." in text


def test_page_numbers_and_note_marks_are_dropped_with_a_reason_and_notes_read_at_the_end() -> None:
    structure = extract_book_structure("libro.xml", (CORPUS / "el-faro-de-cabo-gris.xml").read_bytes())
    dropped = structure.extraction.as_dict()["dropped"]
    assert [item["text"] for item in dropped if item["kind"] == "page_number"] == ["1", "2", "3", "4", "5"]
    assert any(item["kind"] == "note_marker" and item["text"] == "1" for item in dropped)
    chapter = structure.chapters[1]
    assert chapter.paragraphs[-1].startswith("Nota 1. Hasta 1995")
    assert "la encendí a mano." in " ".join(chapter.paragraphs)  # no stray "1" in the sentence
    # Each paragraph knows the page it came from.
    assert chapter.sources[0]["page"] == "3"
    assert structure.chapters[2].sources[0]["page"] == "5"


def test_a_level_without_a_heading_gets_a_title_that_is_not_narrated() -> None:
    source = (b'<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/"><book><bodymatter>'
              b'<level1><p>Texto sin titulo.</p></level1></bodymatter></book></dtbook>')
    structure = extract_book_structure("libro.xml", source)
    assert structure.chapters[0].title == "Capítulo 1"
    assert structure.chapters[0].title_generated
    assert structure.extraction.generated_titles == ["Capítulo 1"]


def test_malformed_xml_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError):
        extract_book_structure("libro.xml", b"<book><p>sin cerrar</book>")


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #

class _Item:
    def __init__(self, name, content):
        self.name, self.content = name, content

    def get_type(self):
        return 9

    def get_name(self):
        return self.name

    def get_content(self):
        return self.content.encode("utf-8")


def _epub(monkeypatch, documents: dict[str, str], toc=()):
    class Book:
        spine = [(name, "yes") for name in documents]

        def get_item_with_id(self, item_id):
            return _Item(f"{item_id}.xhtml", documents[item_id])

        def get_metadata(self, *_args):
            return []

    Book.toc = list(toc)
    monkeypatch.setattr(book_structure.epub, "read_epub", lambda _path: Book())
    monkeypatch.setattr(book_structure.ebooklib, "ITEM_DOCUMENT", 9)
    return extract_book_structure("libro.epub", b"epub")


def test_an_epub_title_inside_header_is_kept(monkeypatch) -> None:
    # <header> used to be deleted before the title was looked for.
    result = _epub(monkeypatch, {"c1": "<html><body><section><header><h1>Capítulo 1. El viaje</h1></header>"
                                       "<p>Salieron de madrugada.</p></section></body></html>"})
    assert result.chapters[0].title == "Capítulo 1. El viaje"
    assert result.chapters[0].paragraphs == ["Salieron de madrugada."]


def test_epub_text_outside_paragraphs_is_not_lost(monkeypatch) -> None:
    result = _epub(monkeypatch, {"c1": "<html><body><h1>Uno</h1><p>Primer párrafo.</p>"
                                       "<div>Texto suelto en un div.</div>"
                                       "<dl><dt>Faro</dt><dd>Torre con luz.</dd></dl>"
                                       "<table><tr><td>Lunes</td><td>Calma</td></tr></table></body></html>"})
    paragraphs = result.chapters[0].paragraphs
    assert "Texto suelto en un div." in paragraphs
    assert "Faro" in paragraphs and "Torre con luz." in paragraphs
    assert "Lunes; Calma;" in paragraphs
    assert result.extraction.coverage["unaccounted_words"] == 0


def test_epub_note_links_are_removed_and_the_notes_read_at_the_end(monkeypatch) -> None:
    result = _epub(monkeypatch, {"c1": '<html><body><h1>Uno</h1><p>Llegó tarde<a href="#n1">1</a>.</p>'
                                       '<aside epub:type="footnote" id="n1">1. Era invierno.</aside></body></html>'})
    assert result.chapters[0].paragraphs == ["Llegó tarde.", "Nota 1. Era invierno."]
    assert any(item["kind"] == "note_marker" for item in result.extraction.as_dict()["dropped"])


def test_an_epub_footer_is_not_read_but_a_person_is_told(monkeypatch) -> None:
    result = _epub(monkeypatch, {"c1": "<html><body><h1>Uno</h1><p>Texto.</p>"
                                       "<footer>© Editorial Faro</footer></body></html>"})
    report = result.extraction.as_dict()
    assert result.chapters[0].paragraphs == ["Texto."]
    assert any(issue["code"] == "epub_footer" and issue["severity"] == "warning" for issue in report["issues"])
    assert report["coverage"]["unaccounted_words"] == 0  # recorded as a drop, not lost


def test_a_headingless_epub_document_uses_its_table_of_contents_title(monkeypatch) -> None:
    class Link:
        href, title = "c1.xhtml", "La llegada"

    result = _epub(monkeypatch, {"c1": "<html><body><p>Amanecía.</p></body></html>"}, toc=[Link()])
    assert result.chapters[0].title == "La llegada"
    # The table of contents names it, but the chapter text does not say it.
    assert result.chapters[0].title_generated


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _text_page(pdf, lines):
    page = pdf.new_page(width=400, height=600)
    writer = fitz.TextWriter(page.rect)
    for y, text in lines:
        writer.append((50, y), text, font=fitz.Font("tiro"), fontsize=10)
    writer.write_text(page)
    return page


def _image_page(pdf, text="Texto escaneado de la página."):
    """A page that is only a picture of text: no text layer at all."""
    source = fitz.open()
    _text_page(source, [(100, text)])
    pixmap = source[0].get_pixmap(dpi=100)
    page = pdf.new_page(width=400, height=600)
    page.insert_image(page.rect, pixmap=pixmap)
    return page


def test_an_image_only_page_without_ocr_blocks_the_book(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "available", lambda language="spa": False)
    pdf = fitz.open()
    _text_page(pdf, [(100, "La primera página tiene texto suficiente para leerla bien.")])
    _image_page(pdf)
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    report = structure.extraction.as_dict()
    assert report["status"] == "blocked"
    assert any(issue["code"] == "ocr_required" and "página 2" in issue["location"] for issue in report["issues"])
    assert [page["status"] for page in report["pages"]] == ["text", "image_only"]


def test_an_image_only_page_is_read_by_ocr_and_its_doubts_reported(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "available", lambda language="spa": True)
    monkeypatch.setattr(ocr, "ocr_pdf_page", lambda page, number, **_: ocr.OcrPage(
        number, ["Texto escaneado de la pagina."], mean_confidence=71.0, words=4,
        low_confidence_words=[{"word": "pagina", "confidence": 41.0}]))
    pdf = fitz.open()
    _text_page(pdf, [(100, "La primera página tiene texto suficiente para leerla bien.")])
    _image_page(pdf)
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    report = structure.extraction.as_dict()
    assert "Texto escaneado de la pagina." in _all_text(structure)
    assert report["pages"][1]["method"] == "ocr"
    assert report["status"] == "needs_review"
    assert any(issue["code"] == "ocr_low_confidence" for issue in report["issues"])
    assert report["method"].endswith("+ocr")


@pytest.mark.skipif(not ocr.available("spa"), reason="Tesseract with the Spanish model is not installed")
def test_real_ocr_reads_a_scanned_spanish_page_and_mixes_it_with_text_pages() -> None:
    # A real scan-like page: Spanish text rendered to an image at 200 dpi.
    pdf = fitz.open()
    _text_page(pdf, [(100, "La primera página tiene texto suficiente para leerla bien.")])
    source = fitz.open()
    page = source.new_page(width=400, height=600)
    page.insert_text((40, 120), "El jilguero cantaba junto al cerezo.", fontsize=16, fontname="tiro")
    page.insert_text((40, 160), "Aquella mañana llovía en Cáceres.", fontsize=16, fontname="tiro")
    scanned = pdf.new_page(width=400, height=600)
    scanned.insert_image(scanned.rect, pixmap=page.get_pixmap(dpi=200))
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    report = structure.extraction.as_dict()
    text = _all_text(structure)
    assert report["pages"][1]["method"] == "ocr" and "+ocr" in report["method"]
    assert "jilguero" in text and "cerezo" in text and "Cáceres" in text
    assert report["status"] != "blocked"


def test_repeated_running_heads_and_page_numbers_are_removed_with_a_record() -> None:
    pdf = fitz.open()
    for number in range(1, 5):
        _text_page(pdf, [(30, "EL FARO DE CABO GRIS"), (100, f"Párrafo completo número {number} de la historia."),
                         (580, str(number))])
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    text = _all_text(structure)
    assert "EL FARO DE CABO GRIS" not in text
    dropped = structure.extraction.as_dict()["dropped"]
    assert sum(item["kind"] == "running_head" for item in dropped) == 4
    assert sum(item["kind"] == "page_number" for item in dropped) == 4
    assert structure.extraction.coverage["unaccounted_words"] == 0


def test_a_paragraph_cut_by_a_page_break_is_put_back_together() -> None:
    pdf = fitz.open()
    _text_page(pdf, [(100, "La niebla subía despacio desde el puerto y")])
    _text_page(pdf, [(100, "se quedaba enredada en los tejados.")])
    structure = extract_book_structure("libro.pdf", pdf.tobytes())
    assert structure.chapters[0].paragraphs == [
        "La niebla subía despacio desde el puerto y se quedaba enredada en los tejados."]
    assert all(chapter.title_generated for chapter in structure.chapters)


def test_tesseract_tsv_is_grouped_into_paragraphs_with_confidences() -> None:
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t96.0\tLa\n"
           "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t93.5\tniebla\n"
           "5\t1\t1\t1\t2\t1\t0\t0\t1\t1\t40.2\tsubí-\n"
           "5\t1\t1\t1\t3\t1\t0\t0\t1\t1\t88.0\ta\n"
           "5\t1\t2\t1\t1\t1\t0\t0\t1\t1\t91.0\tFin.\n")
    page = ocr.parse_tsv(tsv, 7)
    assert page.paragraphs == ["La niebla subía", "Fin."]
    assert page.words == 5
    assert page.low_confidence_words == [{"word": "subí-", "confidence": 40.2}]


# --------------------------------------------------------------------------- #
# Plain text and DOCX
# --------------------------------------------------------------------------- #

def test_plain_text_transcriber_marks_are_dropped_and_roman_sections_are_headings() -> None:
    structure = extract_book_structure("becquer.txt", (CORPUS / "becquer-los-ojos-verdes.txt").read_bytes())
    report = structure.extraction.as_dict()
    assert [chapter.title for chapter in structure.chapters] == ["Prólogo", "I", "II", "III"]
    assert structure.chapters[0].title_generated and not structure.chapters[1].title_generated
    assert "[Ilustración]" not in _all_text(structure)
    assert sum(item["kind"] == "transcriber_marker" for item in report["dropped"]) == 2
    assert report["coverage"]["unaccounted_words"] == 0


def test_docx_tables_are_read() -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("Antes de la tabla.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Faro", "Cabo Gris"
    document.add_paragraph("Después de la tabla.")
    buffer = io.BytesIO()
    document.save(buffer)
    structure = extract_book_structure("libro.docx", buffer.getvalue())
    assert structure.chapters[0].paragraphs == ["Antes de la tabla.", "Faro; Cabo Gris", "Después de la tabla."]
    assert structure.extraction.coverage["unaccounted_words"] == 0


# --------------------------------------------------------------------------- #
# The coverage measurement itself
# --------------------------------------------------------------------------- #

def test_words_that_vanish_are_reported_with_their_context() -> None:
    report = extraction_qa.ExtractionReport("txt")
    coverage = extraction_qa.measure_coverage(
        report, ["El faro estaba lejos. La llave era de hierro antiguo."], ["El faro estaba lejos."])
    assert coverage["unaccounted_words"] == 6
    assert report.warnings and report.warnings[0]["code"] == "unaccounted_words"
    assert any("hierro" in example["context"] for example in coverage["unaccounted_examples"])


def test_hyphenation_in_the_raw_text_is_not_counted_as_missing_words() -> None:
    report = extraction_qa.ExtractionReport("txt")
    coverage = extraction_qa.measure_coverage(report, ["La disca-\npacidad importa."], ["La discapacidad importa."])
    assert coverage["unaccounted_words"] == 0


def test_leftover_line_break_hyphens_and_ocr_tokens_are_flagged() -> None:
    report = extraction_qa.ExtractionReport("pdf")
    extraction_qa.inspect_text_quality(report, [("Uno", ["Los distribui- dores llegaron con l0s paquetes."])])
    codes = {issue["code"] for issue in report.issues}
    assert codes == {"broken_word", "suspicious_token"}
