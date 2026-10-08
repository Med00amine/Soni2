from pathlib import Path

import pytest

from app.ingestion.models import AudioMetadata, Sentence
from app.synchronization.synchronizer import SynchronizationEngine, SynchronizationError


def _sentence(identifier: str) -> Sentence:
    return Sentence(id=identifier, text=identifier, paragraph_id="p", chapter_id="c", order=1)


def _audio(path: str, duration: float) -> AudioMetadata:
    return AudioMetadata(
        path=Path(path), duration_seconds=duration, sample_rate=16_000,
        channels=1, sample_width=2, frame_count=int(duration * 16_000),
    )


def test_synchronizer_accumulates_segments_in_one_audio_stream() -> None:
    points = SynchronizationEngine().synchronize(
        [_sentence("one"), _sentence("two")],
        {"one": _audio("chapter.wav", 1.25), "two": _audio("chapter.wav", 2.5)},
    )

    assert (points[0].clip_begin, points[0].clip_end) == (0.0, 1.25)
    assert (points[1].clip_begin, points[1].clip_end) == (1.25, 3.75)


def test_synchronizer_rejects_missing_audio() -> None:
    with pytest.raises(SynchronizationError, match="Missing audio"):
        SynchronizationEngine().synchronize([_sentence("one")], {})
