"""Decide what a suspected error is, where it most likely comes from, and what to do.

A difference between the expected speech and a transcript has several possible
causes, and the right response differs for each:

``synthesis``      the voice dropped, repeated, garbled or misread words
                   → regenerate this segment (a fresh take)
``audio``          the file itself is defective (corrupt, silent, clipped, cut off)
                   → regenerate
``normalization``  the difference sits on text the normalizer rewrote
                   ("12/03" → "doce de marzo"); regenerating will say the same
                   words again → one retake at most, then a person
``source``         the difference sits on text extraction already doubted
                   (low-confidence OCR, a broken word) → a person checks the
                   source; regenerating cannot fix the text
``asr``            a second, independent recognizer heard the expected words
                   → accepted, recorded as disputed, and listed for listening

Regenerating is only worthwhile for synthesis and audio problems; spending
paid takes on recognizer or source problems changes nothing. A segment that is
not resolved within the retake budget goes to a person with this diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app import qa_review

ORIGIN_SYNTHESIS = "synthesis"
ORIGIN_AUDIO = "audio"
ORIGIN_NORMALIZATION = "normalization"
ORIGIN_SOURCE = "source"
ORIGIN_ASR = "asr"
ORIGIN_NONE = "none"

ACTION_ACCEPT = "accept"
ACTION_ACCEPT_FLAGGED = "accept_flagged"   # accepted, but on the listening list
ACTION_RETAKE = "retake"
ACTION_REVIEW_SOURCE = "review_source"
ACTION_REVIEW_NORMALIZATION = "review_normalization"
ACTION_HUMAN = "human_review"

SEVERITY_CRITICAL = "critical"   # a listener would get the wrong content
SEVERITY_MAJOR = "major"         # audible defect, content probably right
SEVERITY_MINOR = "minor"         # recognizer-level noise
SEVERITY_NONE = "none"

RECOMMENDATIONS = {
    ACTION_ACCEPT: "Nada que hacer.",
    ACTION_ACCEPT_FLAGGED: "Aceptada; escúchala en la revisión por muestreo.",
    ACTION_RETAKE: "Volver a narrar este párrafo.",
    ACTION_REVIEW_SOURCE: "Comprueba el texto original (OCR o extracción) y corrígelo si hace falta.",
    ACTION_REVIEW_NORMALIZATION: "Comprueba cómo se lee la expresión normalizada; añade una entrada al "
                                 "diccionario de pronunciación o corrige el texto.",
    ACTION_HUMAN: "Escucha el párrafo y decide: aceptar, volver a narrar o corregir el texto.",
}


@dataclass
class Evaluation:
    """One recognizer's comparison against the expected text."""

    engine: str
    transcript: str
    verdict: str
    issues: list[dict]
    confidence: float
    reference: str = "spoken"
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"engine": self.engine, "transcript": self.transcript, "verdict": self.verdict,
                "issues": self.issues, "confidence": self.confidence, "reference": self.reference,
                "metrics": self.metrics}


@dataclass
class Diagnosis:
    origin: str
    severity: str
    action: str
    reasons: list[str] = field(default_factory=list)
    corroborated: bool = False
    spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def recommendation(self) -> str:
        return RECOMMENDATIONS[self.action]

    def as_dict(self) -> dict:
        return {"origin": self.origin, "severity": self.severity, "action": self.action,
                "reasons": self.reasons, "corroborated": self.corroborated,
                "recommendation": self.recommendation, "spans": [list(span) for span in self.spans]}


def timing_anomalies(segments: list[dict], spoken_text: str, speech_seconds: float | None,
                     *, ratio: float = 2.5, minimum: float = 1.0, min_letters: int = 4) -> list[dict]:
    """Words that take far longer than the speaker's own pace allows.

    Recognizers that write fluent text can "correct" a narration that
    stumbled: measured on ElevenLabs, a reading Deepgram heard as "echó antana
    en Navandar hacia" was transcribed by Scribe as the expected "echó a andar
    hacia", but Scribe's own timing gave "andar" 1.3 s where this voice says a
    five-letter word in about 0.45 s. The extra second is the stumble. A word
    lasting ``ratio`` times its expected length, and at least ``minimum``
    seconds, is reported.
    """
    characters = len("".join(spoken_text.split()))
    if not segments or not speech_seconds or speech_seconds <= 0 or characters < 20:
        return []
    rate = characters / speech_seconds
    anomalies = []
    for segment in segments:
        word = str(segment.get("text", "")).strip(" .,;:!?¡¿«»\"'()—–-")
        try:
            duration = float(segment["end"]) - float(segment["start"])
        except (KeyError, TypeError, ValueError):
            continue
        # Only ordinary words: the spoken length of "2024", "XVI" or "ONU"
        # cannot be read from how they are written.
        if not word or not word.isalpha() or (word.isupper() and len(word) > 1):
            continue
        # Short words before a dialogue break ("¡No!", "amo") are drawn out on
        # purpose by an expressive narrator; measured in the long-form run.
        if len(word) < min_letters:
            continue
        expected = max(0.15, len(word) / rate)
        if duration >= minimum and duration > ratio * expected:
            anomalies.append({"word": word, "start": round(float(segment["start"]), 3),
                              "end": round(float(segment["end"]), 3), "seconds": round(duration, 3),
                              "expected_seconds": round(expected, 3), "ratio": round(duration / expected, 2)})
    return anomalies


def _issue_words(issues: list[dict]) -> set[str]:
    return {word.strip(".,;:!?¡¿«»\"'()").casefold() for issue in issues
            for word in str(issue.get("expected", "")).split() if word.strip(".,;:!?¡¿«»\"'()")}


def _spans(issues: list[dict]) -> list[tuple[int, int]]:
    spans = []
    for issue in issues:
        start, end = issue.get("expected_start"), issue.get("expected_end")
        if isinstance(start, int) and isinstance(end, int):
            spans.append((start, max(end, start + 1)))
    return spans


def _overlaps(span: tuple[int, int], others: list[tuple[int, int]], slack: int = 2) -> bool:
    return any(span[0] - slack < end and start < span[1] + slack for start, end in others)


def _severity(issues: list[dict]) -> str:
    """The worst thing the differences could mean for a listener."""
    if not issues:
        return SEVERITY_NONE
    for issue in issues:
        if issue.get("category") == qa_review.CATEGORY_AUDIO:
            return SEVERITY_MAJOR if "abrupt" not in str(issue.get("detail", "")) else SEVERITY_CRITICAL
        expected_words = len(str(issue.get("expected", "")).split())
        recognized_words = len(str(issue.get("recognized", "")).split())
        if (issue.get("suspected_stutter") or issue.get("trailing_addition")
                or "cantidad" in str(issue.get("reason", "")) or max(expected_words, recognized_words) >= 3):
            return SEVERITY_CRITICAL
    if all(issue.get("category") == qa_review.CATEGORY_AMBIGUOUS for issue in issues):
        return SEVERITY_MINOR
    return SEVERITY_MAJOR


def diagnose(
    primary: Evaluation,
    *,
    secondary: Evaluation | None = None,
    integrity: dict | None = None,
    changes: list[dict] | None = None,
    source_flags: list[dict] | None = None,
    expected_text: str = "",
    retakes_left: int = 0,
    normalization_retakes: int = 0,
) -> Diagnosis:
    """Classify one take.

    ``changes`` are the normalizer's recorded rewrites and ``source_flags``
    the extraction doubts, both as ``{"start": …, "end": …}`` offsets into the
    *source* text; issue offsets refer to whichever reference matched best, so
    overlap is only meaningful when that reference is the source text, and is
    otherwise judged by the words involved.
    """
    integrity = integrity or {}
    if integrity.get("status") == "failed":
        return Diagnosis(ORIGIN_AUDIO, SEVERITY_CRITICAL, ACTION_RETAKE if retakes_left else ACTION_HUMAN,
                         [f"Audio técnicamente defectuoso: {', '.join(integrity.get('reasons') or [])}."])
    audio_problem = integrity.get("status") == "needs_review"
    if primary.verdict == "verified" and not audio_problem:
        stretched = primary.metrics.get("timing_anomalies") or []
        if stretched:
            # The words are right, but one of them lasts far too long: worth an ear.
            return Diagnosis(ORIGIN_SYNTHESIS, SEVERITY_MINOR, ACTION_ACCEPT_FLAGGED,
                             [f"Texto correcto, pero «{stretched[0]['word']}» dura {stretched[0]['seconds']:.2f} s "
                              f"(lo normal en esta voz: {stretched[0]['expected_seconds']:.2f} s)."])
        return Diagnosis(ORIGIN_NONE, SEVERITY_NONE, ACTION_ACCEPT)
    if primary.verdict == "verified" and audio_problem:
        reasons = integrity.get("reasons") or []
        severity = SEVERITY_CRITICAL if {"abrupt_end", "truncated_final_frame",
                                         "speech_too_fast_for_text"} & set(reasons) else SEVERITY_MAJOR
        return Diagnosis(ORIGIN_AUDIO, severity, ACTION_RETAKE if retakes_left else ACTION_HUMAN,
                         [f"El texto se reconoce, pero el audio parece defectuoso: {', '.join(reasons)}."])

    severity = _severity(primary.issues)
    spans = _spans(primary.issues)
    reasons: list[str] = []
    # A second recognizer that heard the text cannot overrule the first where
    # its own timing shows the audio stumbled.
    stretched = [anomaly for anomaly in (secondary.metrics.get("timing_anomalies") or [] if secondary else [])
                 if anomaly["word"].casefold() in _issue_words(primary.issues)]
    if stretched:
        reasons.append("Las marcas de tiempo muestran una palabra alargada donde el primer reconocedor oyó algo "
                       f"distinto: «{stretched[0]['word']}» dura {stretched[0]['seconds']:.2f} s "
                       f"(lo normal en esta voz: {stretched[0]['expected_seconds']:.2f} s).")
        severity = SEVERITY_CRITICAL
    if secondary is not None and not stretched:
        if secondary.verdict == "verified":
            # Two recognizers disagree and the independent one heard the text.
            return Diagnosis(ORIGIN_ASR, SEVERITY_MINOR, ACTION_ACCEPT_FLAGGED,
                             [f"{primary.engine} oyó diferencias que {secondary.engine} no confirma."],
                             spans=spans)
        corroborated = primary.reference == secondary.reference and any(
            _overlaps(span, _spans(secondary.issues)) for span in spans)
        if not corroborated and secondary.verdict == "ambiguous" and severity != SEVERITY_CRITICAL:
            return Diagnosis(ORIGIN_ASR, SEVERITY_MINOR, ACTION_ACCEPT_FLAGGED,
                             [f"{secondary.engine} solo encuentra diferencias menores en otros puntos."],
                             spans=spans)
        if corroborated:
            reasons.append(f"{primary.engine} y {secondary.engine} oyen la misma diferencia.")
            if severity == SEVERITY_MINOR and primary.verdict == "ambiguous" and secondary.verdict == "ambiguous":
                # Both recognizers agree on a near-identical word. For a name
                # that is usually a spelling of the same sound ("Ximena" and
                # "Jimena" are both /xiˈmena/; "Llanes" heard as "Yanes" is
                # yeísmo), so a retake would say it the same way again.
                return Diagnosis(ORIGIN_ASR, SEVERITY_MINOR, ACTION_ACCEPT_FLAGGED,
                                 reasons + ["Diferencia menor que ambos reconocedores escriben igual (a menudo un "
                                            "nombre que suena igual escrito de otra forma); escúchala."],
                                 True, spans)
    else:
        corroborated = bool(stretched)

    accepted = qa_review.auto_accept_reason(primary.verdict, primary.issues, primary.confidence, expected_text)
    if accepted and not corroborated and not audio_problem:
        return Diagnosis(ORIGIN_ASR, SEVERITY_MINOR, ACTION_ACCEPT_FLAGGED, [accepted], spans=spans)

    # Offsets are into the source text only when the source was the reference.
    on_source = primary.reference == "source"
    flagged = [(item["start"], item["end"]) for item in (source_flags or []) if "start" in item]
    if on_source and flagged and spans and all(_overlaps(span, flagged) for span in spans):
        return Diagnosis(ORIGIN_SOURCE, severity, ACTION_REVIEW_SOURCE,
                         reasons + ["La diferencia está en texto que la extracción ya marcó como dudoso."],
                         corroborated, spans)
    rewritten = [(item["start"], item["end"]) for item in (changes or []) if "start" in item]
    touches_rewrite = bool(rewritten) and (
        (on_source and spans and all(_overlaps(span, rewritten) for span in spans))
        or (not on_source and _issue_words_in_changes(primary.issues, changes or [])))
    if touches_rewrite:
        if normalization_retakes < 1 and retakes_left:
            return Diagnosis(ORIGIN_NORMALIZATION, severity, ACTION_RETAKE,
                             reasons + ["La diferencia está en una expresión normalizada; se prueba una vez más."],
                             corroborated, spans)
        return Diagnosis(ORIGIN_NORMALIZATION, severity, ACTION_REVIEW_NORMALIZATION,
                         reasons + ["La diferencia persiste en una expresión normalizada."], corroborated, spans)
    if retakes_left:
        return Diagnosis(ORIGIN_SYNTHESIS, severity, ACTION_RETAKE,
                         reasons + ["Probable error de locución."], corroborated, spans)
    return Diagnosis(ORIGIN_SYNTHESIS, severity, ACTION_HUMAN,
                     reasons + ["El párrafo no superó la comparación tras las retomas permitidas."],
                     corroborated, spans)


def _issue_words_in_changes(issues: list[dict], changes: list[dict]) -> bool:
    spoken = " ".join(str(change.get("spoken", "")) for change in changes).casefold()
    words = [word for issue in issues for word in str(issue.get("expected", "")).casefold().split()]
    return bool(words) and all(word.strip(".,;:") in spoken for word in words)
