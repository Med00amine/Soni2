"""Offline Spanish pronunciation transformations for TTS input."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from time import perf_counter

from num2words import num2words

from .lexicon import PronunciationLexicon
from .normalizer import TextNormalizer

logger = logging.getLogger(__name__)

_DEFAULT_LEXICON = Path(__file__).with_name("pronunciation_lexicon.json")
_ABBREVIATIONS = {
    "Dra.": "doctora",
    "Dr.": "doctor",
    "Sra.": "señora",
    "Sr.": "señor",
    "pág.": "página",
    "núm.": "número",
    "etc.": "etcétera",
}
_UNITS = {
    "km": "kilómetros",
    "kg": "kilogramos",
    "g": "gramos",
    "m": "metros",
    "cm": "centímetros",
    "°C": "grados Celsius",
}
_MONTHS = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
    12: "diciembre",
}


class SpanishPronunciationProcessor:
    """Convert common Spanish notation to deterministic speech-oriented text."""

    def __init__(
        self,
        lexicon: PronunciationLexicon | None = None,
        *,
        lexicon_path: Path | None = _DEFAULT_LEXICON,
        debug: bool = False,
    ) -> None:
        self.debug = debug
        self.normalizer = TextNormalizer()
        self.lexicon = lexicon or PronunciationLexicon(_load_entries(lexicon_path))

    def process(self, source_text: str) -> str:
        started = perf_counter()
        source = self.normalizer.normalize(source_text)
        result = self.lexicon.resolve(source)
        result = _replace_abbreviations(result)
        result = _replace_dates(result)
        result = _replace_times(result)
        result = _replace_measurements(result)
        result = _replace_currency_and_percent(result)
        result = _replace_decimals(result)
        result = _replace_integers(result)
        result = _replace_symbols(result)
        result = self.normalizer.normalize(result)
        if self.debug:
            logger.info("SOURCE: %s", source_text)
            logger.info("TTS: %s", result)
        logger.debug("spanish_normalization_seconds=%.6f", perf_counter() - started)
        return result


def _load_entries(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Unable to read pronunciation lexicon '{path}'.") from exc
    entries = payload.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("Pronunciation lexicon entries must be an object.")
    return {str(source): str(replacement) for source, replacement in entries.items()}


def _replace_abbreviations(text: str) -> str:
    for source, replacement in sorted(_ABBREVIATIONS.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"(?<!\w){re.escape(source)}(?!\w)", replacement, text)
    return text


def _replace_dates(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        day, month, year = (int(part) for part in match.groups())
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return match.group(0)
        return f"el {num2words(day, lang='es')} de {_MONTHS[month]} de {num2words(year, lang='es')}"

    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", replace, text)


def _replace_times(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        hour, minute = (int(part) for part in match.groups())
        if hour > 23 or minute > 59:
            return match.group(0)
        return f"{num2words(hour, lang='es')} horas y {num2words(minute, lang='es')} minutos"

    return re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", replace, text)


def _replace_measurements(text: str) -> str:
    for unit, spoken in sorted(_UNITS.items(), key=lambda item: -len(item[0])):
        text = re.sub(
            rf"\b(\d+(?:[.,]\d+)?)\s*{re.escape(unit)}\b",
            lambda match: f"{_number(match.group(1))} {spoken}",
            text,
        )
    return text


def _replace_currency_and_percent(text: str) -> str:
    text = re.sub(r"\b(\d+(?:[.,]\d+)?)\s*%", lambda m: f"{_number(m.group(1))} por ciento", text)
    text = re.sub(r"(\d+(?:[.,]\d+)?)\s*€", lambda m: f"{_number(m.group(1))} euros", text)
    text = re.sub(r"(\d+(?:[.,]\d+)?)\s*\$", lambda m: f"{_number(m.group(1))} dólares", text)
    return text


def _replace_decimals(text: str) -> str:
    return re.sub(
        r"\b(\d+)[.,](\d+)\b",
        lambda match: f"{num2words(int(match.group(1)), lang='es')} coma {num2words(int(match.group(2)), lang='es')}",
        text,
    )


def _replace_integers(text: str) -> str:
    return re.sub(r"\b\d+\b", lambda match: num2words(int(match.group(0)), lang="es"), text)


def _replace_symbols(text: str) -> str:
    return text.replace("&", " y ").replace("=", " igual a ").replace(" - ", " más ")


def _number(value: str) -> str:
    if "," in value or "." in value:
        return _replace_decimals(value)
    return num2words(int(value), lang="es")
