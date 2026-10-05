"""Configurable, boundary-aware pronunciation lexicon."""

import re
from collections.abc import Mapping


class PronunciationLexicon:
    """Resolve configured source terms without coupling normalization to a file format."""

    def __init__(self, entries: Mapping[str, str] | None = None) -> None:
        self._entries = dict(entries or {})

    def resolve(self, text: str) -> str:
        for source, pronunciation in sorted(self._entries.items(), key=lambda item: -len(item[0])):
            escaped = re.escape(source)
            pattern = rf"(?<!\w){escaped}(?!\w)"
            text = re.sub(pattern, pronunciation, text)
        return text
