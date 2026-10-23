"""Words must reach the voice whole and be read as a reader would say them."""

from __future__ import annotations

import pytest

from app.book_structure import _paragraphs
from app.sentences import build_synthesis_text
from app.spanish_speech import roman_to_int, spell_numbers_for_speech


# --------------------------------------------------------------------------- #
# Words a line break split in two
# --------------------------------------------------------------------------- #

def test_a_word_hyphenated_at_a_line_end_is_rejoined() -> None:
    # Books hyphenate a word that does not fit. Left alone this reached the
    # voice as "distribui- dores" and was read as two broken pieces.
    text = ("El pueblo estaba lleno de distribui-\ndores de agua. La disca-\n"
            "pacidad no era un obsta-\nculo.")
    assert _paragraphs(text) == [
        "El pueblo estaba lleno de distribuidores de agua. "
        "La discapacidad no era un obstaculo.",
    ]


def test_a_real_hyphen_is_kept() -> None:
    # A compound or a code keeps its hyphen: the next piece is not a lower-case
    # continuation of one word.
    assert _paragraphs("Covid-\n19 cambio todo.") == ["Covid-19 cambio todo."]
    assert _paragraphs("Un acuerdo franco-\nAleman.") == ["Un acuerdo franco-Aleman."]


def test_the_invisible_soft_hyphen_is_removed() -> None:
    # U+00AD only marks where a break is allowed; it is never spoken.
    assert _paragraphs("discapa­cidad plena") == ["discapacidad plena"]


def test_paragraphs_are_still_separated() -> None:
    assert _paragraphs("Primero.\n\nSegundo.") == ["Primero.", "Segundo."]
    assert _paragraphs("   \n\n  ") == []


def test_rejoining_reaches_the_narration_copy() -> None:
    paragraph = _paragraphs("La disca-\npacidad importa.")[0]
    assert "disca- pacidad" not in build_synthesis_text(paragraph, {})
    assert "discapacidad" in build_synthesis_text(paragraph, {})


# --------------------------------------------------------------------------- #
# Roman numerals in chapter headings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heading,spoken", [
    ("Capitulo XIV", "Capitulo catorce"),
    ("Capítulo XVII. La tormenta", "Capítulo diecisiete. La tormenta"),
    ("Parte III", "Parte tres"),
    ("Tomo II", "Tomo dos"),
    ("Apendice IV", "Apendice cuatro"),
    ("Seccion IX", "Seccion nueve"),
])
def test_a_chapter_number_is_read_as_a_number(heading, spoken) -> None:
    assert spell_numbers_for_speech(heading) == spoken


@pytest.mark.parametrize("text", [
    "Siglo XX",            # a century, read as written
    "Juan Carlos I",       # a name
    "Vitamina D",          # a letter
    "Capitulo IIII",       # not a Roman numeral anyone writes
    "La X marca el lugar",
])
def test_letters_that_only_look_like_numerals_are_left_alone(text) -> None:
    assert spell_numbers_for_speech(text) == text


def test_roman_parsing_accepts_only_well_formed_numerals() -> None:
    assert roman_to_int("XIV") == 14
    assert roman_to_int("MCMXCIV") == 1994
    assert roman_to_int("IIII") is None
    assert roman_to_int("XXXX") is None
    assert roman_to_int("ABC") is None
    assert roman_to_int("") is None


def test_numbers_and_numerals_are_handled_in_one_pass() -> None:
    assert spell_numbers_for_speech("3.500 euros en el Capitulo XX") == \
        "tres mil quinientos euros en el Capitulo veinte"


# --------------------------------------------------------------------------- #
# The reviewer must not be asked about a heading that was read correctly
# --------------------------------------------------------------------------- #

def test_a_chapter_numeral_matches_however_the_transcript_writes_it() -> None:
    from app import qa_review

    # Narration says "Parte tres"; the recognizer writes "Parte 3". Without
    # this, every chapter heading in the book came back for review.
    for heard in ("Parte 3", "Parte tres", "Parte III"):
        verdict, issues, _confidence = qa_review.evaluate("Parte III", heard)
        assert verdict == "verified", (heard, issues)
    for heard in ("Capitulo 14", "Capitulo catorce", "Capitulo XIV"):
        assert qa_review.evaluate("Capitulo XIV", heard)[0] == "verified", heard


def test_a_wrong_chapter_number_is_still_caught() -> None:
    from app import qa_review

    assert qa_review.evaluate("Parte III", "Parte 4")[0] == "likely_error"
    assert qa_review.evaluate("Capitulo XIV", "Capitulo 15")[0] == "likely_error"


def test_numerals_outside_a_heading_are_not_treated_as_numbers() -> None:
    from app import qa_review

    # A century and a regnal name are not chapter numbers.
    assert qa_review.evaluate("Siglo XX", "Siglo 20")[0] == "likely_error"
    assert qa_review.evaluate("Juan Carlos I", "Juan Carlos 1")[0] == "likely_error"
