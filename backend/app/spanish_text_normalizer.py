"""Turn the book's written Spanish into what a Peninsular narrator says.

The source text is never modified. :func:`normalize_for_speech` returns the
spoken form together with a record of every change (rule, original text, spoken
text, source offsets) and a list of expressions it could not resolve safely.
An ambiguous expression is **left as written and flagged** instead of guessed:
"D. Pérez" may be "don Pérez" or an initial, "3/4" may be a fraction or the
third of April, "28013" may be a quantity or a postcode.

Rules, applied once each over non-overlapping spans of the original text:

* numeric dates (DD/MM/AAAA, the Spanish order) and ISO dates
* clock times (6:45 → "seis y cuarenta y cinco", 18:30 → "dieciocho treinta")
* ordinals (1.º, 2.ª, 3.er) and "n.º"
* currency (25 €, 3,50 €, $100, £200), percentages, units after a number
* grouped thousands (3.500) and decimal commas (2,5), with gender agreement
  taken from the following noun ("3.500 personas" → "tres mil quinientas")
* Roman numerals after structural words, "siglo" and known regnal names
* common Spanish abbreviations (Sr., Dra., Ud., etc., EE. UU., a. C. …)
* a few symbols (&, §, +, =, ×) between the things they join

Plain integers are left as digits: voices read them correctly in context and
spelling them out would force a gender guess ("21 cartas").

The result is idempotent: normalizing the spoken text again changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.spanish_speech import roman_to_int

_UNITS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve",
          "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete",
          "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós", "veintitrés",
          "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho", "veintinueve")
_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}
_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos",
             6: "seiscientos", 7: "setecientos", 8: "ochocientos", 9: "novecientos"}
_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
           "octubre", "noviembre", "diciembre")


# --------------------------------------------------------------------------- #
# Numbers in words, with gender
# --------------------------------------------------------------------------- #

def _one(form: str, gender: str) -> str:
    """The word for one: "uno" alone, "un" before a masculine noun, "una"."""
    return {"n": "uno", "m": "un", "f": "una"}[gender] if form == "uno" else form


def _under_hundred(value: int, gender: str) -> str:
    if value == 1:
        return _one("uno", gender)
    if value == 21:
        return {"n": "veintiuno", "m": "veintiún", "f": "veintiuna"}[gender]
    if value < 30:
        return _UNITS[value]
    tens, unit = divmod(value, 10)
    if unit == 0:
        return _TENS[tens]
    return f"{_TENS[tens]} y {_one(_UNITS[unit], gender) if unit == 1 else _UNITS[unit]}"


def _under_thousand(value: int, gender: str) -> str:
    if value < 100:
        return _under_hundred(value, gender)
    hundreds, rest = divmod(value, 100)
    if hundreds == 1:
        head = "cien" if rest == 0 else "ciento"
    else:
        head = _HUNDREDS[hundreds]
        if gender == "f":
            head = head[:-2] + "as"
    return head if rest == 0 else f"{head} {_under_hundred(rest, gender)}"


def cardinal(value: int, gender: str = "n") -> str:
    """Spanish words for a non-negative integer.

    ``gender`` is ``n`` (counting: "uno", "veintiuno"), ``m`` (before a
    masculine noun: "un", "veintiún") or ``f`` ("una", "doscientas").
    """
    if value < 0:
        return f"menos {cardinal(-value, gender)}"
    if value < 1000:
        return _under_thousand(value, gender)
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        # "veintiún mil", "doscientas mil personas"; never "un mil".
        head = "mil" if thousands == 1 else f"{_under_thousand(thousands, 'f' if gender == 'f' else 'm')} mil"
        return head if rest == 0 else f"{head} {_under_thousand(rest, gender)}"
    for limit, singular, plural in ((1_000_000_000_000, "billón", "billones"), (1_000_000, "millón", "millones")):
        if value >= limit:
            count, rest = divmod(value, limit)
            head = f"un {singular}" if count == 1 else f"{cardinal(count, 'm')} {plural}"
            return head if rest == 0 else f"{head} {cardinal(rest, gender)}"
    return str(value)


def _fraction_words(fraction: str) -> str:
    """Spanish reads "3,14" as "tres coma catorce", "2,05" as "dos coma cero cinco"."""
    if len(fraction) <= 2 and not fraction.startswith("0"):
        return cardinal(int(fraction))
    return " ".join(_UNITS[int(digit)] for digit in fraction)


def number_words(raw: str, gender: str = "n") -> str:
    """Words for a Spanish-written number: 3.500, 2,5, 1.200.000,75, -4."""
    negative = raw.startswith(("-", "−"))
    body = raw.lstrip("-−")
    whole, _, fraction = body.partition(",")
    whole = whole.replace(".", "")
    if fraction:
        spoken = f"{cardinal(int(whole), 'n')} coma {_fraction_words(fraction)}"
    else:
        spoken = cardinal(int(whole), gender)
    return f"menos {spoken}" if negative else spoken


def _is_round_million(raw: str) -> bool:
    whole = raw.lstrip("-−").partition(",")[0].replace(".", "")
    return not raw.partition(",")[2] and int(whole) >= 1_000_000 and int(whole) % 1_000_000 == 0


_ORDINALS_M = {1: "primero", 2: "segundo", 3: "tercero", 4: "cuarto", 5: "quinto", 6: "sexto", 7: "séptimo",
               8: "octavo", 9: "noveno", 10: "décimo", 11: "undécimo", 12: "duodécimo"}
_ORDINAL_TENS = {10: "décimo", 20: "vigésimo", 30: "trigésimo", 40: "cuadragésimo", 50: "quincuagésimo",
                 60: "sexagésimo", 70: "septuagésimo", 80: "octogésimo", 90: "nonagésimo"}


def ordinal(value: int, gender: str = "m", apocope: bool = False) -> str | None:
    """1 → primero / primera / primer. ``None`` beyond 99, where the voice's
    reading is more trustworthy than a rarely used Latin form."""
    if value in _ORDINALS_M and value <= 12:
        word = _ORDINALS_M[value]
    elif 13 <= value <= 99:
        tens, unit = divmod(value, 10)
        word = _ORDINAL_TENS[tens * 10] if unit == 0 else f"{_ORDINAL_TENS[tens * 10]} {_ORDINALS_M[unit]}"
        if tens == 1:  # decimotercero: one word
            word = "decimo" + _ORDINALS_M[unit]
    else:
        return None
    if gender == "f":
        return " ".join(part[:-1] + "a" if part.endswith("o") else part for part in word.split())
    if apocope and word.split()[-1] in {"primero", "tercero"}:
        word = word[:-1]
    return word


# --------------------------------------------------------------------------- #
# The gender of the noun after a number
# --------------------------------------------------------------------------- #

_MASCULINE_IN_A = {"día", "días", "mapa", "mapas", "problema", "problemas", "tema", "temas", "sistema",
                   "sistemas", "programa", "programas", "idioma", "idiomas", "clima", "climas", "planeta",
                   "planetas", "poema", "poemas", "drama", "dramas", "dilema", "dilemas", "esquema",
                   "esquemas", "sofá", "sofás", "tranvía", "tranvías", "mediodía", "mediodías", "pijama",
                   "pijamas", "cometa", "cometas", "telegrama", "telegramas", "kilogramas"}
_FEMININE_OTHER = {"mujer", "mujeres", "flor", "flores", "noche", "noches", "calle", "calles", "gente", "llave",
                   "llaves", "mano", "manos", "foto", "fotos", "moto", "motos", "imagen", "imágenes", "parte",
                   "partes", "clase", "clases", "frase", "frases", "fuente", "fuentes", "nave", "naves", "nube",
                   "nubes", "torre", "torres", "leche", "piel", "pieles", "red", "redes", "vez", "veces",
                   "luz", "luces", "paz", "voz", "voces", "cruz", "cruces", "ley", "leyes", "tarde", "tardes",
                   "muerte", "muertes", "suerte", "carne", "carnes", "nieve", "sede", "sedes", "serie", "series",
                   "especie", "especies", "superficie", "superficies", "radio", "orden", "órdenes",
                   "costumbre", "costumbres", "nariz", "raíz", "raíces", "sal", "miel", "cárcel", "cárceles"}
_FEMININE_SUFFIXES = ("ción", "ciones", "sión", "siones", "dad", "dades", "tad", "tades", "tud", "tudes",
                      "umbre", "umbres", "itis")


def noun_gender(word: str) -> str:
    """``f`` or ``m`` for the noun that follows a number; ``m`` when unsure."""
    lowered = word.casefold().strip(".,;:!?»”\")")
    if not lowered or not lowered[0].isalpha():
        return "m"
    if lowered in _MASCULINE_IN_A:
        return "m"
    if lowered in _FEMININE_OTHER or lowered.endswith(_FEMININE_SUFFIXES):
        return "f"
    if lowered.endswith(("a", "as")) and not lowered.endswith(("ista", "istas")):
        return "f"
    return "m"


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Change:
    rule: str
    original: str
    spoken: str
    start: int
    end: int

    def as_dict(self) -> dict:
        return {"rule": self.rule, "original": self.original, "spoken": self.spoken,
                "start": self.start, "end": self.end}


@dataclass(frozen=True)
class Flag:
    code: str
    text: str
    start: int
    end: int
    message: str
    # "review": a person should check how it is read; "watch": listen for it.
    severity: str = "review"

    def as_dict(self) -> dict:
        return {"code": self.code, "text": self.text, "start": self.start, "end": self.end,
                "message": self.message, "severity": self.severity}


@dataclass
class SpokenText:
    source: str
    spoken: str
    changes: list[Change] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)

    @property
    def watch_terms(self) -> list[str]:
        return [flag.text for flag in self.flags if flag.severity == "watch"]

    def as_dict(self) -> dict:
        return {"changes": [change.as_dict() for change in self.changes],
                "flags": [flag.as_dict() for flag in self.flags]}


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

_B = r"(?<![\w.,/:])"           # a number starts here
_E = r"(?![\w]|[.,]\d|/\d|:\d)"  # and ends here (a sentence's full stop is fine)
_NUMBER = r"[-−]?(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+,\d+|\d+)"
_NEXT_WORD = re.compile(r"\s*(?:de\s+)?([^\W\d_]+)")

_URL = re.compile(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+\.[\w.]+", re.I)
_DATE_DMY = re.compile(rf"{_B}(\d{{1,2}})([/.-])(\d{{1,2}})\2(\d{{4}}){_E}")
_DATE_ISO = re.compile(rf"{_B}(\d{{4}})-(\d{{2}})-(\d{{2}}){_E}")
# "h." keeps its full stop when that full stop also ends the sentence.
_TIME = re.compile(rf"{_B}(\d{{1,2}}):(\d{{2}})(?:\s?(h)(?:\.(?=\s+[a-záéíóúñ]))?(?!\w))?{_E}")
_NUMERO = re.compile(r"(?<!\w)(?:[nN]\.?\s?[ºo°]|[nN]úm\.|[nN]um\.)(?=\s?\d)")
# "º" (the ordinal mark) but not "°" (degrees), and never "ºC".
_ORDINAL = re.compile(r"(?<![\w.,])(\d{1,3})\.?\s?(º|ª|er|ᵉʳ)(?!\w|\s?C\b)")
_CURRENCY_AFTER = re.compile(rf"{_B}({_NUMBER})\s?(€|EUR|\$|USD|£|libras esterlinas)(?!\w)")
_CURRENCY_BEFORE = re.compile(rf"(?<!\w)(€|US\$|\$|£)\s?({_NUMBER}){_E}")
_PERCENT = re.compile(rf"{_B}({_NUMBER})\s?(%|‰)")
_UNIT_TABLE = {
    "km/h": ("kilómetro por hora", "kilómetros por hora", "m"), "km²": ("kilómetro cuadrado", "kilómetros cuadrados", "m"),
    "m²": ("metro cuadrado", "metros cuadrados", "m"), "m³": ("metro cúbico", "metros cúbicos", "m"),
    "km": ("kilómetro", "kilómetros", "m"), "cm": ("centímetro", "centímetros", "m"),
    "mm": ("milímetro", "milímetros", "m"), "m": ("metro", "metros", "m"), "kg": ("kilo", "kilos", "m"),
    "mg": ("miligramo", "miligramos", "m"), "g": ("gramo", "gramos", "m"), "ml": ("mililitro", "mililitros", "m"),
    "l": ("litro", "litros", "m"), "t": ("tonelada", "toneladas", "f"), "ha": ("hectárea", "hectáreas", "f"),
    "hPa": ("hectopascal", "hectopascales", "m"), "°C": ("grado", "grados", "m"), "ºC": ("grado", "grados", "m"),
    "°": ("grado", "grados", "m"), "º": ("grado", "grados", "m"), "h": ("hora", "horas", "f"),
    "min": ("minuto", "minutos", "m"), "seg": ("segundo", "segundos", "m"),
    "kW": ("kilovatio", "kilovatios", "m"), "W": ("vatio", "vatios", "m"), "V": ("voltio", "voltios", "m"),
}
_UNIT = re.compile(rf"{_B}({_NUMBER})\s?(" + "|".join(
    re.escape(unit) for unit in sorted(_UNIT_TABLE, key=len, reverse=True)) + r")(?![\w/²³])")
_STANDALONE_NUMBER = re.compile(rf"{_B}([-−]?(?:\d{{1,3}}(?:\.\d{{3}})+(?:,\d+)?|\d+,\d+)){_E}")
_LONG_PLAIN = re.compile(rf"{_B}\d{{5,}}{_E}")
_FRACTION_OR_DATE = re.compile(rf"{_B}(\d{{1,2}})/(\d{{1,2}}){_E}")

_STRUCTURAL_ROMAN = re.compile(
    r"(?i)\b(cap[ií]tulos?|partes?|libros?|secci[oó]n|secciones|tomos?|actos?|escenas?|"
    r"vol[uú]menes|volumen|anexos?|ap[eé]ndices?|siglos?|s\.)(\s+)([IVXLCDM]{1,15})(?=[\s.,:;)\]]|$)"
)
_REGNAL_NAMES = {
    "Alfonso", "Fernando", "Felipe", "Carlos", "Isabel", "Juan", "Juana", "Enrique", "Pedro", "Jaime", "Sancho",
    "Ramiro", "García", "Luis", "Napoleón", "Pío", "León", "Gregorio", "Benedicto", "Inocencio", "Clemente",
    "Urbano", "Bonifacio", "Pablo", "Francisco", "Leonor", "Urraca", "Catalina", "María", "Ricardo", "Eduardo",
    "Jorge", "Guillermo", "Federico", "Amadeo", "Alejandro", "Constantino", "Justiniano", "Recaredo",
}
_FEMININE_NAMES = {"Isabel", "Juana", "Leonor", "Urraca", "Catalina", "María"}
_REGNAL = re.compile(r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)\s+([IVXL]{1,6})(?=[\s.,:;)\]»”]|$)")

# (pattern, spoken, keeps sentence-final full stop when the sentence ends there)
_ABBREVIATIONS: list[tuple[str, str, bool]] = [
    (r"EE\.\s?UU\.", "Estados Unidos", True), (r"EEUU", "Estados Unidos", False),
    (r"a\.\s?C\.", "antes de Cristo", True), (r"d\.\s?C\.", "después de Cristo", True),
    (r"p\.\s?ej\.", "por ejemplo", False), (r"Srta\.", "señorita", False), (r"Sras\.", "señoras", False),
    (r"Sres\.", "señores", False), (r"Sra\.", "señora", False), (r"Sr\.", "señor", False),
    (r"Dras\.", "doctoras", False), (r"Dres\.", "doctores", False), (r"Dra\.", "doctora", False),
    (r"Dr\.", "doctor", False), (r"Profa\.", "profesora", False), (r"Prof\.", "profesor", False),
    (r"Uds\.|Vds\.", "ustedes", False), (r"Ud\.|Vd\.", "usted", False), (r"Dña\.|D\.ª|Dª", "doña", False),
    (r"Excmo\.", "excelentísimo", False), (r"Excma\.", "excelentísima", False),
    (r"Ilmo\.", "ilustrísimo", False), (r"Ilma\.", "ilustrísima", False), (r"Sto\.", "santo", False),
    (r"Sta\.", "santa", False), (r"Avda\.", "avenida", False), (r"Pza\.", "plaza", False),
    (r"Cía\.", "compañía", True), (r"Hnos\.", "hermanos", True), (r"págs\.", "páginas", False),
    (r"pág\.", "página", False), (r"tel\.|Tel\.|Tfno\.|tfno\.", "teléfono", False), (r"aprox\.", "aproximadamente", True),
    (r"etc\.", "etcétera", True), (r"arts\.", "artículos", False), (r"art\.", "artículo", False),
    (r"vols\.", "volúmenes", False), (r"vol\.", "volumen", False), (r"núms\.", "números", False),
]
_ABBREVIATION = [(re.compile(rf"(?<![\w.]){pattern}(?=\s|$|[,;:)»”])"), spoken, final)
                 for pattern, spoken, final in _ABBREVIATIONS]
_CALLE = re.compile(r"(?<!\w)[cC]/\s?(?=[A-ZÁÉÍÓÚÑ])")
_DON = re.compile(r"(?<![\w.])D\.\s(?=[A-ZÁÉÍÓÚÑ])")
_AMBIGUOUS_ABBREVIATIONS = re.compile(r"(?<![\w.])(ed\.|a\.\s?m\.|p\.\s?m\.|s\.\s?f\.|op\.\s?cit\.|cf\.|ib[ií]d\.)(?=\s|$|[,;:)])")
_SYMBOLS = [
    (re.compile(r"(?<=\w)\s?&\s?(?=\w)"), " y "),
    (re.compile(r"§\s?(?=\d)"), "sección "),
    (re.compile(r"(?<=\d)\s?\+\s?(?=\d)"), " más "),
    (re.compile(r"(?<=\d)\s?=\s?(?=\d)"), " igual a "),
    (re.compile(r"(?<=\d)\s?[×x]\s?(?=\d)"), " por "),
]
_ACRONYM = re.compile(r"(?<![\w.])([A-ZÁÉÍÓÚÑ]{2,6}|(?:[A-Z]\.){2,5})(?![\w])")
_WORD_ACRONYMS = {"ONU", "OTAN", "ONCE", "RENFE", "UNESCO", "UNICEF", "SIDA", "OVNI", "NASA", "ESO", "AVE", "IVA",
                  "INEM", "FIFA", "OPEP", "FAO", "MIR", "ERE", "OMS", "UEFA"}


class _Builder:
    def __init__(self, text: str) -> None:
        self.text = text
        self.claimed: list[tuple[int, int]] = []
        self.changes: list[Change] = []
        self.flags: list[Flag] = []

    def free(self, start: int, end: int) -> bool:
        return not any(begin < end and start < finish for begin, finish in self.claimed)

    def change(self, rule: str, start: int, end: int, spoken: str) -> None:
        if self.free(start, end):
            self.claimed.append((start, end))
            self.changes.append(Change(rule, self.text[start:end], spoken, start, end))

    def keep(self, start: int, end: int) -> None:
        if self.free(start, end):
            self.claimed.append((start, end))

    def flag(self, code: str, start: int, end: int, message: str, severity: str = "review") -> None:
        if not any(item.start == start and item.code == code for item in self.flags):
            self.flags.append(Flag(code, self.text[start:end], start, end, message, severity))

    def render(self) -> str:
        parts: list[str] = []
        cursor = 0
        for change in sorted(self.changes, key=lambda item: item.start):
            parts.append(self.text[cursor:change.start])
            parts.append(change.spoken)
            cursor = change.end
        parts.append(self.text[cursor:])
        return re.sub(r"[ \t]{2,}", " ", "".join(parts))


def _gender_after(text: str, end: int) -> str:
    match = _NEXT_WORD.match(text, end)
    return noun_gender(match.group(1)) if match else "m"


def _year_words(year: int) -> str:
    return cardinal(year, "n")


def _dates(builder: _Builder, text: str) -> None:
    for match in _DATE_DMY.finditer(text):
        day, month, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
        try:
            date(year, month, day)
        except ValueError:
            builder.flag("invalid_date", match.start(), match.end(),
                         "Parece una fecha, pero no existe con el orden día/mes de España; se deja tal cual.")
            builder.keep(match.start(), match.end())
            continue
        builder.change("date", match.start(), match.end(),
                       f"{cardinal(day, 'n')} de {_MONTHS[month - 1]} de {_year_words(year)}")
    for match in _DATE_ISO.finditer(text):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            date(year, month, day)
        except ValueError:
            continue
        builder.change("date", match.start(), match.end(),
                       f"{cardinal(day, 'n')} de {_MONTHS[month - 1]} de {_year_words(year)}")


def _clock(hour: int, minute: int, suffix: bool) -> str:
    hour_words = "una" if hour == 1 else cardinal(hour, "f" if hour == 21 else "n")
    if hour <= 12:
        if minute == 0:
            spoken = f"{hour_words} en punto"
        elif minute == 15:
            spoken = f"{hour_words} y cuarto"
        elif minute == 30:
            spoken = f"{hour_words} y media"
        else:
            spoken = f"{hour_words} y {cardinal(minute, 'n')}"
    elif minute == 0:
        spoken = f"{hour_words} horas"
        suffix = False
    elif minute < 10:
        spoken = f"{hour_words} cero {cardinal(minute, 'n')}"
    else:
        spoken = f"{hour_words} {cardinal(minute, 'n')}"
    return f"{spoken} horas" if suffix else spoken


def _times(builder: _Builder, text: str) -> None:
    for match in _TIME.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        builder.change("time", match.start(), match.end(), _clock(hour, minute, bool(match.group(3))))


def _ordinals(builder: _Builder, text: str) -> None:
    for match in _NUMERO.finditer(text):
        builder.change("abbreviation", match.start(), match.end(), "número")
    for match in _ORDINAL.finditer(text):
        value, mark = int(match.group(1)), match.group(2)
        # "30º" after a number-like context is degrees; handled by the unit rule
        # only when followed by C. A bare "º" here is an ordinal mark.
        gender = "f" if mark == "ª" else "m"
        word = ordinal(value, gender, apocope=mark in {"er", "ᵉʳ"})
        if word is None:
            builder.flag("ordinal", match.start(), match.end(), "Ordinal demasiado alto para leerlo con seguridad.")
            builder.keep(match.start(), match.end())
            continue
        builder.change("ordinal", match.start(), match.end(), word)


_CURRENCY_NAMES = {"€": ("euro", "euros", "m"), "EUR": ("euro", "euros", "m"), "$": ("dólar", "dólares", "m"),
                   "US$": ("dólar", "dólares", "m"), "USD": ("dólar", "dólares", "m"), "£": ("libra", "libras", "f"),
                   "libras esterlinas": ("libra esterlina", "libras esterlinas", "f")}


def _quantity(raw: str, singular: str, plural: str, gender: str) -> str:
    whole = raw.lstrip("-−").partition(",")[0].replace(".", "")
    one = whole == "1" and not raw.partition(",")[2]
    noun = singular if one else plural
    of = " de" if _is_round_million(raw) else ""
    return f"{number_words(raw, gender)}{of} {noun}"


def _currency(builder: _Builder, text: str) -> None:
    for match in _CURRENCY_AFTER.finditer(text):
        singular, plural, gender = _CURRENCY_NAMES[match.group(2)]
        builder.change("currency", match.start(), match.end(), _quantity(match.group(1), singular, plural, gender))
    for match in _CURRENCY_BEFORE.finditer(text):
        singular, plural, gender = _CURRENCY_NAMES[match.group(1)]
        builder.change("currency", match.start(), match.end(), _quantity(match.group(2), singular, plural, gender))


def _percent(builder: _Builder, text: str) -> None:
    for match in _PERCENT.finditer(text):
        word = "por ciento" if match.group(2) == "%" else "por mil"
        builder.change("percent", match.start(), match.end(), f"{number_words(match.group(1), 'n')} {word}")


def _units(builder: _Builder, text: str) -> None:
    for match in _UNIT.finditer(text):
        raw, unit = match.group(1), match.group(2)
        if unit == "ha":
            following = re.match(r"\s+([a-záéíóúñ]+)", text[match.end():])
            if following and re.search(r"(?:ado|ada|ido|ida|to|ta|cho|so|que)$", following.group(1)):
                # "1990 ha sido": the verb haber, not hectares.
                builder.keep(match.start(), match.end())
                continue
            if following and following.group(1) == "de":
                # "4 ha de terreno" (hectares) or "ha de llegar" (a verb).
                builder.flag("ambiguous_unit", match.start(), match.end(),
                             "«ha» puede ser hectáreas o el verbo haber; se deja tal cual.")
                builder.keep(match.start(), match.end())
                continue
        singular, plural, gender = _UNIT_TABLE[unit]
        builder.change("unit", match.start(), match.end(), _quantity(raw, singular, plural, gender))


def _numbers(builder: _Builder, text: str) -> None:
    for match in _STANDALONE_NUMBER.finditer(text):
        raw = match.group(1)
        gender = "n" if "," in raw else _gender_after(text, match.end())
        builder.change("number", match.start(), match.end(), number_words(raw, gender))
    for match in _LONG_PLAIN.finditer(text):
        if builder.free(match.start(), match.end()):
            builder.flag("long_number", match.start(), match.end(),
                         "Número largo sin separadores: puede ser una cantidad, un código o un teléfono.")
    for match in _FRACTION_OR_DATE.finditer(text):
        if builder.free(match.start(), match.end()):
            builder.flag("fraction_or_date", match.start(), match.end(),
                         "Puede ser una fracción o una fecha sin año; se deja tal cual.")
            builder.keep(match.start(), match.end())


def _romans(builder: _Builder, text: str) -> None:
    for match in _STRUCTURAL_ROMAN.finditer(text):
        value = roman_to_int(match.group(3))
        if value is None:
            continue
        word = match.group(1)
        prefix = "siglo" if word.casefold() == "s." else word
        start = match.start(1) if word.casefold() == "s." else match.start(3)
        spoken = f"{prefix}{match.group(2)}{cardinal(value, 'n')}" if word.casefold() == "s." else cardinal(value, "n")
        builder.change("roman", start, match.end(3), spoken)
    for match in _REGNAL.finditer(text):
        name, numeral = match.group(1), match.group(2)
        value = roman_to_int(numeral)
        if value is None:
            continue
        if name not in _REGNAL_NAMES:
            if builder.free(match.start(2), match.end(2)) and len(numeral) > 1:
                builder.flag("roman_numeral", match.start(2), match.end(2),
                             f"«{name} {numeral}» puede ser un nombre con ordinal; comprueba cómo se lee.", "watch")
            continue
        gender = "f" if name in _FEMININE_NAMES else "m"
        spoken = ordinal(value, gender) if value <= 10 else cardinal(value, "n")
        builder.change("regnal_number", match.start(2), match.end(2), spoken or numeral)


def _abbreviations(builder: _Builder, text: str) -> None:
    for pattern, spoken, final in _ABBREVIATION:
        for match in pattern.finditer(text):
            rest = text[match.end():]
            ends_sentence = final and (not rest.strip() or re.match(r"\s+[A-ZÁÉÍÓÚÑ¿¡—«\"]", rest) is not None)
            starts_sentence = not text[:match.start()].strip() or re.search(r"[.!?…¡¿—«\"]\s*$", text[:match.start()])
            words = spoken[0].upper() + spoken[1:] if starts_sentence else spoken
            builder.change("abbreviation", match.start(), match.end(), words + ("." if ends_sentence else ""))
    for match in _CALLE.finditer(text):
        builder.change("abbreviation", match.start(), match.end(), "calle ")
    for match in _DON.finditer(text):
        if builder.free(match.start(), match.end()):
            builder.flag("ambiguous_abbreviation", match.start(), match.end() - 1,
                         "«D.» puede ser «don» o una inicial; se deja tal cual.")
            builder.keep(match.start(), match.end() - 1)
    for match in _AMBIGUOUS_ABBREVIATIONS.finditer(text):
        if builder.free(match.start(), match.end()):
            builder.flag("ambiguous_abbreviation", match.start(), match.end(),
                         f"La abreviatura «{match.group(0)}» tiene varias lecturas; se deja tal cual.")
            builder.keep(match.start(), match.end())


def _symbols(builder: _Builder, text: str) -> None:
    for pattern, spoken in _SYMBOLS:
        for match in pattern.finditer(text):
            builder.change("symbol", match.start(), match.end(), spoken)


def _watch_list(builder: _Builder, text: str) -> None:
    """Terms worth listening for: they are where voices most often slip."""
    letters = [character for character in text if character.isalpha()]
    shouting = len(letters) >= 6 and sum(character.isupper() for character in letters) / len(letters) > 0.8
    for match in _ACRONYM.finditer(text):
        token = match.group(1)
        # A title set in capitals ("LOS OJOS VERDES") is words, not acronyms.
        if shouting or token in _WORD_ACRONYMS or not builder.free(match.start(), match.end()):
            continue
        if roman_to_int(token) is not None:
            continue
        builder.flag("acronym", match.start(), match.end(),
                     f"Sigla «{token}»: comprueba si se lee deletreada o como palabra.", "watch")
    for match in re.finditer(r"(?<![.!?¿¡:—«\"]\s)(?<=\s)([A-ZÁÉÍÓÚÑ][a-záéíóúüñ]{2,})", text):
        word = match.group(1)
        if _foreign_looking(word):
            builder.flag("foreign_name", match.start(1), match.end(1),
                         f"Nombre que no sigue la ortografía española («{word}»): escucha su pronunciación.",
                         "watch")
    for match in _URL.finditer(text):
        builder.flag("url", match.start(), match.end(), "Dirección web o de correo: comprueba cómo se lee.")


_FOREIGN_SPELLING = re.compile(r"sh|sch|th|ph|ck|w|k|tt|ee|oo|ou|ff|ss|gh|tz|^y[^aeiouáéíóú]|[^aeiouáéíóúnsrldzjy]$")


def _foreign_looking(word: str) -> bool:
    """Spelling Spanish does not use: a name the voice may say the English way."""
    return bool(_FOREIGN_SPELLING.search(word.casefold()))


_LONE_ROMAN = re.compile(r"^\s*([IVXLC]{1,7})\.?\s*$")


def normalize_for_speech(text: str) -> SpokenText:
    builder = _Builder(text)
    lone = _LONE_ROMAN.match(text)
    if lone and roman_to_int(lone.group(1)) is not None:
        # A section number on its own ("II"): read as a number, not as letters.
        builder.change("roman", lone.start(1), lone.end(1), cardinal(roman_to_int(lone.group(1)), "n"))
        return SpokenText(text, builder.render(), builder.changes, builder.flags)
    for match in _URL.finditer(text):
        builder.keep(match.start(), match.end())
    _dates(builder, text)
    _times(builder, text)
    _ordinals(builder, text)
    _currency(builder, text)
    _percent(builder, text)
    _units(builder, text)
    _numbers(builder, text)
    _romans(builder, text)
    _abbreviations(builder, text)
    _symbols(builder, text)
    _watch_list(builder, text)
    builder.changes.sort(key=lambda item: item.start)
    builder.flags.sort(key=lambda item: item.start)
    return SpokenText(text, builder.render(), builder.changes, builder.flags)


def spoken_form(text: str) -> str:
    return normalize_for_speech(text).spoken
