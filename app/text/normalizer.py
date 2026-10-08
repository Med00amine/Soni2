"""Non-destructive text normalization for speech synthesis."""

import re
import unicodedata

from .lexicon import PronunciationLexicon


class TextNormalizer:
    """Produce TTS text while retaining the original source text on the model."""

    def __init__(self, lexicon: PronunciationLexicon | None = None) -> None:
        self.lexicon = lexicon or PronunciationLexicon()

    def normalize(self, text: str) -> str:
        normalized = unicodedata.normalize("NFC", text)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return self.lexicon.resolve(normalized)

