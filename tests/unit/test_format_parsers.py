from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.ingestion.detection import detect_format, parser_for
from app.ingestion.parsers import BookParseError


def test_html_parser_extracts_semantic_reading_order(tmp_path: Path) -> None:
    source = tmp_path / "book.html"
    source.write_text(
        "<html lang='es'><head><title>HTML Book</title><script>ignore()</script></head>"
        "<body><nav>Skip</nav><article><h1>Chapter one</h1><p>First.</p></article>"
        "<section><h2>Chapter two</h2><p>Second.</p></section></body></html>",
        encoding="utf-8",
    )

    book = parser_for(source).parse(source)

    assert book.source_format == "html"
    assert [chapter.title for chapter in book.chapters] == ["Chapter one", "Chapter two"]
    assert [sentence.text for chapter in book.chapters for paragraph in chapter.paragraphs
            for sentence in paragraph.sentences] == ["First.", "Second."]
    assert parser_for(source).parse(source).content_fingerprint == book.content_fingerprint


def test_epub_parser_uses_spine_order(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml",
                         "<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                         "<rootfiles><rootfile full-path='OPS/package.opf'/></rootfiles></container>")
        archive.writestr("OPS/package.opf",
                         "<package xmlns='http://www.idpf.org/2007/opf'><metadata>"
                         "<dc:title xmlns:dc='http://purl.org/dc/elements/1.1/'>EPUB Book</dc:title>"
                         "</metadata><manifest><item id='two' href='two.xhtml' media-type='application/xhtml+xml'/>"
                         "<item id='one' href='one.xhtml' media-type='application/xhtml+xml'/></manifest>"
                         "<spine><itemref idref='one'/><itemref idref='two'/></spine></package>")
        archive.writestr("OPS/one.xhtml", "<html><body><h1>One</h1><p>First chapter.</p></body></html>")
        archive.writestr("OPS/two.xhtml", "<html><body><h1>Two</h1><p>Second chapter.</p></body></html>")

    book = parser_for(source).parse(source)

    assert detect_format(source) == "epub"
    assert book.title == "EPUB Book"
    assert [chapter.title for chapter in book.chapters] == ["One", "Two"]


def test_invalid_pdf_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"not a pdf")
    with pytest.raises(BookParseError, match="valid PDF"):
        detect_format(source)
