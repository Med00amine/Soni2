"""Sentence segmentation with conservative punctuation rules."""

import re


class SentenceSegmenter:
    """Split plain text without losing terminal punctuation."""

    _boundary = re.compile(r"(?<=[.!?])(?:[\"»”']+)?\s+")

    def segment(self, text: str) -> list[str]:
        cleaned = " ".join(text.split())
        if not cleaned:
            return []
        return [part.strip() for part in self._boundary.split(cleaned) if part.strip()]

