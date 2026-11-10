"""Spell out non-Spanish words where ONCE asks for it, and only there.

ONCE's rules (relayed for this upgrade):

* in **headings**, proper and common nouns written in another language are
  spelled out;
* in **bibliographies** and in **notes**, non-Spanish **surnames** are spelled.

Nowhere else: an ordinary paragraph is never spelled. The written text is never
changed; the spelling goes into the spoken form only ("Shakespeare (ese,
hache, a, ka, e, ese, pe, e, a, erre, e)"), in a format that is a setting.

A word is spelled only on clear evidence that it is not Spanish:

* the book marks it as another language (``xml:lang`` / ``lang``), or
* its spelling is impossible in native Spanish words: the letters k or w,
  the groups sh (at the start or end of a word), sch, th, ph, ck, tz, and
  doubled letters Spanish never doubles (ss, ff, tt, zz, pp, bb, dd, gg, mm),
  or a diacritic Spanish does not use (ä, ö, ç, ø, à, è…, ü outside güe/güi).

A word with only weaker signs (an unusual final consonant) is **not** spelled:
it is flagged for a person to decide, rather than guessed. Words Spanish has
adopted (kilo, whisky, web…) and any word in the configured "never" list are
left alone; words in the "always" list are spelled whatever their spelling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# The Spanish names of the letters (RAE, Ortografía 2010).
LETTER_NAMES = {
    "a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe", "g": "ge", "h": "hache", "i": "i",
    "j": "jota", "k": "ka", "l": "ele", "m": "eme", "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu",
    "r": "erre", "s": "ese", "t": "te", "u": "u", "v": "uve", "w": "uve doble", "x": "equis", "y": "ye", "z": "zeta",
}
# Letters that are not a base letter plus a mark.
SPECIAL_LETTERS = {"ß": "doble ese", "ø": "o con barra", "æ": "a, e", "œ": "o, e", "ł": "ele con barra",
                   "đ": "de con barra", "ı": "i sin punto", "'": "apóstrofo", "’": "apóstrofo", "-": "guion"}
MARK_NAMES = {"́": "con tilde", "̀": "con acento grave", "̈": "con diéresis",
              "̂": "con acento circunflejo", "̧": "con cedilla", "̊": "con anillo",
              "̃": "con virgulilla", "̌": "con carón", "̨": "con colita", "̆": "con breve",
              "̇": "con punto", "̋": "con doble acento"}
# Loanwords and exonyms Spanish writes this way: never spelled.
ADOPTED = {
    "kilo", "kilos", "kilogramo", "kilogramos", "kilómetro", "kilómetros", "kilovatio", "kilovatios", "kiosco",
    "kioscos", "kárate", "karate", "koala", "koalas", "kiwi", "kiwis", "karaoke", "kayak", "kétchup", "káiser",
    "whisky", "whiskys", "web", "webs", "wifi", "windsurf", "waterpolo", "kimono", "kril", "búnker", "kurdo",
    "kurdos", "kurda", "kurdas", "kuwaití", "kenia", "kenya", "kremlin", "kosovo", "kazajistán", "kirguistán",
    "pizza", "pizzas", "jazz", "show", "shows", "sheriff", "flash", "hall", "hobby", "pub", "test", "tests",
}
_STRONG = re.compile(r"[kw]|^sh|sh$|sch|th|ph|ck|tz|(ss|ff|tt|zz|pp|bb|dd|gg|mm)")
_SPANISH_MARKS = {"́"}  # á é í ó ú; ñ and ü are handled apart
_WEAK_FINAL = re.compile(r"[bcfghkmpqtvw]$")
_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|['’\-](?=[^\W\d_]))*")


def _marks(word: str) -> set[str]:
    return {char for char in unicodedata.normalize("NFD", word) if unicodedata.combining(char)}


def _foreign_diacritic(word: str) -> bool:
    lowered = word.casefold()
    for index, char in enumerate(lowered):
        if char in "ñ":
            continue
        if char == "ü":
            # Spanish writes ü only in güe, güi.
            if not (index and lowered[index - 1] == "g" and lowered[index + 1:index + 2] in {"e", "i", "é", "í"}):
                return True
            continue
        if char in SPECIAL_LETTERS and char not in {"'", "’", "-"}:
            return True
        if _marks(char) - _SPANISH_MARKS:
            return True
    return False


def evidence(word: str) -> str:
    """ "strong", "weak" or "" (nothing suggests the word is not Spanish)."""
    lowered = word.casefold()
    if lowered in ADOPTED or len(lowered) < 2:
        return ""
    if _foreign_diacritic(lowered):
        return "strong"
    base = "".join(char for char in unicodedata.normalize("NFD", lowered) if not unicodedata.combining(char))
    if _STRONG.search(base):
        return "strong"
    if _WEAK_FINAL.search(base) and len(base) > 3:
        return "weak"
    return ""


def spell(word: str) -> str:
    """ "Müller" -> "eme, u con diéresis, ele, ele, e, erre"."""
    names = []
    for char in word:
        # lower(), not casefold(): casefold turns ß into "ss" and loses the letter.
        lowered = char.lower()
        if lowered in LETTER_NAMES:
            names.append(LETTER_NAMES[lowered])
            continue
        if lowered in SPECIAL_LETTERS:
            names.append(SPECIAL_LETTERS[lowered])
            continue
        decomposed = unicodedata.normalize("NFD", lowered)
        base = decomposed[0]
        marks = [MARK_NAMES.get(mark, "con acento") for mark in decomposed[1:] if unicodedata.combining(mark)]
        if base in LETTER_NAMES:
            names.append(" ".join([LETTER_NAMES[base], *marks]))
    return ", ".join(names)


@dataclass(frozen=True)
class SpellingRules:
    """What is spelled, and how it is said."""

    enabled: bool = True
    # {word}: the word as written; {letters}: its letters' names.
    template: str = "{word} ({letters})"
    always: frozenset[str] = field(default_factory=frozenset)
    never: frozenset[str] = field(default_factory=frozenset)

    def render(self, word: str) -> str:
        return self.template.format(word=word, letters=spell(word))

    def decide(self, word: str, foreign_terms: set[str]) -> str:
        """ "spell", "flag" or "" for one word."""
        lowered = word.casefold()
        if lowered in self.never:
            return ""
        if lowered in self.always or lowered in foreign_terms:
            return "spell"
        found = evidence(word)
        return {"strong": "spell", "weak": "flag"}.get(found, "")


# A surname at the head of a bibliography entry, or after "y", "&", ";" in
# its list of authors: "Smith, J.", "SMITH, John", "Müller y Schmidt, H.".
_AUTHOR = re.compile(
    r"(?:^|;\s*|\s(?:y|e|&|and)\s+|,\s+(?=[A-ZÁÉÍÓÚÄÖÜ][^\W\d_]+,))"
    r"((?:(?:de|del|van|von|der|den|la|le|da|di|du|mac|mc|o')\s+)*[A-ZÁÉÍÓÚÑÄÖÜ][^\W\d_'’\-]*(?:[’'\-][A-Za-zÁÉÍÓÚÑÄÖÜ][^\W\d_]*)?)"
    r"(?=,?\s*(?:[A-ZÁÉÍÓÚÄÖÜ]\.|[A-ZÁÉÍÓÚÄÖÜ][a-záéíóúäöü]+\b|\(\d{4}|\d{4}))",
    re.UNICODE)
# A surname cited in a note: "Smith (2004)", "Smith, op. cit.", "Smith, p. 12", "Smith y Jones (1999)".
_CITED = re.compile(
    r"\b([A-ZÁÉÍÓÚÑÄÖÜ][^\W\d_'’\-]+(?:[’'\-][A-Za-zÄÖÜ][^\W\d_]*)?)"
    r"(?=\s*(?:\(\d{4}|,\s*(?:op\.|p\.|pp\.|ibid|ib[ií]d|\d{4}|[A-Z]\.)|\s+(?:y|e|&)\s+[A-Z][^\W\d_]+\s*\(\d{4}|\s+et\s+al))",
    re.UNICODE)


def candidates(text: str, context: str) -> list[tuple[int, int, str]]:
    """The words that may be spelled in this context: (start, end, word).

    Heading: every word. Bibliography: the authors' surnames. Note: surnames
    in the form of a citation. Any other context: none.
    """
    if context == "heading":
        return [(match.start(), match.end(), match.group()) for match in _WORD.finditer(text)]
    if context == "bibliography":
        found = [(match.start(1), match.end(1), match.group(1)) for match in _AUTHOR.finditer(text)]
    elif context == "note":
        found = [(match.start(1), match.end(1), match.group(1)) for match in _CITED.finditer(text)]
    else:
        return []
    words = []
    for start, end, name in found:
        # A compound surname ("van der Berg") is spelled word by word, particles included.
        for match in _WORD.finditer(name):
            words.append((start + match.start(), start + match.end(), match.group()))
    return words


def is_acronym(word: str) -> bool:
    """All capitals and short: a set of initials, which the acronym rules handle."""
    letters = [char for char in word if char.isalpha()]
    return 2 <= len(letters) <= 6 and all(char.isupper() for char in letters)
