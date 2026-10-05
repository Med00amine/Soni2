"""Stable TTS engine interface."""

from abc import ABC, abstractmethod
from pathlib import Path


class TTSEngine(ABC):
    """Engine contract used by the pipeline, independent of model vendor."""

    @abstractmethod
    def synthesize(self, text: str, output_path: Path, voice_id: str | None = None) -> Path:
        """Synthesize text to a lossless audio file and return its path."""

