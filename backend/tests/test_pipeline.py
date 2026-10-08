import io
import base64

import fitz
import pytest
from fastapi.testclient import TestClient
from ebooklib import epub

from app import main as app_module
from app.main import clean_text, extract_text


def test_text_extract_and_clean():
    result = clean_text(extract_text("sample.txt", b"  Hola   mundo.\n\n\n  Adi\u00ad\nos."))
    assert result == "Hola mundo.\n\nAdios."


def test_xml_extract():
    assert extract_text("sample.xml", b"<root><title>Inicio</title><p>Hola</p></root>") == "Inicio\nHola"


def test_pdf_extract():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Este es un libro de prueba con suficiente texto legible.")
    pdf = doc.tobytes()
    doc.close()
    assert "libro de prueba" in extract_text("sample.pdf", pdf)


def test_scanned_pdf_is_reported():
    doc = fitz.open()
    doc.new_page()
    pdf = doc.tobytes()
    doc.close()
    with pytest.raises(ValueError, match="scanned"):
        extract_text("scan.pdf", pdf)


def test_epub_extracts_chapter_text():
    book = epub.EpubBook()
    book.set_identifier("sample-book")
    book.set_title("Sample book")
    book.set_language("es")
    chapter = epub.EpubHtml(title="Capítulo uno", file_name="chapter1.xhtml", lang="es")
    chapter.content = "<html><body><h1>Capítulo uno</h1><p>Hola desde un EPUB.</p></body></html>"
    book.add_item(chapter)
    book.spine = [chapter]
    epub_bytes = io.BytesIO()
    epub.write_epub(epub_bytes, book)
    result = extract_text("sample.epub", epub_bytes.getvalue())
    assert "Capítulo uno" in result
    assert "Hola desde un EPUB." in result


def test_preview_returns_audio_and_matching_transcript(monkeypatch):
    async def fake_synthesize(text: str) -> bytes:
        assert text == "Este es un texto suficientemente largo para generar una vista previa."
        return b"sample-mp3"

    monkeypatch.setattr(app_module, "synthesize", fake_synthesize)
    client = TestClient(app_module.app)
    response = client.post(
        "/api/books/preview",
        files={"file": ("libro.txt", b"Este es un texto suficientemente largo para generar una vista previa.", "text/plain")},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["transcript"] == "Este es un texto suficientemente largo para generar una vista previa."
    assert base64.b64decode(result["audio_base64"]) == b"sample-mp3"
