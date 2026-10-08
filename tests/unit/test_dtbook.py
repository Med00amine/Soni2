from pathlib import Path

import pytest

from app.ingestion.dtbook import DTBookParseError, DTBookParser


FIXTURE = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"


def test_parser_extracts_metadata_and_reading_order() -> None:
    book = DTBookParser().parse(FIXTURE)

    assert book.id == "horizonte-2026-001-packaged"
    assert book.title == "El tiempo que nos queda"
    assert book.author == "Ana Martínez Ruiz"
    assert book.language == "es"
    chapter_ids = [chapter.id for chapter in book.chapters]
    assert chapter_ids[:2] == ["dedicatoria", "cita"]
    assert "prologo" in chapter_ids
    prologue = next(chapter for chapter in book.chapters if chapter.id == "prologo")
    assert prologue.paragraphs[1].sentences[1].id == "id_8"
    sentence = prologue.paragraphs[1].sentences[0]
    assert sentence.source_text == sentence.original_text == sentence.text


def test_parser_preserves_nested_sections_and_fallback_sentences() -> None:
    book = DTBookParser().parse(FIXTURE)
    prologue = next(chapter for chapter in book.chapters if chapter.id == "parte1")
    section = prologue.sections[0]

    assert section.id == "cap1"
    assert section.title == "Capítulo 1: El regreso"
    assert section.paragraphs[2].sentences[1].text.startswith("Alguien había cortado")


def test_parser_reports_invalid_xml(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.xml"
    invalid.write_text("<dtbook>", encoding="utf-8")

    with pytest.raises(DTBookParseError, match="Unable to parse"):
        DTBookParser().parse(invalid)
