"""Stable source-linked sentence segmentation and synthesis-only pronunciation rules."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping

from app.spanish_speech import spell_numbers_for_speech  # noqa: F401  (kept for callers)
from app.spanish_text_normalizer import SpokenText, normalize_for_speech


_ABBREVIATIONS = {
    "dr", "dra", "sr", "sra", "srta", "ud", "uds", "d", "dª", "dº",
    "pág", "pags", "págs", "núm", "num", "nº", "no", "aprox", "av", "etc",
    "ee.uu", "vs", "vol", "fig", "art", "cap", "prof", "lic",
}
_CLOSERS = set('"\'»”’)]}')


def segment_sentences(text: str, source_id: str, chapter_index: int) -> list[dict[str, object]]:
    """Split text conservatively and retain exact text offsets into the source string."""
    result: list[dict[str, object]] = []
    start = 0
    index = 0
    for position, character in enumerate(text):
        if character not in ".!?…" or (character == "." and not _period_ends_sentence(text, position)):
            continue
        end = position + 1
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        if end < len(text) and not text[end].isspace():
            continue
        raw = text[start:end]
        left_trim = len(raw) - len(raw.lstrip())
        sentence = raw.strip()
        if sentence:
            sentence_start = start + left_trim
            sentence_end = sentence_start + len(sentence)
            identity = f"{source_id}:{chapter_index}:{index}:{sentence_start}:{sentence_end}:{sentence}"
            result.append({
                "sentence_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "chapter_index": chapter_index,
                "sequence": index,
                "text": sentence,
                "text_start": sentence_start,
                "text_end": sentence_end,
            })
            index += 1
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    tail = text[start:]
    left_trim = len(tail) - len(tail.lstrip())
    sentence = tail.strip()
    if sentence:
        sentence_start = start + left_trim
        sentence_end = sentence_start + len(sentence)
        identity = f"{source_id}:{chapter_index}:{index}:{sentence_start}:{sentence_end}:{sentence}"
        result.append({
            "sentence_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "chapter_index": chapter_index,
            "sequence": index,
            "text": sentence,
            "text_start": sentence_start,
            "text_end": sentence_end,
        })
    return result


def segment_paragraphs(paragraphs: list[str], source_id: str, chapter_index: int) -> list[dict[str, object]]:
    """One narration unit per paragraph, in the same shape as ``segment_sentences``.

    A whole paragraph is narrated in one request so the voice keeps its
    intonation across sentences; sentence-by-sentence narration reset it at
    every full stop and joined clips that each began with up to a second of
    provider silence, which sounded like stuttering. Offsets refer to the
    paragraphs joined with blank lines.
    """
    result: list[dict[str, object]] = []
    offset = 0
    for paragraph in paragraphs:
        text = paragraph.strip()
        if text:
            index = len(result)
            identity = f"{source_id}:{chapter_index}:{index}:paragraph:{text}"
            result.append({
                "sentence_id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "chapter_index": chapter_index,
                "sequence": index,
                "text": text,
                "text_start": offset,
                "text_end": offset + len(text),
            })
        offset += len(paragraph) + 2
    return result


def _period_ends_sentence(text: str, position: int) -> bool:
    if position > 0 and position + 1 < len(text) and text[position - 1].isdigit() and text[position + 1].isdigit():
        return False
    prefix = text[:position + 1]
    match = re.search(r"([\wªº.]+)\.$", prefix, flags=re.UNICODE)
    if match and match.group(1).casefold() in _ABBREVIATIONS:
        return False
    return True


def build_spoken_text(text: str) -> SpokenText:
    """What a Peninsular narrator says for ``text``, with every change recorded.

    This is also what speech review compares the recognizer against: the
    expected spoken form, not the written one.
    """
    return normalize_for_speech(text)


def build_synthesis_text(text: str, lexicon: Mapping[str, str]) -> str:
    """Prepare the narration copy of a sentence; the source text is unchanged.

    The text is first normalized into its spoken Spanish form (dates, times,
    amounts, abbreviations…), then the reviewer's pronunciation lexicon is
    applied, so an operator entry always wins over the automatic rewrite.
    Lexicon respellings are phonetic hints for the voice, which is why speech
    review compares against the spoken form *before* them.
    """
    return apply_pronunciation_lexicon(normalize_for_speech(text).spoken, lexicon)


def apply_pronunciation_lexicon(text: str, lexicon: Mapping[str, str]) -> str:
    """Apply longest literal terms to a TTS-only copy; callers retain the source text."""
    replacements = [(term.strip(), pronunciation.strip()) for term, pronunciation in lexicon.items()
                    if term.strip() and pronunciation.strip()]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    rendered = text
    for term, pronunciation in replacements:
        rendered = re.sub(
            rf"(?<!\w){re.escape(term)}(?!\w)",
            lambda _match, value=pronunciation: value,
            rendered,
            flags=re.IGNORECASE | re.UNICODE,
        )
    return rendered
