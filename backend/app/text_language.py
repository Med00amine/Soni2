"""Guess the language of a passage so speech review listens for the right one.

Books for Spanish readers often carry English: a bilingual reader's translations,
an English foreword, quotations. Transcribing English narration as Spanish turns
correct audio into nonsense, and every such paragraph used to land in the human
review queue. Only distinctive, high-frequency words count as evidence; words
both languages share ("a", "no", "me", "he", "son") count for neither.
"""

from __future__ import annotations

import re

_SPANISH = frozenset("""
el la los las del de que y en un una unos unas es por con para se su sus al lo como más mas pero muy
también tambien cuando donde dónde porque está esta están estan este esto ese esa eso estos estas fue
hay ya yo tú tu él ella ellos ellas nosotros nos mi mis qué cómo sin sobre entre hasta desde todo
todos toda nada algo aquí aqui ahora siempre nunca bien sí gracias hola señor señora pues entonces
había habia tiene tienen hace muy cuál cual quién quien también año años día días
""".split())

_ENGLISH = frozenset("""
the of and to is you that it was for on are with his they at be this have from or had by but not what
all were we when your can said there an each which she do how their if will up about out many then them
these so some her would make like him into look more been who its now could people my than first did
get just our over also after because very through where much before here story translation chapter
i'm don't it's that's what's isn't
""".split())

_ACCENTED = re.compile(r"[áéíóúñ¿¡]")
_WORD = re.compile(r"[a-záéíóúüñ']+")


def recognition_language(text: str) -> str:
    """``es``, ``en`` or ``multi`` (Deepgram's code-switching mode) for one passage."""
    lowered = text.casefold()
    words = _WORD.findall(lowered)
    spanish = sum(word in _SPANISH for word in words) + len(_ACCENTED.findall(lowered))
    english = sum(word in _ENGLISH for word in words)
    if spanish + english < 3:
        # Too little to tell: the book is Spanish.
        return "es"
    share = english / (spanish + english)
    if share >= 0.8:
        return "en"
    if share <= 0.2:
        return "es"
    return "multi"
