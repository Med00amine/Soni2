"""Stable TTS engine interface."""

from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SynthesisMetrics:
    """Operational measurements for one synthesis request."""

    sentence_id: str | None
    text_length: int
    model: str
    device: str
    synthesis_seconds: float
    audio_seconds: float
    retries: int

    @property
    def real_time_factor(self) -> float:
        return self.synthesis_seconds / self.audio_seconds if self.audio_seconds else float("inf")


class TTSEngine(ABC):
    """Engine contract used by the pipeline, independent of model vendor."""

    @abstractmethod
    def synthesize(self, text: str, output_path: Path, voice_id: str | None = None) -> Path:
        """Synthesize text to a lossless audio file and return its path."""

    @property
    def last_metrics(self) -> SynthesisMetrics | None:
        """Return metrics for the most recent synthesis request, when available."""
        return None
