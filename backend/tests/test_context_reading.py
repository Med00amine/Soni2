"""Reading rules that depend on where the text is: headings, bibliographies, notes, prose.

ONCE B: foreign words in headings, and foreign surnames in bibliographies and
notes, are spelled out; nowhere else. ONCE F: Roman numerals, acronyms,
initialisms, abbreviations and numbers are read correctly in each context,
without over-normalizing. The dataset below pins the spoken form of each case;
the written text is never changed, and ordinary prose is read exactly as
before (its cache keys do not move).
"""

from __future__ import annotations

import pytest

from app import foreign_spelling, main, once_structure, planilla
from app.book_structure import extract_book_structure
from app.foreign_spelling import SpellingRules
from app.narration_plan import apply_narration_selection, build_narration_plan
from app.spanish_normalization import compare_transcripts
from app.spanish_text_normalizer import normalize_for_speech

RULES = SpellingRules()


def spoken(text: str, context: str = "text", **options) -> str:
    return normalize_for_speech(text, context, spelling=options.pop("spelling", RULES), **options).spoken


# --------------------------------------------------------------------------- #
# B: what is spelled, where
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heading, expected", [
    ("Capítulo 3. Shakespeare y Cervantes",
     "Capítulo 3. Shakespeare (ese, hache, a, ka, e, ese, pe, e, a, erre, e) y Cervantes"),
    ("Walter en Madrid", "Walter (uve doble, a, ele, te, e, erre) en Madrid"),
    ("El sueño de Müller", "El sueño de Müller (eme, u con diéresis, ele, ele, e, erre)"),
    ("Recuerdos de François", "Recuerdos de François (efe, erre, a, ene, ce con cedilla, o, i, ese)"),
    ("La Weltanschauung", "La Weltanschauung (uve doble, e, ele, te, a, ene, ese, ce, hache, a, u, u, ene, ge)"),
    # Spanish words, Spanish names and words Spanish has adopted are not spelled.
    ("La pingüina y el cigüeñal", "La pingüina y el cigüeñal"),
    ("Capítulo 2. La llegada", "Capítulo 2. La llegada"),
    ("El kilo de pan y el whisky", "El kilo de pan y el whisky"),
    ("LOS OJOS VERDES", "LOS OJOS VERDES"),
    ("Antonio Pérez Galdós", "Antonio Pérez Galdós"),
])
def test_foreign_words_in_headings_are_spelled_and_spanish_ones_are_not(heading, expected) -> None:
    assert spoken(heading, "heading") == expected


def test_a_word_with_only_a_weak_sign_is_flagged_not_spelled() -> None:
    result = normalize_for_speech("El club de Gilbert", "heading", spelling=RULES)
    assert result.spoken == "El club de Gilbert"
    assert any(flag.code == "foreign_word_review" and flag.text == "Gilbert" for flag in result.flags)


def test_words_the_book_marks_as_another_language_are_spelled_even_if_they_look_spanish() -> None:
    assert spoken("La casa de Home", "heading", foreign_terms=["Home"]) == "La casa de Home (hache, o, eme, e)"
    assert spoken("La casa de Home", "heading") == "La casa de Home"


def test_acronyms_in_a_heading_are_not_spelled_as_foreign_words() -> None:
    assert spoken("La ONU y la OTAN", "heading") == "La ONU y la OTAN"


@pytest.mark.parametrize("entry, expected", [
    ("Smith, J. (2004). The Lighthouse. Londres: Penguin.",
     "Smith (ese, eme, i, te, hache), J. (2004). The Lighthouse. Londres: Penguin."),
    ("García Márquez, G. (1967). Cien años de soledad. Buenos Aires.",
     "García Márquez, G. (1967). Cien años de soledad. Buenos Aires."),
    ("Müller, H. y Schmidt, K. (1999). Geschichte.",
     "Müller (eme, u con diéresis, ele, ele, e, erre), H. y Schmidt (ese, ce, hache, eme, i, de, te), K. (1999). "
     "Geschichte."),
])
def test_foreign_surnames_in_a_bibliography_are_spelled_and_nothing_else(entry, expected) -> None:
    assert spoken(entry, "bibliography") == expected


@pytest.mark.parametrize("note, expected", [
    ("Véase Thompson (2004), p. 12.", "Véase Thompson (te, hache, o, eme, pe, ese, o, ene) (2004), página 12."),
    ("Thompson, op. cit., p. 40.", "Thompson (te, hache, o, eme, pe, ese, o, ene), obra citada, página 40."),
    # A foreign name that is not cited as an author is not spelled in a note.
    ("Thompson llegó a la isla en barco.", "Thompson llegó a la isla en barco."),
])
def test_foreign_surnames_cited_in_a_note_are_spelled(note, expected) -> None:
    assert spoken(note, "note") == expected


@pytest.mark.parametrize("text", [
    "Shakespeare vivió en Londres y escribió para el teatro Globe.",
    "Smith, J. llegó tarde.",
    "Véase Thompson (2004), p. 12.",
    "IV. La llegada fue tranquila.",
    "Leyó a Müller y a Schmidt.",
])
def test_ordinary_prose_is_never_spelled_and_reads_exactly_as_before(text) -> None:
    assert spoken(text) == normalize_for_speech(text).spoken
    assert "(ese" not in spoken(text) and "(te" not in spoken(text)


def test_the_spelling_format_and_word_lists_are_settings() -> None:
    rules = SpellingRules(template="{word}, se deletrea {letters},", never=frozenset({"walter"}),
                          always=frozenset({"casa"}))
    assert spoken("Walter en la Casa", "heading", spelling=rules) == "Walter en la Casa, se deletrea ce, a, ese, a,"
    assert spoken("Shakespeare", "heading", spelling=SpellingRules(enabled=False)) == "Shakespeare"


def test_the_written_text_is_never_changed() -> None:
    result = normalize_for_speech("Capítulo 3. Shakespeare", "heading", spelling=RULES)
    assert result.source == "Capítulo 3. Shakespeare"
    change = next(change for change in result.changes if change.rule == "foreign_spelling")
    assert (change.original, change.start, change.end) == ("Shakespeare", 12, 23)


@pytest.mark.parametrize("word, letters", [
    ("Wöhler", "uve doble, o con diéresis, hache, ele, e, erre"),
    ("Ørsted", "o con barra, erre, ese, te, e, de"),
    ("Straße", "ese, te, erre, a, doble ese, e"),
    ("O'Brien", "o, apóstrofo, be, erre, i, e, ene"),
])
def test_letters_and_marks_have_their_spanish_names(word, letters) -> None:
    assert foreign_spelling.spell(word) == letters


# --------------------------------------------------------------------------- #
# F: Roman numerals, acronyms, initialisms, abbreviations, numbers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, context, expected", [
    # Roman numerals
    ("IV. La llegada", "heading", "cuatro. La llegada"),
    ("XII — El regreso", "heading", "doce — El regreso"),
    ("II", "heading", "dos"),
    ("Capítulo XIV", "heading", "Capítulo catorce"),
    ("Capítulo XIV", "text", "Capítulo catorce"),
    ("Parte II: El mar", "heading", "Parte dos: El mar"),
    ("En el siglo XIX llegó el tren.", "text", "En el siglo diecinueve llegó el tren."),
    ("Reinaba Felipe II en Castilla.", "text", "Reinaba Felipe segundo en Castilla."),
    ("IV. La llegada fue tranquila.", "text", "IV. La llegada fue tranquila."),  # only a heading starts so
    # A letter, not a five; and an Italian word in a heading is spelled.
    ("V de Vendetta", "heading", "V de Vendetta (uve, e, ene, de, e, te, te, a)"),
    # Acronyms are left for the voice (and flagged to listen for), never rewritten
    ("La ONCE y la ONU firmaron.", "text", "La ONCE y la ONU firmaron."),
    ("El DNI y el NIF.", "text", "El DNI y el NIF."),
    # Initialisms and abbreviations
    ("Viajó a EE. UU. en 1990.", "text", "Viajó a Estados Unidos en 1990."),
    ("Nació en el 50 a. C. en Roma.", "text", "Nació en el 50 antes de Cristo en Roma."),
    ("El Sr. Pérez y la Dra. Ruiz.", "text", "El señor Pérez y la doctora Ruiz."),
    ("Véase la pág. 12.", "text", "Véase la página 12."),
    # Numbers in headings, bibliographies and prose
    ("Capítulo 1", "heading", "Capítulo 1"),
    ("Smith, J. (2004). Faros, 2.ª ed., pp. 23-45.", "bibliography",
     "Smith (ese, eme, i, te, hache), J. (2004). Faros, segunda edición, páginas 23 a 45."),
    ("Pérez, A. (ed.) (2010). Islas, vol. 3, trad. de B. Ruiz, s. f.", "bibliography",
     "Pérez, A. (editor) (2010). Islas, volumen 3, traducción de B. Ruiz, sin fecha."),
    ("Ruiz et al., cap. 4, cf. p. 7.", "note", "Ruiz y otros, capítulo 4, compárese página 7."),
    ("García, P. (1999). Faros. Madrid: Espasa, 2.ª ed.", "bibliography",
     "García, P. (1999). Faros. Madrid: Espasa, segunda edición."),
    ("Asistieron 3.500 personas.", "text", "Asistieron tres mil quinientas personas."),
    ("Medía 2,5 metros.", "text", "Medía dos coma cinco metros."),
    ("Tenía 147 ventanas.", "text", "Tenía 147 ventanas."),
    ("Llegó a las 18:30 h.", "text", "Llegó a las dieciocho treinta horas."),
    # Ambiguous abbreviations stay as written outside a bibliography or a note
    ("Ver ed. anterior.", "text", "Ver ed. anterior."),
])
def test_reading_rules_by_context(text, context, expected) -> None:
    assert spoken(text, context) == expected


def test_citation_rules_apply_only_in_bibliographies_and_notes() -> None:
    assert spoken("Lo dijo en la p. 12 y en op. cit.", "text") == "Lo dijo en la p. 12 y en op. cit."
    assert spoken("Lo dijo en la p. 12 y en op. cit.", "note") == "Lo dijo en la página 12 y en obra citada."


# --------------------------------------------------------------------------- #
# Speech review: a spelled word is heard as the same letters
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heard", [
    "Capítulo 3. Shakespeare, S H A K E S P E A R E, y Cervantes.",
    "Capítulo tres. Shakespeare, ese hache a ka e ese pe e a erre e, y Cervantes.",
])
def test_a_recognizer_writing_letters_or_their_names_matches(heard) -> None:
    expected = spoken("Capítulo 3. Shakespeare y Cervantes", "heading")
    assert compare_transcripts(expected, heard) == []


def test_a_wrong_or_missing_letter_is_still_a_difference() -> None:
    expected = spoken("Capítulo 3. Shakespeare y Cervantes", "heading")
    assert compare_transcripts(expected, "Capítulo 3. Shakespeare, S H A K E S P E A R, y Cervantes.")


def test_prose_with_letter_like_words_is_not_folded() -> None:
    assert compare_transcripts("dije a ese te ama", "dije a ese te ama") == []
    assert compare_transcripts("las vocales a e i o u", "las vocales a e i o u") == []


# --------------------------------------------------------------------------- #
# Through the application
# --------------------------------------------------------------------------- #

DTBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" version="2005-3">
<head><meta name="dc:Title" content="Viajeros"/><meta name="dc:Creator" content="Ana Pérez"/></head>
<book><bodymatter>
<level1><h1>Capítulo 1. Shakespeare en Madrid</h1><p>Shakespeare nunca vino a Madrid, dice Smith (2004).</p>
<note id="n1"><p>1. Véase Smith (2004), p. 12.</p></note></level1>
<level1><h1>IV. La casa <span xml:lang="en">Home</span></h1><p>La casa estaba vacía.</p></level1>
<level1><h1>Bibliografía</h1><p>Smith, J. (2004). Travellers. Londres, pp. 23-45.</p></level1>
</bodymatter></book></dtbook>
"""


def _units(sheet: dict | None):
    structure = extract_book_structure("viajeros.xml", DTBOOK.encode("utf-8"))
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, {section.id for section in plan})
    if sheet is None:
        return main.build_units(selected, "hash", {}, structure)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": "Viajeros", "author": "Ana Pérez"}, planilla.from_dict(sheet))
    return main.build_units(built.chapters(), "hash", {}, structure, records=built.records())


def test_a_once_book_is_read_by_context_and_its_text_kept() -> None:
    units = {unit["text"]: unit for unit in _units({"production_date": "noviembre de 2026"})}
    heading = units["Capítulo 1. Shakespeare en Madrid"]
    assert heading["spoken_text"] == ("Capítulo 1. Shakespeare (ese, hache, a, ka, e, ese, pe, e, a, erre, e) "
                                      "en Madrid")
    assert heading["provenance"]["speech_context"] == "heading"
    # The prose that names him is read as it is.
    assert units["Shakespeare nunca vino a Madrid, dice Smith (2004)."]["spoken_text"] == (
        "Shakespeare nunca vino a Madrid, dice Smith (2004).")
    assert units["Nota 1. Véase Smith (2004), p. 12. Fin de nota 1."]["spoken_text"] == (
        "Nota 1. Véase Smith (ese, eme, i, te, hache) (2004), página 12. Fin de nota 1.")
    # The heading the book marks as English, opening with a Roman numeral.
    assert units["IV. La casa Home"]["spoken_text"] == "cuatro. La casa Home (hache, o, eme, e)"
    assert units["Smith, J. (2004). Travellers. Londres, pp. 23-45."]["spoken_text"] == (
        "Smith (ese, eme, i, te, hache), J. (2004). Travellers. Londres, páginas 23 a 45.")
    # The index lists the heading the way it is read.
    assert "(ese, hache" in units["Capítulo 1. Shakespeare en Madrid."]["spoken_text"]
    assert units["IV. La casa Home."]["spoken_text"] == "cuatro. La casa Home (hache, o, eme, e)."
    # Nothing in the book's own text changed.
    assert "Capítulo 1. Shakespeare en Madrid" in units


def test_without_the_once_structure_nothing_is_read_differently() -> None:
    for unit in _units(None):
        assert unit["spoken_text"] == normalize_for_speech(unit["text"]).spoken


def test_speech_contexts_can_be_turned_off_or_on_for_every_book(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"speech_contexts": "off"}))
    for unit in _units({"production_date": "noviembre de 2026"}):
        assert "(ese, hache" not in unit["spoken_text"]
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"speech_contexts": "always"}))
    assert any("(ese, hache" in unit["spoken_text"] for unit in _units(None))


def test_the_direct_download_reads_each_paragraph_as_the_reviewed_run_does() -> None:
    from app.daisy_export import make_paragraphs
    from app.sentences import build_synthesis_text

    structure = extract_book_structure("viajeros.xml", DTBOOK.encode("utf-8"))
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, {section.id for section in plan})
    sheet = planilla.from_dict({"production_date": "noviembre de 2026"})
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": "Viajeros", "author": "Ana Pérez"}, sheet)
    reviewed = [unit["synthesis_text"] for unit in main.build_units(built.chapters(), "hash", {}, structure,
                                                                     records=built.records())]
    # As _build_direct_package lays the book out.
    chapter_paragraphs, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        chapter_paragraphs.append((section.title, ([section.title] if section.heading_spoken else [])
                                   + [part for text in texts for part in make_paragraphs(text)]))
        layout.append({"kind": section.kind, "level": section.level,
                       "units": once_structure.unit_layout(record, texts, section.heading_spoken, make_paragraphs)})
    paragraphs = [text for _title, body in chapter_paragraphs for text in body]
    contexts, terms = main.direct_reading(structure, built, chapter_paragraphs, layout, paragraphs)
    direct = [build_synthesis_text(text, {}, context, foreign_terms=foreign, spelling=main.spelling_rules())
              for text, context, foreign in zip(paragraphs, contexts, terms)]
    # The same texts, so the two paths share every cached recording.
    assert direct == reviewed
