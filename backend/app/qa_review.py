"""Turn canonical text/ASR differences into an honest review decision.

The Spanish normalizer in :mod:`app.spanish_normalization` already removes
differences that are only spoken-vs-written forms, so "147" and "ciento
cuarenta y siete" never reach this module as a difference. What arrives here is
a real divergence, and the job of this module is to decide whether it is:

``verified``
    No divergence survived normalization.
``ambiguous``
    A small divergence of a kind automatic speech recognition gets wrong on its
    own. Accepted automatically and recorded as such, never retaken.
``likely_error``
    A divergence worth spending a retake on, including every wrong quantity and
    every repeated word or syllable (a stutter).

A wrong number is always a real error: numeric tokens are compared by value, so
"147" against "ciento cuarenta y ocho" stays a mismatch and is never treated as
ambiguous.

:func:`auto_accept_reason` decides what needs no person. Narration engines fail
by stuttering, dropping or garbling whole phrases, or reading a number wrong;
a few scattered one-word differences across a long paragraph are what the
recognizer gets wrong by itself, and sending each of those to a reviewer made
the queue too long to be useful.
"""

from __future__ import annotations

import difflib
import unicodedata

from app.spanish_normalization import canonical_tokens, compare_transcripts

# Short Spanish function words that recognizers routinely drop or insert without
# changing what a listener understands. Their presence alone is not an error.
_FUNCTION_WORDS = {
    "a", "al", "ante", "con", "contra", "de", "del", "desde", "e", "el", "en", "entre", "hacia",
    "hasta", "la", "las", "lo", "los", "mas", "me", "mi", "ni", "o", "para", "por", "pues", "que",
    "se", "si", "sin", "sobre", "su", "sus", "te", "tras", "tu", "un", "una", "unas", "unos", "u",
    "y", "ya", "le", "les", "nos", "es", "ha", "he",
}
# Canonical token prefixes the normalizer uses for values compared numerically.
_VALUE_PREFIXES = ("num:", "measure:", "time:", "date:", "ord:")

CATEGORY_AMBIGUOUS = "ambiguous_expression"
CATEGORY_MISMATCH = "likely_mismatch"
CATEGORY_AUDIO = "audio_defect"

# Prefix of the note stored on a passage accepted without a person, which the
# quality report uses to show it as its own outcome.
AUTO_ACCEPT_PREFIX = "Aceptada automáticamente"
# A difference in a long passage is accepted when this much of it matched and
# no single difference is longer than this many words.
_LONG_PASSAGE_WORDS = 12
_LONG_PASSAGE_MIN_MATCH = 0.95
_LONG_PASSAGE_MAX_SPAN = 2


def _plain(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in folded if not unicodedata.combining(character))


def _words(value: str) -> list[str]:
    return [word for word in _plain(value).replace("’", "'").split() if word]


def _is_value_difference(expected: str, recognized: str) -> bool:
    """True when either side canonicalizes to a number, time, date or measure."""
    left, right = canonical_tokens(expected, recognized)
    return any(token.value.startswith(_VALUE_PREFIXES) for token in (*left, *right))


def _near_homophone(expected: str, recognized: str) -> bool:
    """Short, very similar words are usually a recognizer slip, not a misreading."""
    if not expected or not recognized:
        return False
    ratio = difflib.SequenceMatcher(a=_plain(expected), b=_plain(recognized), autojunk=False).ratio()
    return ratio >= 0.82


def _repeats_neighbour(issue: dict[str, str], recognized_text: str) -> bool:
    """True when extra words repeat what is said next to them: a stutter.

    "el el camino" or "ca camino": the recognizer heard a word, or the broken
    start of one, twice. That is a narration defect, not a recognizer slip.
    """
    inserted = _words(issue.get("recognized", ""))
    start, end = issue.get("recognized_start"), issue.get("recognized_end")
    if not inserted or not recognized_text or not isinstance(start, int) or not isinstance(end, int):
        return False
    before = _words(recognized_text[:start])[-len(inserted):]
    after = _words(recognized_text[end:])[:len(inserted)]
    if inserted in (before, after):
        return True
    fragment = inserted[0]
    following = after[:1]
    return (len(inserted) == 1 and len(fragment) >= 2 and fragment not in _FUNCTION_WORDS
            and bool(following) and following[0] != fragment and following[0].startswith(fragment))


def classify_issue(issue: dict[str, str], expected_word_count: int, recognized_text: str = "") -> dict[str, str]:
    """Annotate one difference with a difference type and a review category."""
    kind = issue.get("kind", "")
    expected = issue.get("expected", "")
    recognized = issue.get("recognized", "")
    difference_type = {
        "delete": "omission",
        "insert": "addition_or_repetition",
        "replace": "substitution",
    }.get(kind, "difference")

    expected_words = _words(expected)
    recognized_words = _words(recognized)
    span = max(len(expected_words), len(recognized_words))

    both_function_words = (
        span == 1 and kind == "replace"
        and bool(expected_words) and bool(recognized_words)
        and set(expected_words) <= _FUNCTION_WORDS and set(recognized_words) <= _FUNCTION_WORDS
    )
    end = issue.get("recognized_end")
    if (kind == "insert" and isinstance(end, int) and recognized_text
            and not recognized_text[end:].strip(" .,;:!?¡¿»”\"'…")):
        # Words after the last word of the text: a voice that mumbles or
        # invents something as it trails off. Heard: "... llegara una heno".
        return {**issue, "difference_type": "addition_or_repetition", "category": CATEGORY_MISMATCH,
                "reason": "Se oye algo añadido al final del párrafo.", "trailing_addition": True}
    if kind == "insert" and _repeats_neighbour(issue, recognized_text):
        category = CATEGORY_MISMATCH
        reason = "Posible tartamudeo: se repite una palabra o una sílaba."
        return {**issue, "difference_type": "repetition", "category": category, "reason": reason,
                "suspected_stutter": True}
    if both_function_words:
        # One short function word swapped for another, such as "el" for "en" or
        # "la" for "una". Checked before the quantity rule because "un" and
        # "una" are also the numeral one, which made an article swap look like a
        # wrong quantity.
        category = CATEGORY_AMBIGUOUS
        reason = "Dos palabras funcionales cortas intercambiadas; suele ser un fallo del reconocedor."
    elif _is_value_difference(expected, recognized):
        # Quantities, times, dates and units are compared by value. A surviving
        # difference means the spoken value or unit is genuinely wrong.
        category = CATEGORY_MISMATCH
        reason = "La cantidad, hora, fecha o unidad pronunciada no coincide con el texto."
    elif span == 1 and kind in {"delete", "insert"} and set(expected_words or recognized_words) <= _FUNCTION_WORDS:
        category = CATEGORY_AMBIGUOUS
        reason = "Palabra funcional corta; el reconocedor automático suele añadirla u omitirla."
    elif span == 1 and kind == "replace" and _near_homophone(expected, recognized):
        category = CATEGORY_AMBIGUOUS
        reason = "Palabras casi homófonas; puede ser un fallo del reconocedor y no de la locución."
    elif span <= 2 and expected_word_count <= 4:
        category = CATEGORY_AMBIGUOUS
        reason = "Frase muy corta: el reconocimiento automático es poco fiable en este caso."
    else:
        category = CATEGORY_MISMATCH
        reason = "Diferencia amplia respecto al texto revisado."

    return {**issue, "difference_type": difference_type, "category": category, "reason": reason}


def evaluate(expected: str, recognized: str) -> tuple[str, list[dict[str, str]], float]:
    """Return ``(verdict, classified issues, confidence)`` for one sentence.

    ``verdict`` is ``verified``, ``ambiguous`` or ``likely_error``.
    ``confidence`` is the share of the sentence that matched after
    normalization: 1.0 when nothing differed, lower as more of it diverged.
    """
    issues = compare_transcripts(expected, recognized)
    source_tokens, _ = canonical_tokens(expected, recognized)
    expected_count = max(1, len(source_tokens))
    if not issues:
        return "verified", [], 1.0

    classified = [classify_issue(issue, expected_count, recognized) for issue in issues]
    diverged = sum(
        max(len(_words(issue.get("expected", ""))), len(_words(issue.get("recognized", ""))))
        for issue in classified
    )
    confidence = max(0.0, min(1.0, 1.0 - diverged / expected_count))

    if all(issue["category"] == CATEGORY_AMBIGUOUS for issue in classified) and confidence >= 0.5:
        # Small, recognizer-prone divergence: a person decides, and a retake is
        # not worth another paid generation.
        return "ambiguous", classified, confidence
    return "likely_error", classified, confidence


def auto_accept_reason(verdict: str, issues: list[dict[str, str]], confidence: float, expected: str) -> str:
    """Why a difference can be accepted without a person, or ``""`` if one should look.

    Never for an acoustic defect, a suspected stutter, a wrong quantity or a
    longer gap; those are what a listener would notice.
    """
    if verdict == "verified" or not issues:
        return ""
    if any(issue.get("category") == CATEGORY_AUDIO or issue.get("suspected_stutter")
           or issue.get("trailing_addition") for issue in issues):
        return ""
    if verdict == "ambiguous":
        return "diferencias menores que el reconocimiento automático suele cometer (palabras cortas o casi iguales)"
    if len(_words(expected)) < _LONG_PASSAGE_WORDS or confidence < _LONG_PASSAGE_MIN_MATCH:
        return ""
    for issue in issues:
        left, right = issue.get("expected", ""), issue.get("recognized", "")
        if max(len(_words(left)), len(_words(right))) > _LONG_PASSAGE_MAX_SPAN:
            return ""
        if _is_value_difference(left, right):
            return ""
    return (f"diferencias sueltas de una o dos palabras en un párrafo largo; "
            f"coincide el {confidence:.0%} del texto")


def audio_issue(expected: str, recognized: str, detail: str) -> dict[str, str]:
    """A difference entry describing an acoustic defect rather than a misreading."""
    return {
        "kind": "acoustic",
        "category": CATEGORY_AUDIO,
        "difference_type": "audio_defect",
        "expected": expected,
        "recognized": recognized,
        "detail": detail,
        "reason": "El análisis acústico detectó un posible defecto en el audio generado.",
    }
