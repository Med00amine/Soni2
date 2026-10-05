"""Deterministic synchronization based on measured audio durations."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from app.ingestion.models import AudioMetadata, Sentence, SynchronizationPoint


class SynchronizationError(ValueError):
    """Raised when text and audio cannot be synchronized safely."""


class SynchronizationEngine:
    """Create sequential clip ranges for sentence-level audio files."""

    def synchronize(
        self,
        sentences: Iterable[Sentence],
        audio: Mapping[str, AudioMetadata],
    ) -> list[SynchronizationPoint]:
        points: list[SynchronizationPoint] = []
        elapsed = 0.0
        previous_audio: Path | None = None
        seen: set[str] = set()
        for sentence in sentences:
            if sentence.id in seen:
                raise SynchronizationError(f"Duplicate text ID: {sentence.id}")
            seen.add(sentence.id)
            metadata = audio.get(sentence.id)
            if metadata is None:
                raise SynchronizationError(f"Missing audio segment for text ID: {sentence.id}")
            if metadata.duration_seconds <= 0:
                raise SynchronizationError(f"Audio has no duration for text ID: {sentence.id}")
            if previous_audio is not None and Path(metadata.path) != previous_audio:
                elapsed = 0.0
            begin = elapsed
            elapsed += metadata.duration_seconds
            previous_audio = Path(metadata.path)
            points.append(
                SynchronizationPoint(
                    text_id=sentence.id,
                    audio_file=Path(metadata.path),
                    clip_begin=begin,
                    clip_end=elapsed,
                )
            )
        return points
