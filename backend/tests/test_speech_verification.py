"""Speech verification against real recognizer output, metrics, timing and diagnosis.

The transcripts below are what Deepgram nova-3 and ElevenLabs Scribe actually
returned for ElevenLabs narrations during the 2026-10-25 benchmark (see
docs/quality-system.md). They cover both directions: correct speech that a
naive comparison would flag, and real narration errors that must stay caught.
"""

from __future__ import annotations

import pytest

from app import qa_metrics, qa_review, repair
from app.spanish_text_normalizer import normalize_for_speech

NUMBERS = ("El 12/03/2024, a las 18:30 h, el Sr. García pagó 3,50 € por el billete. El tren recorrió 245 km "
           "a 120 km/h y el precio subió un 2,5 %. En la pág. 23 se cita a Felipe II y al s. XVI; asistieron "
           "1.200 personas.")


def verdict(source: str, transcript: str) -> str:
    return qa_review.evaluate_against(normalize_for_speech(source).spoken, source, transcript)[0]


@pytest.mark.parametrize("transcript", [
    # Deepgram writes digits for what was said in words.
    "El 12 de marzo de 2024, a las 18 30 horas, el señor García pagó 3 coma 50 euros por el billete. El tren "
    "recorrió 245 kilómetros a 120 kilómetros por hora, y el precio subió un 2 coma 5 por 100. En la página 23 "
    "se cita a Felipe 2º y al siglo 16. Asistieron 1200 personas.",
    # Scribe writes the symbols back.
    "El 12 de marzo de 2024, a las 18:30 horas, el señor García pagó 3,50 euros por el billete. El tren "
    "recorrió 245 kilómetros a 120 kilómetros por hora y el precio subió un 2,5 %. En la página 23 se cita a "
    "Felipe II y al siglo XVI. Asistieron 1200 personas.",
    # eleven_v4 reading the raw text its own way, all correct Spanish.
    "El 12 de marzo de 2024, a las 18:30 horas, el señor García pagó tres euros con cincuenta céntimos por el "
    "billete. El tren recorrió doscientos cuarenta y cinco kilómetros a ciento veinte kilómetros por hora y el "
    "precio subió un 2,5 %. En la página veintitrés se cita a Felipe II y al siglo XVI. Asistieron 1200 personas",
])
def test_correct_narrations_of_numbers_dates_and_abbreviations_are_verified(transcript) -> None:
    assert verdict(NUMBERS, transcript) == "verified"


@pytest.mark.parametrize("transcript,expected_word", [
    # eleven_multilingual_v2 on the raw text: an English "zero" and a garbled century.
    ("El 12 Zero 3 2024, a las 18 30 h, el señor García pagó 3 coma 50 por el billete. El tren recorrió 245 un "
     "kilometros a 120 kilometro, y el precio subió un 2 coma 5 por 100. En la página 23 se cita a Felipe II y "
     "al Sebati Seasing, asistieron 1200 personas.", "XVI"),
    # eleven_flash_v2_5 on the raw text: a wrong amount and nonsense units.
    ("El 12/03/2024 a las 18:30, el señor García pagó 350 € por el billete. El tren recorrió 245 km a 120 km/h "
     "y el precio subió un 2,5 %. En la página 23 se cita a Felipe II y al S XVI asistieron 1200 personas",
     "3,50"),
])
def test_real_misreadings_of_numbers_stay_errors(transcript, expected_word) -> None:
    result, issues, _confidence, _reference = qa_review.evaluate_against(
        normalize_for_speech(NUMBERS).spoken, NUMBERS, transcript)
    assert result == "likely_error"
    assert issues


def test_names_spelled_differently_but_pronounced_the_same_are_not_errors() -> None:
    # Both recognizers wrote "Jimena" for "Ximena" and Scribe "Yanes" for
    # "Llanes": the same sounds in Peninsular Spanish (yeísmo).
    source = "Se presentó como Ximena Zubizarreta y traía galletas de Llanes."
    result, issues, _c, _r = qa_review.evaluate_against(source, source,
                                                        "Se presentó como Jimena Zubizarreta y traía galletas de Yanes.")
    assert result == "ambiguous"
    assert all(issue.get("phonetic_variant") for issue in issues)
    assert qa_review.phonetic_key("caza") != qa_review.phonetic_key("casa")  # Spain keeps θ and s apart
    assert verdict("Lucía Ferrández llegó.", "Lucía Fernández llegó.") != "verified"


def test_word_error_rate_counts_substitutions_deletions_and_insertions_exactly() -> None:
    metrics = qa_metrics.error_rates("el faro estaba muy lejos del pueblo", "el faro estaba lejos del puerto ayer")
    assert (metrics["substitutions"], metrics["deletions"], metrics["insertions"]) == (1, 1, 1)
    assert metrics["reference_words"] == 7 and metrics["wer"] == round(3 / 7, 4)
    # Numbers are compared by value, so the spoken form of 147 is no error.
    assert qa_metrics.error_rates("tenía 147 ventanas", "tenía ciento cuarenta y siete ventanas")["wer"] == 0.0
    total = qa_metrics.aggregate([metrics, {"reference_words": 3, "substitutions": 0, "deletions": 0,
                                            "insertions": 0}])
    assert total["reference_words"] == 10 and total["wer"] == 0.3 and total["segments_with_errors"] == 1


def test_a_stretched_word_reveals_a_stumble_a_fluent_recognizer_smoothed_over() -> None:
    # Scribe's timing for a narration Deepgram heard as "echó antana en
    # Navandar hacia" while Scribe wrote the expected "echó a andar hacia".
    segments = [{"text": "echó", "start": 14.64, "end": 14.88}, {"text": "a", "start": 14.90, "end": 14.92},
                {"text": "andar", "start": 14.94, "end": 16.24}, {"text": "hacia", "start": 16.28, "end": 16.48},
                {"text": "2024", "start": 17.0, "end": 18.3}]
    anomalies = repair.timing_anomalies(segments, "x" * 200, 18.2)
    assert [item["word"] for item in anomalies] == ["andar"]  # "2024" is not judged by its spelling


def _evaluation(engine, transcript, source, *, anomalies=()):
    result, issues, confidence, reference = qa_review.evaluate_against(source, source, transcript)
    return repair.Evaluation(engine, transcript, result, issues, confidence, reference,
                             {"timing_anomalies": list(anomalies)})


SOURCE = "Inés guardó la llave en el bolsillo del abrigo y echó a andar hacia la estación sin mirar atrás."
STUMBLE = "Inés guardó la llave en el bolsillo del abrigo y echó antana en Navandar hacia la estación sin mirar atrás."


def test_diagnosis_accepts_what_a_second_recognizer_confirms() -> None:
    primary = _evaluation("deepgram", STUMBLE, SOURCE)
    secondary = _evaluation("scribe", SOURCE, SOURCE)
    diagnosis = repair.diagnose(primary, secondary=secondary, retakes_left=2, expected_text=SOURCE)
    assert (diagnosis.origin, diagnosis.action) == (repair.ORIGIN_ASR, repair.ACTION_ACCEPT_FLAGGED)


def test_diagnosis_retakes_when_the_confirming_recognizer_shows_a_stretched_word() -> None:
    primary = _evaluation("deepgram", STUMBLE, SOURCE)
    secondary = _evaluation("scribe", SOURCE, SOURCE, anomalies=[
        {"word": "andar", "start": 14.94, "end": 16.24, "seconds": 1.3, "expected_seconds": 0.45, "ratio": 2.9}])
    diagnosis = repair.diagnose(primary, secondary=secondary, retakes_left=2, expected_text=SOURCE)
    assert (diagnosis.origin, diagnosis.action, diagnosis.severity) == (
        repair.ORIGIN_SYNTHESIS, repair.ACTION_RETAKE, repair.SEVERITY_CRITICAL)
    assert diagnosis.corroborated


def test_diagnosis_retakes_a_difference_both_recognizers_hear_and_escalates_when_out_of_takes() -> None:
    wrong = "Inés guardó la llave en el bolsillo y echó a correr hacia el puerto."
    primary, secondary = _evaluation("deepgram", wrong, SOURCE), _evaluation("scribe", wrong, SOURCE)
    assert repair.diagnose(primary, secondary=secondary, retakes_left=1, expected_text=SOURCE).action == \
        repair.ACTION_RETAKE
    last = repair.diagnose(primary, secondary=secondary, retakes_left=0, expected_text=SOURCE)
    assert last.action == repair.ACTION_HUMAN and last.corroborated


def test_diagnosis_does_not_pay_for_retakes_of_a_name_both_recognizers_spell_differently() -> None:
    source = "Se presentó como Ximena Zubizarreta."
    heard = "Se presentó como Jimena Zubizarreta."
    diagnosis = repair.diagnose(_evaluation("deepgram", heard, source), secondary=_evaluation("scribe", heard, source),
                                retakes_left=2, expected_text=source)
    assert diagnosis.action == repair.ACTION_ACCEPT_FLAGGED and diagnosis.corroborated


def test_a_difference_on_doubtful_source_text_goes_to_a_person_not_to_a_retake() -> None:
    source = "El faro estaba a 3 km de l0s acantilados del norte de la isla."
    primary = repair.Evaluation("deepgram", "El faro estaba a 3 km de los acantilados del norte de la isla.",
                                *qa_review.evaluate_against(source, source,
                                                            "El faro estaba a 3 km de los acantilados del norte de la isla.")[:3],
                                reference="source")
    start = source.index("l0s")
    diagnosis = repair.diagnose(primary, source_flags=[{"start": start, "end": start + 3}], retakes_left=2,
                                expected_text=source)
    assert (diagnosis.origin, diagnosis.action) == (repair.ORIGIN_SOURCE, repair.ACTION_REVIEW_SOURCE)


def test_a_difference_on_normalized_text_gets_one_retake_then_a_person() -> None:
    source = "Llegó el 12/03/2024 al puerto con todos sus hermanos y sus primos."
    heard = "Llegó el trece de marzo de dos mil veinticuatro al puerto con todos sus hermanos y sus primos."
    result, issues, confidence, reference = qa_review.evaluate_against(source, source, heard)
    primary = repair.Evaluation("deepgram", heard, result, issues, confidence, "source")
    changes = [{"start": source.index("12/03/2024"), "end": source.index("12/03/2024") + 10}]
    first = repair.diagnose(primary, changes=changes, retakes_left=2, expected_text=source)
    assert (first.origin, first.action) == (repair.ORIGIN_NORMALIZATION, repair.ACTION_RETAKE)
    second = repair.diagnose(primary, changes=changes, retakes_left=1, normalization_retakes=1, expected_text=source)
    assert second.action == repair.ACTION_REVIEW_NORMALIZATION


def test_defective_audio_is_retaken_whatever_the_recognizer_says() -> None:
    primary = _evaluation("deepgram", SOURCE, SOURCE)
    failed = repair.diagnose(primary, integrity={"status": "failed", "reasons": ["corrupt_frames"]}, retakes_left=1)
    assert (failed.origin, failed.action) == (repair.ORIGIN_AUDIO, repair.ACTION_RETAKE)
    cut = repair.diagnose(primary, integrity={"status": "needs_review", "reasons": ["abrupt_end"]}, retakes_left=1)
    assert cut.action == repair.ACTION_RETAKE and cut.severity == repair.SEVERITY_CRITICAL
    assert repair.diagnose(primary, retakes_left=1).action == repair.ACTION_ACCEPT
