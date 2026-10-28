"""Spanish QA outcomes: equivalent, ambiguous, and genuinely wrong.

These are the false positives reported against the previous behaviour: a
correctly spoken number read back by the recognizer as words must not be an
error, while a wrong number must stay one.
"""

from __future__ import annotations

import pytest

from app import qa_review


def verdict(expected: str, recognized: str) -> str:
    return qa_review.evaluate(expected, recognized)[0]


def issues(expected: str, recognized: str) -> list[dict[str, str]]:
    return qa_review.evaluate(expected, recognized)[1]


# --------------------------------------------------------------------------- #
# Correct after Spanish normalization — the reported false positives
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("source,spoken", [
    ("147", "ciento cuarenta y siete"),
    ("12", "doce"),
    ("3 500", "tres mil quinientos"),
    ("2,5", "dos coma cinco"),
    ("18 km", "dieciocho kilómetros"),
    ("960", "novecientos sesenta"),
    ("6:45", "seis cuarenta y cinco"),
    ("23:10", "veintitrés diez"),
    ("12%", "doce por ciento"),
    ("20 €", "veinte euros"),
    ("12/03/2024", "doce de marzo de dos mil veinticuatro"),
])
def test_equivalent_spoken_forms_are_verified_not_flagged(source, spoken) -> None:
    state, found, confidence = qa_review.evaluate(source, spoken)
    assert state == "verified", found
    assert found == []
    assert confidence == 1.0


def test_measurement_unit_is_not_counted_as_an_inserted_word() -> None:
    # "metros" correctly renders the source "m"; it is not an addition.
    state, found, _confidence = qa_review.evaluate("El camino mide 9 m.", "El camino mide nueve metros.")
    assert state == "verified"
    assert found == []


def test_accents_punctuation_and_case_do_not_create_issues() -> None:
    assert verdict("La canción está aquí.", "La cancion esta aqui") == "verified"


# --------------------------------------------------------------------------- #
# Genuine errors must survive
# --------------------------------------------------------------------------- #

def test_a_wrong_number_is_a_likely_error_never_ambiguous() -> None:
    state, found, confidence = qa_review.evaluate("147", "ciento cuarenta y ocho")
    assert state == "likely_error"
    assert len(found) == 1
    assert found[0]["category"] == qa_review.CATEGORY_MISMATCH
    assert found[0]["expected"] == "147"
    assert found[0]["recognized"] == "ciento cuarenta y ocho"
    assert confidence < 1.0


def test_a_decimal_is_never_confused_with_the_concatenated_integer() -> None:
    state, found, _confidence = qa_review.evaluate("2,5", "25")
    assert state == "likely_error"
    assert found[0]["category"] == qa_review.CATEGORY_MISMATCH


@pytest.mark.parametrize("source,spoken", [
    ("18 km", "diecinueve kilómetros"),
    ("9 km", "nueve metros"),
    ("960", "novecientos cincuenta"),
    ("3 20", "trescientos veinte"),
])
def test_different_quantities_and_units_stay_errors(source, spoken) -> None:
    state, found, _confidence = qa_review.evaluate(source, spoken)
    assert state == "likely_error"
    assert all(item["category"] == qa_review.CATEGORY_MISMATCH for item in found)


def test_a_missing_content_phrase_is_a_likely_error() -> None:
    state, found, _confidence = qa_review.evaluate(
        "El tren llegó al pueblo justo antes del amanecer.", "El tren llegó al pueblo.",
    )
    assert state == "likely_error"
    assert found[0]["difference_type"] == "omission"


def test_truncated_speech_is_detected_as_an_omission() -> None:
    state, found, confidence = qa_review.evaluate(
        "La casa estaba vacía desde hacía muchos años y nadie se atrevía a entrar.",
        "La casa estaba vacía",
    )
    assert state == "likely_error"
    assert any(item["difference_type"] == "omission" for item in found)
    assert confidence < 0.8


def test_repeated_content_is_reported_as_an_addition() -> None:
    state, found, _confidence = qa_review.evaluate(
        "El camino está abierto todos los días del año.",
        "El camino está abierto abierto abierto todos los días del año.",
    )
    assert state == "likely_error"
    assert any(item["difference_type"] == "addition_or_repetition" for item in found)


# --------------------------------------------------------------------------- #
# Ambiguous cases go to a person, not to another paid retake
# --------------------------------------------------------------------------- #

def test_a_dropped_short_function_word_is_ambiguous_not_an_error() -> None:
    state, found, _confidence = qa_review.evaluate(
        "El tren llegó al pueblo y se detuvo en la vieja estación de piedra.",
        "El tren llegó al pueblo se detuvo en la vieja estación de piedra.",
    )
    assert state == "ambiguous"
    assert found[0]["category"] == qa_review.CATEGORY_AMBIGUOUS
    assert "funcional" in found[0]["reason"]


def test_a_very_short_sentence_with_one_difference_is_ambiguous() -> None:
    # Recognizers are unreliable on two-word clips, so a person decides.
    assert verdict("Buenos días.", "Buenas días.") == "ambiguous"


def test_ambiguous_results_are_never_reported_as_automatically_correct() -> None:
    state, found, _confidence = qa_review.evaluate(
        "El tren llegó al pueblo y se detuvo en la vieja estación de piedra.",
        "El tren llegó al pueblo se detuvo en la vieja estación de piedra.",
    )
    assert state != "verified"
    assert found != []


# --------------------------------------------------------------------------- #
# Issue shape used by the report and the browser
# --------------------------------------------------------------------------- #

def test_every_issue_carries_a_type_category_and_explanation() -> None:
    for found in (issues("147", "ciento cuarenta y ocho"),
                  issues("Había 12 cajas en el almacén central.", "Había cajas en el almacén central.")):
        assert found
        for item in found:
            assert item["difference_type"] in {"omission", "addition_or_repetition", "substitution", "difference"}
            assert item["category"] in {qa_review.CATEGORY_AMBIGUOUS, qa_review.CATEGORY_MISMATCH}
            assert item["reason"]


def test_acoustic_defects_are_a_separate_category_from_misreadings() -> None:
    issue = qa_review.audio_issue("Hola.", "Hola.", '{"status": "needs_review"}')
    assert issue["category"] == qa_review.CATEGORY_AUDIO
    assert issue["difference_type"] == "audio_defect"


# --------------------------------------------------------------------------- #
# Observed live-recognizer behaviour
# --------------------------------------------------------------------------- #

def test_a_number_read_back_digit_by_digit_is_not_an_error() -> None:
    # Deepgram nova-3 transcribes the narrated "ciento cuarenta y siete" as
    # "1 4 7". Same quantity, different transcription style.
    state, found, _confidence = qa_review.evaluate("147", "1 4 7")
    assert state == "verified"
    assert found == []


def test_the_exact_live_deepgram_transcript_is_accepted() -> None:
    source = "La casa tenía 147 ventanas y el camino medía 18 km."
    spoken = "La casa tenía 1 4 7 ventanas y el camino medía 18 kilómetros"
    state, found, confidence = qa_review.evaluate(source, spoken)
    assert state == "verified", found
    assert found == []
    assert confidence == 1.0


def test_digit_readback_must_still_reconstruct_the_same_number() -> None:
    # "1 4 8" is a different quantity from 147 and stays an error.
    assert verdict("147", "1 4 8") == "likely_error"
    # Multi-digit groups are not single digits, so no folding happens.
    assert verdict("3 20", "trescientos veinte") == "likely_error"
    assert verdict("2,5", "25") == "likely_error"


# --------------------------------------------------------------------------- #
# Digit and mixed transcript forms, measured from Deepgram nova-3
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("source,spoken", [
    # The recognizer writes a correctly narrated time as two plain numbers.
    ("El tren salía a las 6:45 de la mañana.", "El tren salía a las 6 45 de la mañana."),
    ("Llegó a las 23:10 de la noche.", "Llegó a las 23 10 de la noche"),
    ("La reunión era a las 3:20.", "La reunión era a las 3 20."),
    # And a correctly narrated date with the day and year in digits.
    ("El 12/03/2024 firmaron el acuerdo.", "El 12 de marzo de 2024, firmaron el acuerdo."),
    ("Nació el 1/01/2000.", "Nació el 1 de enero del 2000."),
    # And "por ciento" as "por 100" or "por cien".
    ("El interés subió un 12%.", "El interés subió un 12 por 100"),
    ("Creció un 7%.", "Creció un 7 por cien"),
    # Unit abbreviations the recognizer spells out.
    ("La presión bajó a 960 hPa.", "La presión bajó a 960 hectopascales"),
    ("Sembraron 4 ha.", "Sembraron 4 hectáreas"),
    ("Pesaba 3 t.", "Pesaba 3 toneladas"),
])
def test_digit_and_spelled_out_transcript_forms_are_not_errors(source, spoken) -> None:
    state, found, _confidence = qa_review.evaluate(source, spoken)
    assert state == "verified", found


def test_two_adjacent_numbers_only_become_a_time_when_the_text_has_one() -> None:
    # Without a time in the source, "3 20" is two quantities, not 3:20.
    assert verdict("Contó 3 20 cajas.", "Contó trescientos veinte cajas.") == "likely_error"
    assert verdict("Contó 3 20 cajas.", "Contó 320 cajas.") == "likely_error"
    # A single-digit minute is not a clock minute either.
    assert verdict("Salía a las 6:45.", "Salía a las 6 5.") == "likely_error"


def test_a_wrong_unit_is_still_an_error_even_with_the_right_number() -> None:
    state, found, _confidence = qa_review.evaluate("El muro tenía 9 km.", "El muro tenía nueve metros.")
    assert state == "likely_error"
    assert found[0]["category"] == qa_review.CATEGORY_MISMATCH


def test_a_wrong_time_or_date_is_still_an_error() -> None:
    assert verdict("Salía a las 6:45.", "Salía a las 6 55.") == "likely_error"
    assert verdict("El 12/03/2024 firmaron.", "El 13 de marzo de 2024 firmaron.") == "likely_error"


def test_two_short_function_words_swapped_are_ambiguous_not_an_error() -> None:
    # Measured: "El interés" came back as "En interés".
    state, found, _confidence = qa_review.evaluate(
        "El interés subió mucho durante el primer trimestre del año.",
        "En interés subió mucho durante el primer trimestre del año.",
    )
    assert state == "ambiguous"
    assert found[0]["category"] == qa_review.CATEGORY_AMBIGUOUS


def test_plain_numbers_of_four_or_more_digits_are_compared_by_value() -> None:
    # The pattern previously only read one to three leading digits, so a bare
    # "3500" was never canonicalized and never matched "3.500" in the book.
    assert verdict("El presupuesto fue de 3.500 euros.", "El presupuesto fue de 3500 euros") == "verified"
    assert verdict("Costaba 1.200 pesetas.", "Costaba 1200 pesetas") == "verified"
    assert verdict("Eran 1.200.000 personas.", "Eran 1200000 personas") == "verified"
    assert verdict("El presupuesto fue de 3.500 euros.", "El presupuesto fue de 3400 euros") == "likely_error"


def test_a_decimal_spoken_back_with_digits_around_coma_matches() -> None:
    # Narration spells "2,5" out, so the transcript comes back mixed.
    assert verdict("La cuerda medía 2,5 metros.", "La cuerda medía 2 coma 5 metros.") == "verified"
    assert verdict("Pesaba 1,75 kg.", "Pesaba 1 coma 7 5 kilogramos") == "verified"
    # And the digits must still describe the same value.
    assert verdict("La cuerda medía 2,5 metros.", "La cuerda medía 2 coma 6 metros.") == "likely_error"
    assert verdict("La cuerda medía 2,5 metros.", "La cuerda medía 25 metros.") == "likely_error"


# --------------------------------------------------------------------------- #
# Alignment: several numeric expressions in one sentence
# --------------------------------------------------------------------------- #

def test_several_numeric_expressions_in_one_sentence_all_align() -> None:
    # A percentage late in the sentence used to block the earlier "18 km" from
    # being read as a measurement at all, so the two sides canonicalized
    # differently and identical text was reported as a difference.
    state, found, _confidence = qa_review.evaluate(
        "El camino media 18 km y el interes subio un 12%.",
        "El camino media 18 kilometros y el interes subio un 12 por 100",
    )
    assert state == "verified", found


def test_a_decimal_later_in_the_sentence_does_not_hide_an_earlier_amount() -> None:
    state, found, _confidence = qa_review.evaluate(
        "El presupuesto fue de 3.500 euros y la cuerda media 2,5 metros.",
        "El presupuesto fue de 3500 euros y la cuerda media, 2 coma 5 metros",
    )
    assert state == "verified", found


def test_two_times_in_one_sentence_both_fold() -> None:
    assert verdict("El tren salia a las 6:45 y llego a las 23:10.",
                   "El tren salia a las 6 45, y llego a las 23 10.") == "verified"


def test_identical_text_is_never_reported_as_a_difference() -> None:
    # The clearest symptom of the alignment bug was a report listing the same
    # string on both sides.
    for text in ("La casa tenia 147 ventanas y 12 puertas.",
                 "Mide 18 km, pesa 3 t y cubre 4 ha al 12%.",
                 "A las 6:45 del 12/03/2024 habia 3.500 personas y 2,5 metros de nieve."):
        state, found, confidence = qa_review.evaluate(text, text)
        assert state == "verified", found
        assert found == []
        assert confidence == 1.0


def test_an_article_swap_is_not_reported_as_a_wrong_quantity() -> None:
    # "un" and "una" also canonicalize to the numeral one, so an article swap
    # used to be reported as a quantity mismatch.
    state, found, _confidence = qa_review.evaluate(
        "la casa estaba vacia desde hacia muchos anos",
        "una casa estaba vacia desde hacia muchos anos",
    )
    assert state == "ambiguous"
    assert found[0]["category"] == qa_review.CATEGORY_AMBIGUOUS
    assert "cantidad" not in found[0]["reason"]


def test_a_numeral_narrated_as_an_article_still_matches() -> None:
    assert verdict("Habia 1 libro en la mesa del salon", "Habia un libro en la mesa del salon") == "verified"


# --------------------------------------------------------------------------- #
# Stutters, and what needs no person
# --------------------------------------------------------------------------- #

LONG = ("Amanecía despacio sobre el río cuando Marta abrió la ventana y miró el jardín, donde los pájaros "
        "cantaban entre las ramas mojadas del viejo almendro, y el olor a tierra húmeda llenaba la casa "
        "entera mientras la ciudad empezaba a despertar sin ninguna prisa aquel domingo de octubre.")


def accept_reason(expected: str, recognized: str) -> str:
    state, found, confidence = qa_review.evaluate(expected, recognized)
    return qa_review.auto_accept_reason(state, found, confidence, expected)


@pytest.mark.parametrize("spoken", [
    "El el camino era largo y el sol brillaba.",      # a word said twice
    "El camino camino era largo y el sol brillaba.",
    "El ca camino era largo y el sol brillaba.",      # a broken-off syllable
])
def test_a_repeated_word_or_syllable_is_a_suspected_stutter(spoken) -> None:
    state, found, _confidence = qa_review.evaluate("El camino era largo y el sol brillaba.", spoken)
    assert state == "likely_error"
    assert any(issue.get("suspected_stutter") for issue in found)
    # Never let through: a listener hears it at once.
    assert accept_reason("El camino era largo y el sol brillaba.", spoken) == ""


def test_an_added_function_word_that_is_not_a_repeat_is_still_minor() -> None:
    found = issues("Fue a casa de su madre.", "Fue a la casa de su madre.")
    assert not any(issue.get("suspected_stutter") for issue in found)


def test_ambiguous_differences_are_accepted_without_a_person() -> None:
    assert verdict("Buenos días.", "Buenas días.") == "ambiguous"
    assert accept_reason("Buenos días.", "Buenas días.")


def test_scattered_one_word_slips_in_a_long_paragraph_are_accepted() -> None:
    heard = LONG.replace("miró", "vio").replace("viejo", "bello")
    state, _found, confidence = qa_review.evaluate(LONG, heard)
    assert state == "likely_error" and confidence >= 0.95
    assert "coincide" in accept_reason(LONG, heard)


@pytest.mark.parametrize("heard", [
    # A whole phrase missing: the narration dropped it.
    LONG.replace(" donde los pájaros cantaban entre las ramas mojadas del viejo almendro,", ""),
    # A wrong quantity is never minor.
    LONG.replace("aquel domingo", "aquel domingo 14").replace("despacio", "despacio"),
])
def test_a_missing_phrase_or_a_wrong_number_still_needs_attention(heard) -> None:
    assert accept_reason(LONG, heard) == ""


def test_short_passages_get_no_proportional_allowance() -> None:
    # Two words out of six is a third of the sentence, not a slip.
    assert accept_reason("El perro corría por el parque.", "El gato corría por el bosque.") == ""


def test_an_acoustic_defect_is_never_accepted_automatically() -> None:
    found = [qa_review.audio_issue("Hola.", "Hola.", "{}")]
    assert qa_review.auto_accept_reason("likely_error", found, 1.0, LONG) == ""


@pytest.mark.parametrize("text,language", [
    ("Before we dive into some Spanish, I want to congratulate you on your journey.", "en"),
    ("Amanecía despacio sobre el río cuando Marta abrió la ventana.", "es"),
    ("Translation of the Story: the mysterious package de la tía que vivía en el pueblo", "multi"),
    ("Gracias.", "es"),
    ("CHAPTER 1", "es"),
])
def test_speech_review_listens_for_the_language_the_passage_is_in(text, language) -> None:
    from app.text_language import recognition_language
    assert recognition_language(text) == language


def test_something_added_after_the_last_word_is_not_a_minor_slip() -> None:
    # The voice trailed off into an extra word: heard "... antes de que llegara una heno".
    heard = LONG.rstrip(".") + " una heno."
    state, found, _confidence = qa_review.evaluate(LONG, heard)
    assert state == "likely_error"
    assert any(issue.get("trailing_addition") for issue in found)
    assert accept_reason(LONG, heard) == ""


# --------------------------------------------------------------------------- #
# ONCE's own wording, measured on the first real ONCE-structured book
# --------------------------------------------------------------------------- #
#
# Every ONCE audiobook carries the legal notice and the production line. On the
# first real run the voice read them correctly and the comparison still sent
# the production line to a person after two paid retakes.

@pytest.mark.parametrize("source,heard", [
    # The acronym ONCE is said like the number eleven; Deepgram writes "11".
    # "y ha sido" after it used to parse as "11 hectares".
    ("la Organización Nacional de Ciegos Españoles (ONCE) y ha sido realizada",
     "la Organización Nacional de Ciegos Españoles, 11, y ha sido realizada"),
    ("Llegaron once y ha empezado a llover", "Llegaron 11 y ha empezado a llover"),
    # "S.L." read as "Sociedad Limitada"; "S-2" written "S2" or "S 2".
    ("para el Servicio Bibliográfico de la ONCE, Sonidos S-2 Productions, S.L. octubre de 2026",
     "para el servicio bibliográfico de la 11. Sonidos S 2 productions, sociedad limitada. Octubre de 2026."),
    ("Sonidos S-2 Productions, S.L. octubre de 2026", "Sonidos S2 Productions, Sociedad Limitada, octubre de 2026"),
    ("Editorial Planeta, S.A.", "Editorial Planeta, sociedad anónima"),
    ("Grabaciones Norte, S.L.U.", "Grabaciones Norte, sociedad limitada unipersonal"),
])
def test_once_wording_read_correctly_is_not_an_error(source, heard) -> None:
    assert verdict(source, heard) == "verified", issues(source, heard)


@pytest.mark.parametrize("source,heard", [
    ("Sonidos S-2 Productions, S.L.", "Sonidos S 3 productions, sociedad limitada"),
    ("Editorial Planeta, S.A.", "Editorial Planeta, sociedad limitada"),
    ("caminó a 2 metros de la casa", "caminó a 3 metros de la casa"),
])
def test_a_wrong_code_or_company_form_is_still_an_error(source, heard) -> None:
    assert verdict(source, heard) == "likely_error"


def test_a_number_does_not_swallow_the_y_that_follows_it() -> None:
    from app.spanish_normalization import comparison_tokens

    assert [token.value for token in comparison_tokens("once y ha sido")] == ["num:11", "y", "ha", "sido"]
    assert [token.value for token in comparison_tokens("treinta y dos")] == ["num:32"]
    # A unit after a number is still a unit.
    assert [token.value for token in comparison_tokens("cuatro hectáreas")] == ["measure:4:ha"]
