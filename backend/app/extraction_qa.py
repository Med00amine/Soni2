"""Evidence that the text a book was read from is the text the book contains.

Extraction is where content most easily disappears without anyone noticing: a
parser that only knows ``<p>`` drops every list item, a PDF page that is a
scanned image yields no text at all, a running head is either narrated on every
page or removed together with a real line. This module does not trust the
parser. It counts the words of the raw source independently, subtracts what the
parser deliberately dropped (each drop carries a reason), and reports whatever
remains unaccounted for, with context, so a person can check it.

Severity levels:

``blocking``
    Content is known to be missing (an image-only page with no OCR, nothing
    extracted at all). The book cannot pass.
``warning``
    Content may be missing or altered (unaccounted words, low-confidence OCR,
    leftover hyphenation). A person must look before release.
``info``
    Recorded for traceability (running heads removed, invented navigation
    titles that are not narrated).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# A word broken at a line end that survived extraction: "distribui- dores".
_BROKEN_WORD = re.compile(r"\b([a-záéíóúüñ]{2,})- ([a-záéíóúüñ]{2,})\b")
# Characters that are almost always OCR or encoding damage inside a word.
_GARBAGE_WORD = re.compile(r"(?<![\w])(?=\w*\d)(?=\w*[a-záéíóúñ])[a-záéíóúñ]+\d+[a-záéíóúñ]*(?![\w])", re.I)
_REPLACEMENT = "�"


def words(text: str) -> list[str]:
    """Comparable words: case folded, NFC, punctuation ignored."""
    normalized = unicodedata.normalize("NFC", text.replace("­", "")).casefold()
    return _WORD.findall(normalized)


def rejoin_line_breaks(text: str) -> str:
    """Undo line-end hyphenation in raw text, the way the parser does.

    Without this every hyphenated word would be counted as two missing
    fragments and one surplus word, burying real omissions in noise.
    """
    text = text.replace("­", "")
    return re.sub(r"(?<=[^\W\d_])-[ \t]*\r?\n[ \t]*(?=[a-záéíóúüñ])", "", text)


@dataclass
class ExtractionReport:
    source_format: str
    method: str = ""
    pages: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    generated_titles: list[str] = field(default_factory=list)

    def drop(self, kind: str, text: str, reason: str, location: str = "") -> None:
        text = " ".join(text.split())
        if text:
            self.dropped.append({"kind": kind, "text": text[:300], "reason": reason, "location": location,
                                 "_full": text})

    def issue(self, severity: str, code: str, message: str, *, location: str = "", text: str = "") -> None:
        self.issues.append({"severity": severity, "code": code, "message": message,
                            "location": location, "text": text[:300]})

    @property
    def blocking(self) -> list[dict]:
        return [item for item in self.issues if item["severity"] == BLOCKING]

    @property
    def warnings(self) -> list[dict]:
        return [item for item in self.issues if item["severity"] == WARNING]

    def as_dict(self) -> dict:
        return {
            "source_format": self.source_format,
            "method": self.method,
            "pages": self.pages,
            "dropped": [{key: value for key, value in item.items() if not key.startswith("_")}
                        for item in self.dropped],
            "issues": self.issues,
            "coverage": self.coverage,
            "generated_titles": self.generated_titles,
            "status": ("blocked" if self.blocking else "needs_review" if self.warnings else "passed"),
        }


def measure_coverage(report: ExtractionReport, raw_texts: list[str], extracted: list[str],
                     *, sample: int = 12) -> dict:
    """Count source words that reached neither the narration nor a recorded drop.

    Multiset arithmetic over words: order is checked elsewhere (completeness),
    this answers "is anything missing". Words the parser adds itself (the
    "Nota" before a footnote) are reported as additions, never as coverage.
    """
    raw_text = "\n".join(rejoin_line_breaks(text) for text in raw_texts)
    source = Counter(words(raw_text))
    produced = Counter(words("\n".join(extracted)))
    dropped = Counter(words("\n".join(item.get("_full", item["text"]) for item in report.dropped)))
    missing = source - produced - dropped
    added = produced - source
    total = sum(source.values())
    unaccounted = sum(missing.values())
    examples = _missing_in_context(raw_text, missing, sample)
    coverage = {
        "source_words": total,
        "extracted_words": sum(produced.values()),
        "dropped_words": sum(dropped.values()),
        "unaccounted_words": unaccounted,
        "added_words": sum(added.values()),
        "ratio": round(1 - unaccounted / total, 6) if total else 0.0,
        "unaccounted_examples": examples,
        "added_examples": sorted(added, key=lambda word: -added[word])[:sample],
    }
    report.coverage = coverage
    if total == 0 or not any(text.strip() for text in extracted):
        report.issue(BLOCKING, "no_text", "No se ha extraído ningún texto narrable del documento.")
    elif unaccounted:
        report.issue(WARNING, "unaccounted_words",
                     f"{unaccounted} palabras del original no aparecen en el texto que se narrará ni en "
                     "lo descartado a propósito. Comprueba que no falte contenido.",
                     text="; ".join(example["context"] for example in examples[:5]))
    return coverage


def _missing_in_context(raw_text: str, missing: Counter, limit: int) -> list[dict]:
    examples: list[dict] = []
    folded = unicodedata.normalize("NFC", raw_text).casefold()
    for word in sorted(missing, key=lambda item: -missing[item])[:limit]:
        match = re.search(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", folded)
        context = ""
        if match:
            start, end = max(0, match.start() - 50), min(len(raw_text), match.end() + 50)
            context = " ".join(unicodedata.normalize("NFC", raw_text)[start:end].split())
        examples.append({"word": word, "count": missing[word], "context": context})
    return examples


def inspect_text_quality(report: ExtractionReport, chapters: list[tuple[str, list[str]]]) -> None:
    """Flag leftovers of extraction damage in the text that will be narrated."""
    for chapter_index, (title, paragraphs) in enumerate(chapters):
        for paragraph_index, paragraph in enumerate(paragraphs):
            location = f"capítulo {chapter_index + 1}, párrafo {paragraph_index + 1}"
            for match in _BROKEN_WORD.finditer(paragraph):
                report.issue(WARNING, "broken_word",
                             f"Posible palabra partida por un salto de línea: «{match.group(0)}».",
                             location=location, text=paragraph[max(0, match.start() - 40):match.end() + 40])
            if _REPLACEMENT in paragraph:
                report.issue(WARNING, "encoding_damage", "El texto contiene caracteres ilegibles (�).",
                             location=location, text=paragraph[:200])
            for match in _GARBAGE_WORD.finditer(paragraph):
                if re.fullmatch(r"\d+(?:er|º|ª|o|a|os|as)", match.group(0), re.I):
                    continue
                report.issue(WARNING, "suspicious_token",
                             f"Palabra con letras y cifras mezcladas, típica de un error de OCR: «{match.group(0)}».",
                             location=location, text=paragraph[max(0, match.start() - 40):match.end() + 40])


def detect_repeated_margin_lines(pages: list[list[str]], *, min_pages: int = 3, share: float = 0.4) -> set[str]:
    """Lines repeated at the top or bottom of many pages: running heads and footers.

    Digits are masked so "Capítulo 3 · 47" and "Capítulo 3 · 48" count as one
    line. Only a page's first and last lines are considered, and a line that
    ends a sentence never counts: running heads do not end with a full stop,
    and a body sentence that happens to recur must not be deleted.
    """
    if len(pages) < min_pages:
        return set()
    counts: Counter = Counter()
    for lines in pages:
        edge = {_mask(line) for line in (lines[:1] + lines[-1:]) if line.strip()
                and not re.search(r"[.!?…:;]\s*[»”\"')]*\s*$", line)}
        counts.update(edge)
    threshold = max(min_pages, int(len(pages) * share + 0.999))
    return {line for line, count in counts.items() if count >= threshold and len(line) <= 120}


def _mask(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split())).casefold()


def is_repeated_margin(line: str, repeated: set[str]) -> bool:
    return _mask(line) in repeated
