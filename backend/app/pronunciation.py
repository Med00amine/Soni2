"""Work out how a word is pronounced, and how to respell it so a voice says it.

Spanish orthography is close to phonemic, so a word's sound can be derived
from its spelling with rules: this module produces the IPA, the syllables and
the stressed syllable for any Spanish word. That is what lets the interface
show a reviewer *why* a word came out wrong.

The second half is the repair. A voice reads what it is given, so the fix for a
mispronounced word is to give it a different spelling that it reads correctly —
a respelling. Foreign names are the common case: a Spanish voice reads
"Hugh" letter by letter, but reads "Hiú" as a person would say it. Respellings
are suggested here and **confirmed by ear** before being saved, because no rule
can know how a name is meant to sound.

Nothing here changes the book. Respellings live in the pronunciation lexicon
and are applied to the narration copy only, so quality review still compares
the audio against what the book actually says.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

VOWELS = "aeiouáéíóúü"
STRONG_VOWELS = "aeoáéó"
WEAK_VOWELS = "iuíúü"
ACCENTED = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u"}

# Castilian Spanish: "c" before e/i and "z" are θ, as they are in Spain.
_DIGRAPHS = {"ch": "tʃ", "ll": "ʎ", "rr": "r", "qu": "k", "gu": "ɡ", "gü": "ɡw"}


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _is_vowel(ch: str) -> bool:
    return ch in VOWELS


_CLUSTERS = {"bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr", "tr", "tl",
             "ch", "ll", "rr", "qu"}


def syllables(word: str) -> list[str]:
    """Split a Spanish word into syllables.

    Works from the vowel groups outwards: the consonants between two vowels are
    shared, one going to the next syllable, two when they form a cluster that
    can start a word (bl, br, tr, ch, ll, …). Consonants before the first vowel
    open the word; consonants after the last vowel close it, rather than
    becoming a syllable of their own.
    """
    text = word.lower()
    if not text:
        return []

    # Where each vowel group (the nucleus of a syllable) starts and ends.
    nuclei: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if not _is_vowel(text[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < len(text) and _is_vowel(text[index])                 and _forms_diphthong(text[index - 1], text[index]):
            index += 1
        nuclei.append((start, index))
    if not nuclei:
        return [text]

    parts: list[str] = []
    for position, (start, stop) in enumerate(nuclei):
        # The onset: consonants between the previous nucleus and this one.
        previous_end = nuclei[position - 1][1] if position else 0
        cluster = text[previous_end:start]
        if position == 0:
            onset = cluster
        else:
            onset = _onset_share(cluster)
            # Whatever the onset does not take closed the previous syllable.
            parts[-1] += cluster[:len(cluster) - len(onset)]
        coda = text[stop:] if position == len(nuclei) - 1 else ""
        parts.append(onset + text[start:stop] + coda)
    return [part for part in parts if part]


def _onset_share(cluster: str) -> str:
    """How much of a consonant run between vowels starts the next syllable."""
    if not cluster:
        return ""
    if len(cluster) >= 2 and cluster[-2:] in _CLUSTERS:
        return cluster[-2:]
    return cluster[-1:]


def _forms_diphthong(first: str, second: str) -> bool:
    """Two vowels share a syllable unless both are strong or the weak one is stressed."""
    if first in STRONG_VOWELS and second in STRONG_VOWELS:
        return False
    if first in "íú" or second in "íú":
        return False
    return True


def stressed_index(word: str) -> int:
    """Which syllable carries the stress, by the Spanish rules."""
    parts = syllables(word)
    if not parts:
        return -1
    for position, part in enumerate(parts):
        if any(ch in "áéíóú" for ch in part):
            return position
    plain = strip_accents(word.lower())
    # A word ending in a vowel, "n" or "s" is stressed on the next-to-last
    # syllable; anything else on the last.
    if plain and (plain[-1] in "aeiou" or plain[-1] in "ns"):
        return max(0, len(parts) - 2)
    return len(parts) - 1


def _consonant_ipa(text: str, index: int) -> tuple[str, int]:
    """The sound of the consonant at ``index``, and how many letters it used."""
    two = text[index:index + 2]
    following = text[index + 2] if len(text) > index + 2 else ""
    if two == "ch":
        return "tʃ", 2
    if two == "ll":
        return "ʎ", 2
    if two == "rr":
        return "r", 2
    if two == "qu":
        return "k", 2
    if two == "gu" and following in "eiéí":
        return "ɡ", 2
    if two == "gü":
        return "ɡw", 2
    letter = text[index]
    next_letter = text[index + 1] if len(text) > index + 1 else ""
    if letter == "c":
        return ("θ", 1) if next_letter in "eiéí" else ("k", 1)
    if letter == "g":
        return ("x", 1) if next_letter in "eiéí" else ("ɡ", 1)
    if letter == "z":
        return "θ", 1
    if letter == "j":
        return "x", 1
    if letter == "h":
        # Silent, except that "hu" before a vowel sounds like "w".
        if next_letter == "u" and len(text) > index + 2 and _is_vowel(text[index + 2]):
            return "w", 2
        return "", 1
    if letter == "v":
        return "b", 1
    if letter == "ñ":
        return "ɲ", 1
    if letter == "y":
        return ("ʝ", 1) if next_letter and _is_vowel(next_letter) else ("i", 1)
    if letter == "x":
        return ("s", 1) if next_letter and not _is_vowel(next_letter) else ("ks", 1)
    if letter == "r":
        # Trilled at the start of a word or after n, l, s.
        previous = text[index - 1] if index else ""
        return ("r", 1) if index == 0 or previous in "nls" else ("ɾ", 1)
    if letter == "w":
        return "w", 1
    return letter, 1


def ipa(word: str) -> str:
    """The IPA of a Spanish word, with the stress mark where the stress falls."""
    text = word.lower().strip()
    if not text or not any(_is_vowel(ch) for ch in text):
        return ""
    parts = syllables(text)
    stress = stressed_index(text)
    rendered: list[str] = []
    for position, part in enumerate(parts):
        sounds = ""
        index = 0
        while index < len(part):
            if _is_vowel(part[index]):
                sounds += ACCENTED.get(part[index], part[index])
                index += 1
                continue
            sound, used = _consonant_ipa(part, index)
            sounds += sound
            index += used
        rendered.append(("ˈ" if position == stress and len(parts) > 1 else "") + sounds)
    return "".join(rendered)


# The Spanish name of each letter, for reading an initialism aloud.
LETTER_NAMES = {
    "a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe", "g": "ge",
    "h": "hache", "i": "i", "j": "jota", "k": "ka", "l": "ele", "m": "eme",
    "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu", "r": "erre",
    "s": "ese", "t": "te", "u": "u", "v": "uve", "w": "uve doble", "x": "equis",
    "y": "i griega", "z": "zeta",
}


def spell_out(word: str) -> str:
    """"ONCE" -> "o ene ce e", for an initialism meant to be read letter by letter."""
    return " ".join(LETTER_NAMES.get(ch, ch) for ch in word.lower() if ch.isalpha())


# Letter sequences a Spanish voice reads differently from the language the word
# came from. Used to suggest a respelling for a foreign name.
_FOREIGN_HINTS = (
    # Longest first. Spanish has no "sh" or "th", so they go to the nearest
    # sound it does have; "w" becomes "gu", as in "Guáshington".
    ("tion", "sion"), ("tsch", "ch"), ("sch", "ch"), ("sh", "ch"), ("ph", "f"),
    ("th", "t"), ("wh", "gu"), ("ck", "c"), ("gh", "g"),
    ("ee", "i"), ("oo", "u"), ("ou", "au"), ("ea", "i"), ("ai", "ei"),
    ("ss", "s"), ("ll", "l"), ("w", "gu"), ("k", "c"), ("y", "i"), ("j", "y"),
    ("z", "s"),
)


def respell_foreign(word: str) -> str:
    """A first guess at Spanish spelling for a non-Spanish word.

    A guess, nothing more: only the ear can confirm a name. The interface
    offers it as a starting point and plays it before anything is saved.
    """
    text = word.lower().strip()
    if not text:
        return ""
    # Repeat until nothing changes: one substitution can create another
    # sequence Spanish does not use ("nietzsche" -> "nietzche" -> "nietche").
    for _pass in range(4):
        before = text
        for source, target in _FOREIGN_HINTS:
            text = text.replace(source, target)
        if text == before:
            break
    # A silent final "e" after a consonant is not silent in Spanish.
    text = re.sub(r"(?<=[bcdfgklmnprstv])e$", "", text)
    # An "h" that is not part of "ch" is silent in Spanish, so it only misleads.
    text = re.sub(r"(?<!c)h", "", text)
    text = re.sub(r"([bcdfgjklmnpqrstvxyz])+", r"", text)
    return text or word.lower()


# Spaces and hyphens are fine in a respelling: "o ene ce e" and "gui-fi" are
# both things a voice reads correctly.
_SPANISH_SHAPE = re.compile(r"^[a-záéíóúüñ][a-záéíóúüñ \-]*$")


def looks_spanish(word: str) -> bool:
    """True when the spelling is one Spanish rules can already read."""
    text = word.lower()
    if not _SPANISH_SHAPE.fullmatch(text):
        return False
    # Letter runs Spanish does not use. An "h" is only Spanish after a "c".
    return not re.search(r"(sh|sch|th|ph|ck|w|kk|tt|ee|oo|(?<!c)h)", text)


@dataclass
class Suggestion:
    """One way to respell a word, with why it is offered."""

    respelling: str
    label: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"respelling": self.respelling, "label": self.label, "reason": self.reason}


@dataclass
class WordPronunciation:
    """Everything the interface needs to show and fix one word."""

    word: str
    ipa: str
    syllables: list[str] = field(default_factory=list)
    stressed: int = -1
    looks_spanish: bool = True
    suggestions: list[Suggestion] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "word": self.word,
            "ipa": self.ipa,
            "syllables": self.syllables,
            "stressed": self.stressed,
            "looks_spanish": self.looks_spanish,
            "suggestions": [item.as_dict() for item in self.suggestions],
        }


def analyse(word: str) -> WordPronunciation:
    """Read a word the way Spanish rules do, and offer ways to respell it."""
    clean = word.strip()
    parts = syllables(clean)
    spanish = looks_spanish(clean)
    suggestions: list[Suggestion] = []

    if not spanish:
        guess = respell_foreign(clean)
        if guess and guess != clean.lower():
            suggestions.append(Suggestion(
                guess, "Adaptado al español",
                "Las letras que el español no usa se cambian por las que suenan igual."))
    if clean.isupper() and len(clean) <= 6 and clean.isalpha():
        suggestions.append(Suggestion(
            spell_out(clean), "Letra por letra",
            "Para una sigla que se lee deletreada, como «o ene ce e»."))
        suggestions.append(Suggestion(
            clean.lower(), "Como una palabra",
            "Para una sigla que se lee como palabra, como «once»."))
    # Only for a word Spanish can already read: splitting a foreign word into
    # Spanish syllables produces nonsense, not a pronunciation.
    if spanish and len(parts) > 2:
        suggestions.append(Suggestion(
            " ".join(parts), "Separado en sílabas",
            "Separar las sílabas ayuda cuando la voz acentúa la palabra donde no debe."))

    return WordPronunciation(
        word=clean,
        ipa=ipa(clean),
        syllables=parts,
        stressed=stressed_index(clean),
        looks_spanish=spanish,
        suggestions=suggestions,
    )
