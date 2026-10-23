"""PDF reading by layout: what is a title, a running head, a footnote, a split word."""

from __future__ import annotations

import fitz
import pytest

from app.book_structure import extract_book_structure, parse_page_range


def _book(marker: bool = False) -> bytes:
    """Three pages set like a printed book. With ``marker``, a footnote marker
    follows ADLAB on the same line in its own span, as real typesetting does."""
    pdf = fitz.open()
    serif, sans = fitz.Font("tiro"), fitz.Font("helv")

    def lines(page, rows):
        writer = fitz.TextWriter(page.rect)
        for y, text, size in rows:
            writer.append((51, y), text, font=serif, fontsize=size)
            if marker and text.endswith("ADLAB"):
                writer.append(writer.last_point, "1", font=sans, fontsize=size)
        writer.write_text(page)

    lines(pdf.new_page(width=397, height=595), [
        (120, "Capítulo 1", 15), (140, "El arte de hablar", 15),
        (200, "La audiodescripción es un servicio de accesibi-", 10),
        (213, "lidad para las personas ciegas. El proyecto ADLAB", 10),
        (226, "la define así.", 10),
        (300, "Este segundo párrafo continúa en la página siguien-", 10),
        (313, "te porque es largo y no termina aquí", 10),
        (510, "1 ADLAB es un proyecto europeo.", 8.5),
        (560, "19", 10),
    ])
    lines(pdf.new_page(width=397, height=595), [
        (30, "Capítulo 1. El arte de hablar", 8),
        (70, "sino en la otra página, y entonces acaba.", 10),
        (560, "20", 10),
    ])
    lines(pdf.new_page(width=397, height=595), [
        (120, "Capítulo 2", 15), (140, "Mirar con los sentidos", 15),
        (200, "Un capítulo nuevo empieza aquí.", 10),
        (560, "21", 10),
    ])
    data = pdf.tobytes()
    pdf.close()
    return data


def test_a_typeset_pdf_is_read_as_chapters_of_clean_paragraphs() -> None:
    book = extract_book_structure("libro.pdf", _book(marker=True))
    titles = [chapter.title for chapter in book.chapters]
    assert titles == ["Capítulo 1. El arte de hablar", "Capítulo 2. Mirar con los sentidos"]
    text = " ".join(book.chapters[0].paragraphs)
    # A word split at a line end is one word again: "accesibi- lidad" read aloud sounded like a stutter.
    assert "accesibilidad" in text and "accesibi" not in text.replace("accesibilidad", "")
    # Page numbers and the running head are not narrated.
    assert " 19" not in text and " 20" not in text
    assert text.count("El arte de hablar") == 0
    # A sentence that runs onto the next page stays one paragraph.
    assert "continúa en la página siguiente porque es largo y no termina aquí sino en la otra página" in text


def test_footnotes_are_read_at_the_end_of_their_chapter_without_their_markers() -> None:
    paragraphs = extract_book_structure("libro.pdf", _book(marker=True)).chapters[0].paragraphs
    assert "El proyecto ADLAB la define así." in " ".join(paragraphs)
    assert paragraphs[-1] == "Nota 1. ADLAB es un proyecto europeo."


def test_a_page_range_reads_only_those_pages() -> None:
    book = extract_book_structure("libro.pdf", _book(), pages="3-3")
    assert [chapter.title for chapter in book.chapters] == ["Capítulo 2. Mirar con los sentidos"]
    assert book.chapters[0].paragraphs == ["Un capítulo nuevo empieza aquí."]


@pytest.mark.parametrize("value,expected", [
    ("20-114", (20, 114)), ("20 - 114", (20, 114)), ("20–114", (20, 114)), ("7", (7, 7)),
    ("20-500", (20, 128)), ("", None),
])
def test_page_ranges_are_read_the_way_people_write_them(value, expected) -> None:
    assert parse_page_range(value, 128) == expected


@pytest.mark.parametrize("value", ["114-20", "abc", "0-5", "200-210"])
def test_an_impossible_page_range_is_explained(value) -> None:
    with pytest.raises(ValueError):
        parse_page_range(value, 128)


def test_epub_notes_and_subheadings_are_read_where_a_listener_expects_them(monkeypatch) -> None:
    from app import book_structure

    chapter = """<html xmlns:epub="http://www.idpf.org/2007/ops"><body><section>
    <h1>Capítulo 1. La llegada</h1>
    <p>Conocía cada escalón antes de que llegara.<a epub:type="noteref" href="#n1">1</a></p>
    <h2>El inventario</h2>
    <p>Había 12 mechas de <i>repuesto</i>.</p>
    <blockquote><p>Una cita leída una sola vez.</p></blockquote>
    <aside epub:type="footnote" id="n1"><p>1. El faro se inauguró en 1861.</p></aside>
    </section></body></html>"""

    class Item:
        def get_type(self):
            return 9

        def get_name(self):
            return "cap1.xhtml"

        def get_content(self):
            return chapter.encode("utf-8")

    class Book:
        spine = [("cap1", "yes")]

        def get_item_with_id(self, _item_id):
            return Item()

        def get_metadata(self, *_args):
            return []

    monkeypatch.setattr(book_structure.epub, "read_epub", lambda _path: Book())
    monkeypatch.setattr(book_structure.ebooklib, "ITEM_DOCUMENT", 9)
    result = book_structure.extract_book_structure("faro.epub", b"epub")
    assert result.chapters[0].title == "Capítulo 1. La llegada"
    assert result.chapters[0].paragraphs == [
        # The note reference is not read as "uno" at the end of the sentence.
        "Conocía cada escalón antes de que llegara.",
        "El inventario",
        "Había 12 mechas de repuesto.",
        "Una cita leída una sola vez.",
        "Nota 1. El faro se inauguró en 1861.",
    ]
