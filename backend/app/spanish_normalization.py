"""Spanish-aware canonical tokens for comparing book text with ASR output.

The normalizer is deliberately conservative: it only folds well-formed number
phrases and explicit number/unit expressions. Original text is retained in each
token span so QA can report what the reader and recognizer actually saw.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
from datetime import date
import re
import unicodedata


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int
    norm_start: int = -1
    norm_end: int = -1


def _plain(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _plain_with_offsets(value: str) -> tuple[str, list[int], list[int]]:
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, original in enumerate(value):
        for char in unicodedata.normalize("NFKD", original.casefold()):
            if unicodedata.combining(char):
                continue
            chars.append(char)
            starts.append(index)
            ends.append(index + 1)
    return "".join(chars), starts, ends


_SMALL = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3,
    "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13,
    "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
         "setenta": 70, "ochenta": 80, "noventa": 90}
_HUNDREDS = {"cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300,
             "cuatrocientos": 400, "quinientos": 500, "seiscientos": 600,
             "setecientos": 700, "ochocientos": 800, "novecientos": 900}
_ORDINALS = {
    "primero": 1, "primer": 1, "primera": 1, "segundo": 2, "segunda": 2,
    "tercero": 3, "tercer": 3, "tercera": 3, "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6, "septimo": 7,
    "septima": 7, "octavo": 8, "octava": 8, "noveno": 9, "novena": 9,
    "decimo": 10, "decima": 10,
}
_UNITS = {
    "m": "m", "metro": "m", "metros": "m", "km": "km", "kilometro": "km",
    "kilometros": "km", "cm": "cm", "centimetro": "cm", "centimetros": "cm",
    "mm": "mm", "milimetro": "mm", "milimetros": "mm", "milla": "mi",
    "millas": "mi", "kg": "kg", "kilo": "kg", "kilos": "kg",
    "kilogramo": "kg", "kilogramos": "kg", "g": "g", "gramo": "g",
    "gramos": "g", "l": "l", "litro": "l", "litros": "l", "porciento": "%",
    "euro": "EUR", "euros": "EUR", "eur": "EUR", "dolar": "USD", "dolares": "USD", "usd": "USD",
    # Abbreviations a recognizer writes out in full when they are narrated.
    "hpa": "hPa", "hectopascal": "hPa", "hectopascales": "hPa",
    "ha": "ha", "hectarea": "ha", "hectareas": "ha",
    "ml": "ml", "mililitro": "ml", "mililitros": "ml",
    "t": "t", "tonelada": "t", "toneladas": "t",
}


def _parse_under_1000(words: list[str]) -> int | None:
    if not words:
        return None
    words = [word for word in words if word != "y"]
    if not words:
        return None
    total = 0
    i = 0
    if words[i] in _HUNDREDS:
        total += _HUNDREDS[words[i]]
        i += 1
        if total == 100 and i == len(words):
            return total
    tail = words[i:]
    if not tail:
        return total
    if len(tail) == 1 and tail[0] in _SMALL:
        return total + _SMALL[tail[0]]
    if len(tail) == 1 and tail[0] in _TENS:
        return total + _TENS[tail[0]]
    if len(tail) == 2 and tail[0] in _TENS and tail[1] in _SMALL and 1 <= _SMALL[tail[1]] <= 9:
        return total + _TENS[tail[0]] + _SMALL[tail[1]]
    return None


def _parse_cardinal(words: list[str]) -> int | None:
    words = [word for word in words if word != "y"]
    if not words:
        return None
    if len(words) == 1 and words[0] in _ORDINALS:
        return None
    total = 0
    group: list[str] = []
    for word in words:
        if word in {"millon", "millones", "billon", "billones", "mil"}:
            value = _parse_under_1000(group) if group else 1
            if value is None:
                return None
            multiplier = {"mil": 1_000, "millon": 1_000_000, "millones": 1_000_000,
                          "billon": 1_000_000_000_000, "billones": 1_000_000_000_000}[word]
            total += value * multiplier
            group = []
        elif word in _SMALL or word in _TENS or word in _HUNDREDS:
            group.append(word)
        else:
            return None
    tail = _parse_under_1000(group) if group else 0
    if tail is None:
        return None
    result = total + tail
    return result if result <= 999_999_999_999_999 else None


def _parse_number_words(words: list[str]) -> tuple[str, int] | None:
    """Return (canonical value, consumed word count) for a confident phrase."""
    if not words:
        return None
    # Ordinals are limited to common, unambiguous standalone forms.
    if words[0] in _ORDINALS:
        return f"ord:{_ORDINALS[words[0]]}", 1
    # Decimal speech: "dos coma cinco". Preserve fractional digits rather than
    # interpreting them as an integer (2,5 must never become 25).
    if "coma" in words:
        comma = words.index("coma")
        whole = _parse_cardinal(words[:comma])
        fractional_words = words[comma + 1:]
        fractional = "".join(str(_SMALL[w]) for w in fractional_words) if fractional_words and all(w in _SMALL and _SMALL[w] < 10 for w in fractional_words) else ""
        if whole is not None and fractional:
            return f"num:{whole}.{fractional}", comma + 1 + len(fractional_words)
    # Take the longest valid prefix; surrounding prose remains separately compared.
    for length in range(min(len(words), 12), 0, -1):
        value = _parse_cardinal(words[:length])
        if value is not None:
            return f"num:{value}", length
    return None


_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_NUMBER_RE = re.compile(r"\d+(?:(?:[.,]\d{3})+|(?:[.,]\d+))?(?:\s\d{3})*")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")
# Built from _UNITS so the pattern and the lookup table can never drift
# apart: a unit listed in the table is always recognized after a number.
# Longest first, so "kilometros" wins over "km" and "metros" over "m".
_UNIT_PATTERN = "(?:" + "|".join(
    [re.escape(name) for name in sorted(_UNITS, key=len, reverse=True)] + ["[$]", "€"]
) + ")"
_NUMBER_CORE = r"(?:\d{1,3}(?:[.,]\d{3})+(?:,\d+)?|\d{1,3}(?:\s\d{3})+|\d+(?:[.,]\d+)?)"
# A decimal the recognizer wrote with digits around the spoken separator:
# "2 coma 5" for 2,5, optionally followed by a unit.
_DIGIT_DECIMAL_RE = re.compile(
    rf"(?<!\w)(\d+)\s+coma\s+(\d(?:\s+\d)*)(?:\s*({_UNIT_PATTERN}))?(?!\w)", re.I
)
# Recognizers write a spoken "por ciento" as "por ciento", "por cien" or
# "por 100"; all three mean the same percentage.
_PERCENT_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(?:%|por\s+(?:ciento|cien|100))(?!\w)", re.I)
# And a spoken date as "12 de marzo de 2024", with the day and year left in
# digits, which the word-phrase parser cannot see.
_TEXTUAL_DATE_RE = re.compile(r"(?<!\w)(\d{1,2})\s+de\s+([a-z]+)\s+del?\s+(\d{3,4})(?!\w)", re.I)
_DIGIT_ORDINAL_RE = re.compile(r"(?<!\w)(\d+)(?:\.?[\u00ba\u00aa]|\.?er|\.?to|\.?ra|\.?ro)(?!\w)", re.I)
_MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
           "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
           "noviembre": 11, "diciembre": 12}


def _numeric_value(raw: str) -> str:
    compact = raw.replace(" ", "")
    if "," in compact:
        # In Spanish text comma is the decimal separator.
        compact = compact.replace(".", "").replace(",", ".")
    elif "." in compact:
        parts = compact.split(".")
        compact = "".join(parts) if all(len(part) == 3 for part in parts[1:]) else compact
    try:
        number = float(compact) if "." in compact else int(compact)
    except ValueError:
        return compact
    return str(number).rstrip("0").rstrip(".") if isinstance(number, float) and number % 1 else str(int(number)) if isinstance(number, float) else str(number)


def _numeric_tokens(text: str, allow_spoken_time: bool, source_has_time: bool, source_has_date: bool = False) -> list[Token]:
    plain, source_starts, source_ends = _plain_with_offsets(text)
    def span(start: int, end: int) -> tuple[int, int]:
        return source_starts[start], source_ends[end - 1]
    words = list(_WORD_RE.finditer(plain))
    # Use original character offsets; accent folding does not change character count
    # for the supported Spanish text and ordinary decomposed accents are normalized.
    tokens: list[Token] = []
    # Claimed character ranges, so a later pass cannot block an earlier
    # expression. Pass order sets priority; position does not.
    claimed: list[tuple[int, int]] = []

    def is_free(start: int, end: int) -> bool:
        return not any(begin < end and start < finish for begin, finish in claimed)

    def claim(start: int, end: int) -> None:
        claimed.append((start, end))


    for match in _DIGIT_ORDINAL_RE.finditer(plain):
        original_start, original_end = span(match.start(), match.end())
        tokens.append(Token(f"ord:{int(match.group(1))}", original_start, original_end, match.start(), match.end()))
        claim(match.start(), match.end())

    for match in _DATE_RE.finditer(plain):
        day, month, year = map(int, match.groups())
        try:
            date(year, month, day)
        except ValueError:
            continue
        else:
            original_start, original_end = span(match.start(), match.end())
            tokens.append(Token(f"date:{year:04d}-{month:02d}-{day:02d}", original_start, original_end, match.start(), match.end()))
            claim(match.start(), match.end())

    # A date the recognizer wrote as "12 de marzo de 2024": the day and year stay
    # in digits, so the spoken-word date parser below never sees it.
    for match in _TEXTUAL_DATE_RE.finditer(plain):
        if not is_free(match.start(), match.end()):
            continue
        month = _MONTHS.get(match.group(2))
        if month is None:
            continue
        day, year = int(match.group(1)), int(match.group(3))
        try:
            date(year, month, day)
        except ValueError:
            continue
        original_start, original_end = span(match.start(), match.end())
        tokens.append(Token(f"date:{year:04d}-{month:02d}-{day:02d}",
                            original_start, original_end, match.start(), match.end()))
        claim(match.start(), match.end())

    # A decimal the recognizer wrote as digits around the spoken separator:
    # "2 coma 5 metros" for 2,5 metros. Narration spells the decimal out, so the
    # transcript comes back in this mixed form.
    for match in _DIGIT_DECIMAL_RE.finditer(plain):
        if not is_free(match.start(), match.end()):
            continue
        fraction = "".join(match.group(2).split())
        value = _numeric_value(f"{match.group(1)}.{fraction}")
        unit = _UNITS.get(re.sub(r"\s+", "", _plain(match.group(3) or ""))) if match.group(3) else None
        key = f"measure:{value}:{unit}" if unit else f"num:{value}"
        original_start, original_end = span(match.start(), match.end())
        tokens.append(Token(key, original_start, original_end, match.start(), match.end()))
        claim(match.start(), match.end())

    # Percentages, including the "por 100" and "por cien" forms recognizers emit.
    for match in _PERCENT_RE.finditer(plain):
        if not is_free(match.start(), match.end()):
            continue
        original_start, original_end = span(match.start(), match.end())
        tokens.append(Token(f"measure:{_numeric_value(match.group(1))}:%",
                            original_start, original_end, match.start(), match.end()))
        claim(match.start(), match.end())

    # Numeric times are explicit and always safe to canonicalize.
    for match in _TIME_RE.finditer(plain):
        hour, minute = map(int, match.group().split(":"))
        if hour <= 23 and minute <= 59:
            original_start, original_end = span(match.start(), match.end())
            tokens.append(Token(f"time:{hour:02d}:{minute:02d}", original_start, original_end, match.start(), match.end()))
            claim(match.start(), match.end())

    # Numeric quantities, percentages, and currency. Grouped whitespace is only
    # accepted in strict thousands groups (3 500), never arbitrary number pairs.
    numeric_pattern = re.compile(
        rf"(?<!\w){_NUMBER_CORE}(?:\s*{_UNIT_PATTERN})?(?:\s*%)?(?!\w)", re.I
    )
    for match in numeric_pattern.finditer(plain):
        if not is_free(match.start(), match.end()):
            continue
        raw = match.group().strip()
        if not raw:
            continue
        num_match = re.match(_NUMBER_CORE, raw)
        if not num_match:
            continue
        value = _numeric_value(num_match.group())
        tail = _plain(raw[num_match.end():]).strip()
        unit = "%" if tail == "%" else "EUR" if tail == chr(0x20ac) else _UNITS.get(re.sub(r"\s+", "", tail))
        key = f"measure:{value}:{unit}" if unit else f"num:{value}"
        original_start, original_end = span(match.start(), match.end())
        tokens.append(Token(key, original_start, original_end, match.start(), match.end()))
        claim(match.start(), match.end())

    # Parse recognized spoken number phrases and optional spoken units.
    wi = 0
    while wi < len(words):
        current = words[wi]
        if not is_free(current.start(), current.end()):
            wi += 1
            continue
        phrase_words = [_plain(item.group()) for item in words[wi:wi + 14]]
        if allow_spoken_time and source_has_date:
            day = _parse_number_words(phrase_words)
            if day and day[0].startswith("num:"):
                month_at = wi + day[1] + 1
                year_at = month_at + 2
                if (wi + day[1] < len(words) and _plain(words[wi + day[1]].group()) == "de"
                        and month_at < len(words) and _plain(words[month_at].group()) in _MONTHS
                        and month_at + 1 < len(words) and _plain(words[month_at + 1].group()) == "de"):
                    year_words = [_plain(item.group()) for item in words[year_at:year_at + 8]]
                    year = _parse_number_words(year_words)
                    month = _MONTHS[_plain(words[month_at].group())]
                    if year and year[0].startswith("num:"):
                        try:
                            day_value, year_value = int(day[0][4:]), int(year[0][4:])
                            if 1 <= day_value <= 31 and year_value >= 1:
                                date(year_value, month, day_value)
                                end_index = year_at + year[1]
                                end = words[end_index - 1].end()
                                original_start, original_end = span(current.start(), end)
                                tokens.append(Token(f"date:{year_value:04d}-{month:02d}-{day_value:02d}", original_start, original_end, current.start(), end))
                                claim(current.start(), end)
                                wi = end_index
                                continue
                        except (ValueError, TypeError):
                            pass
        parsed = _parse_number_words(phrase_words)
        if not parsed:
            wi += 1
            continue
        key, consumed = parsed
        end_index = wi + consumed
        # Spoken clock times are only folded when the source itself contained an
        # explicit time. This avoids treating two adjacent prose numbers as a time.
        if allow_spoken_time and source_has_time and key.startswith("num:"):
            second = _parse_number_words(phrase_words[consumed:]) if consumed < len(phrase_words) else None
            if second and second[0].startswith("num:") and "." not in key and "." not in second[0]:
                try:
                    hour, minute = int(key[4:]), int(second[0][4:])
                    if hour <= 23 and minute <= 59:
                        key = f"time:{hour:02d}:{minute:02d}"
                        end_index += second[1]
                except ValueError:
                    pass
        unit_word = _plain(words[end_index].group()) if end_index < len(words) else ""
        unit = _UNITS.get(unit_word)
        if unit_word == "por" and end_index + 1 < len(words) and _plain(words[end_index + 1].group()) == "ciento":
            unit, end_index = "%", end_index + 1
        if unit and key.startswith("num:"):
            key = f"measure:{key[4:]}:{unit}"
            end_index += 1
        end = words[end_index - 1].end()
        original_start, original_end = span(current.start(), end)
        tokens.append(Token(key, original_start, original_end, current.start(), end))
        claim(current.start(), end)
        wi = end_index

    tokens.sort(key=lambda item: (item.norm_start, item.norm_end))
    # A clock time the recognizer wrote as two plain numbers: "6 45" for 6:45.
    # Only folded when the source itself contained an explicit time, and only
    # for digits separated by nothing but spaces, so two ordinary adjacent
    # numbers in prose are never merged into a time.
    if allow_spoken_time and source_has_time:
        merged: list[Token] = []
        index = 0
        while index < len(tokens):
            current = tokens[index]
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if (following is not None
                    and current.value.startswith("num:") and following.value.startswith("num:")
                    and "." not in current.value and "." not in following.value
                    and not plain[current.norm_end:following.norm_start].strip()):
                try:
                    hour, minute = int(current.value[4:]), int(following.value[4:])
                except ValueError:
                    hour, minute = -1, -1
                raw_minute = plain[following.norm_start:following.norm_end]
                if 0 <= hour <= 23 and 0 <= minute <= 59 and len(raw_minute) == 2:
                    merged.append(Token(f"time:{hour:02d}:{minute:02d}", current.start, following.end,
                                        current.norm_start, following.norm_end))
                    index += 2
                    continue
            merged.append(current)
            index += 1
        tokens = merged

    # Emit ordinary words not covered by a parsed numeric expression.
    result: list[Token] = []
    token_index = 0
    for word in words:
        while token_index < len(tokens) and tokens[token_index].norm_end <= word.start():
            token_index += 1
        if token_index < len(tokens) and tokens[token_index].norm_start <= word.start() < tokens[token_index].norm_end:
            continue
        original_start, original_end = span(word.start(), word.end())
        result.append(Token(_plain(word.group()), original_start, original_end, word.start(), word.end()))
    result.extend(tokens)
    result.sort(key=lambda item: (item.norm_start, item.norm_end))
    return result


def canonical_tokens(source_text: str, spoken_text: str | None = None) -> tuple[list[Token], list[Token]]:
    """Return source and ASR tokens; source context enables safe spoken-time parsing."""
    source_has_time = bool(_TIME_RE.search(_plain(source_text)))
    source_has_date = bool(_DATE_RE.search(_plain(source_text)))
    source = _numeric_tokens(source_text, allow_spoken_time=False, source_has_time=source_has_time, source_has_date=source_has_date)
    spoken = _numeric_tokens(spoken_text if spoken_text is not None else source_text,
                             allow_spoken_time=True, source_has_time=source_has_time, source_has_date=source_has_date)
    return source, spoken


def comparison_tokens(text: str) -> list[Token]:
    """Canonicalize one text for metrics; use compare() for source-aware time handling."""
    return _numeric_tokens(text, allow_spoken_time=False, source_has_time=False)


def surface(text: str, tokens: list[Token], start: int, end: int) -> str:
    """Recover original spelling for an aligned token range."""
    if start >= end:
        return ""
    return text[tokens[start].start:tokens[end - 1].end]


def surface_span(text: str, tokens: list[Token], start: int, end: int) -> tuple[int, int]:
    """Character offsets of an aligned token range, for highlighting the text.

    An empty range (one side of an insertion or omission) collapses to the
    position where the missing or added words belong, so the interface can mark
    the spot rather than nothing at all.
    """
    if start < end:
        return tokens[start].start, tokens[end - 1].end
    if start < len(tokens):
        point = tokens[start].start
    elif tokens:
        point = tokens[-1].end
    else:
        point = len(text)
    return point, point


def _digit_run_value(tokens: list[Token]) -> str | None:
    """Concatenate a run of single-digit number tokens, e.g. 1 4 7 -> "147"."""
    digits: list[str] = []
    for token in tokens:
        if not token.value.startswith("num:"):
            return None
        value = token.value[4:]
        if len(value) != 1 or not value.isdigit():
            return None
        digits.append(value)
    return "".join(digits) if len(digits) >= 2 else None


def _is_digit_readback(left: list[Token], right: list[Token]) -> bool:
    """True when one side spells a number out digit by digit.

    Recognizers often transcribe "ciento cuarenta y siete" as "1 4 7". That is
    the same quantity read back differently, not a misreading. Only runs of
    single digits that reconstruct the other side's value exactly are folded,
    so "3 20" against "trescientos veinte" stays a genuine mismatch.
    """
    for single, run in ((left, right), (right, left)):
        if len(single) == 1 and single[0].value.startswith("num:"):
            if _digit_run_value(run) == single[0].value[4:]:
                return True
    return False


def compare_transcripts(source_text: str, spoken_text: str) -> list[dict[str, str]]:
    """Align source and ASR canonically while reporting original text spans."""
    source_tokens, spoken_tokens = canonical_tokens(source_text, spoken_text)
    matcher = difflib.SequenceMatcher(
        a=[token.value for token in source_tokens],
        b=[token.value for token in spoken_tokens],
        autojunk=False,
    )
    issues: list[dict[str, str]] = []
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and _is_digit_readback(
            source_tokens[start_a:end_a], spoken_tokens[start_b:end_b]
        ):
            continue
        expected_start, expected_end = surface_span(source_text, source_tokens, start_a, end_a)
        recognized_start, recognized_end = surface_span(spoken_text, spoken_tokens, start_b, end_b)
        issues.append({
            "kind": tag,
            "expected": surface(source_text, source_tokens, start_a, end_a),
            "recognized": surface(spoken_text, spoken_tokens, start_b, end_b),
            # Offsets into each text, so the interface can show the reviewer
            # exactly which words differ instead of two paragraphs to compare.
            "expected_start": expected_start,
            "expected_end": expected_end,
            "recognized_start": recognized_start,
            "recognized_end": recognized_end,
            "category": "likely_mismatch",
        })
    return issues

