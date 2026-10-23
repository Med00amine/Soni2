"""Spell Spanish number formats out for the narration copy of a sentence.

Deepgram Aura-2 reads Spanish digit groups with English conventions, which is a
real narration defect rather than a reporting artifact. Measured against the
live voice:

    "3.500 euros"   is narrated as "tres punto cinco euros"
    "2,5 metros"    is narrated as "veinticinco metros"
    "1.200 pesetas" is narrated as "uno punto dos pesetas"

The same quantities spelled out are narrated correctly, so the fix is to spell
them out before synthesis. Only the formats that are demonstrably misread are
rewritten — a Spanish thousands separator, a decimal comma, and unit
abbreviations after a number: ElevenLabs dropped the "m" of "9 m" on every
take, and review heard "9 de altura" until a person was asked. Plain integers,
times, dates and percentages are narrated correctly and are left as written.

The source text is never modified: callers apply this to the synthesis copy
only, so quality review still compares the audio against what the book says.
"""

from __future__ import annotations

import re

_UNITS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
          "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete",
          "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós", "veintitrés",
          "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho", "veintinueve")
_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta",
         7: "setenta", 8: "ochenta", 9: "noventa"}
_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos",
             6: "seiscientos", 7: "setecientos", 8: "ochocientos", 9: "novecientos"}

# A Spanish thousands separator: 3.500, 1.200.000, optionally with a decimal comma.
_GROUPED = re.compile(r"(?<![\d.,:/])\d{1,3}(?:\.\d{3})+(?:,\d+)?(?![\d.,:/])")
# A Spanish decimal comma: 2,5.
_DECIMAL = re.compile(r"(?<![\d.,:/])\d+,\d+(?![\d.,:/])")


def _under_hundred(value: int) -> str:
    if value < 30:
        return _UNITS[value]
    tens, unit = divmod(value, 10)
    return _TENS[tens] if unit == 0 else f"{_TENS[tens]} y {_UNITS[unit]}"


def _under_thousand(value: int) -> str:
    if value < 100:
        return _under_hundred(value)
    hundreds, rest = divmod(value, 100)
    if hundreds == 1 and rest == 0:
        return "cien"
    return _HUNDREDS[hundreds] if rest == 0 else f"{_HUNDREDS[hundreds]} {_under_hundred(rest)}"


def spanish_cardinal(value: int) -> str:
    """Spell a non-negative integer in Spanish words."""
    if value < 0:
        return f"menos {spanish_cardinal(-value)}"
    if value < 1000:
        return _under_thousand(value)
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        # "mil", never "un mil".
        head = "mil" if thousands == 1 else f"{_under_thousand(thousands)} mil"
        return head if rest == 0 else f"{head} {_under_thousand(rest)}"
    for limit, singular, plural in ((1_000_000_000_000, "billón", "billones"),
                                    (1_000_000, "millón", "millones")):
        if value >= limit:
            count, rest = divmod(value, limit)
            head = f"un {singular}" if count == 1 else f"{spanish_cardinal(count)} {plural}"
            return head if rest == 0 else f"{head} {spanish_cardinal(rest)}"
    return str(value)


def _spell_decimal(whole: str, fraction: str) -> str:
    # Spanish reads the fractional part digit by digit, which is also how the
    # comparison normalizer reads it back.
    digits = " ".join(_UNITS[int(digit)] for digit in fraction)
    return f"{spanish_cardinal(int(whole))} coma {digits}"


def _grouped(match: re.Match[str]) -> str:
    raw = match.group()
    whole, _, fraction = raw.partition(",")
    whole = whole.replace(".", "")
    return _spell_decimal(whole, fraction) if fraction else spanish_cardinal(int(whole))


def _decimal(match: re.Match[str]) -> str:
    whole, _, fraction = match.group().partition(",")
    return _spell_decimal(whole, fraction)


# Unit abbreviations after a number, as (singular, plural). "ha" is left out on
# purpose: "1990 ha sido" is a verb, not hectares.
_UNIT_WORDS = {
    "km/h": ("kilómetro por hora", "kilómetros por hora"), "km": ("kilómetro", "kilómetros"),
    "cm": ("centímetro", "centímetros"), "mm": ("milímetro", "milímetros"), "m": ("metro", "metros"),
    "kg": ("kilo", "kilos"), "mg": ("miligramo", "miligramos"), "hPa": ("hectopascal", "hectopascales"),
    "°C": ("grado", "grados"), "ºC": ("grado", "grados"),
}
_UNIT = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s?(km/h|km|cm|mm|kg|mg|hPa|°C|ºC|m)(?![\w/])")


def _unit(match: re.Match[str]) -> str:
    number, unit = match.groups()
    singular, plural = _UNIT_WORDS[unit]
    return f"{number} {singular if number == '1' else plural}"


def spell_numbers_for_speech(text: str) -> str:
    """Rewrite the number formats the voice misreads, leaving the rest alone."""
    spelled = _DECIMAL.sub(_decimal, _GROUPED.sub(_grouped, _UNIT.sub(_unit, text)))
    return spell_structural_romans(spelled)


# A Roman numeral after a structural word. Scoped deliberately: a bare "I" or
# "XX" in prose is far more often a letter, a name or a date than a number, and
# rewriting those would introduce mistakes rather than remove them.
_ROMAN_AFTER = re.compile(
    r"(?i)\b(cap[ií]tulos?|partes?|libros?|secci[oó]n|secciones|tomos?|actos?|escenas?|"
    r"vol[uú]menes|volumen|anexos?|ap[eé]ndices?)(\s+)([IVXLCDM]{1,15})(?=[\s.,:;)\]]|$)"
)
_ROMAN_VALUES = (("M", 1000), ("CM", 900), ("D", 500), ("CD", 400), ("C", 100), ("XC", 90),
                 ("L", 50), ("XL", 40), ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1))


def roman_to_int(value: str) -> int | None:
    """Parse a well-formed Roman numeral; None if it is not one."""
    text = value.upper()
    total = 0
    index = 0
    for symbol, amount in _ROMAN_VALUES:
        while text.startswith(symbol, index):
            total += amount
            index += len(symbol)
    if index != len(text) or not 1 <= total <= 3999:
        return None
    # Reject spellings Roman numerals do not use, such as "IIII" or "XXXX".
    return total if _int_to_roman(total) == text else None


def _int_to_roman(value: int) -> str:
    parts: list[str] = []
    for symbol, amount in _ROMAN_VALUES:
        while value >= amount:
            parts.append(symbol)
            value -= amount
    return "".join(parts)


def _spell_roman(match: re.Match[str]) -> str:
    number = roman_to_int(match.group(3))
    if number is None:
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{spanish_cardinal(number)}"


def spell_structural_romans(text: str) -> str:
    """"Capítulo XIV" -> "Capítulo catorce", so it is not read letter by letter."""
    return _ROMAN_AFTER.sub(_spell_roman, text)
