"""Mastering: trimmed edges, deliberate pauses, one consistent loudness.

These are the three things that make a book sound like one reading rather than
a pile of clips, and each is measured rather than assumed. Everything here
needs ffmpeg, so the module skips without it.
"""

from __future__ import annotations

import subprocess

import pytest

from app import audio_master

pytestmark = pytest.mark.skipif(not audio_master.available(), reason="mastering needs ffmpeg")

SPEECH_SECONDS = 1.0


def _clip(freq: int, *, lead: float, tail: float, volume: float, tmp_path) -> bytes:
    """A tone wrapped in silence and set to a level, like a provider's clip."""
    target = tmp_path / f"clip-{freq}-{volume}.mp3"
    subprocess.run(
        [audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={SPEECH_SECONDS}",
         "-af", f"volume={volume},adelay={int(lead * 1000)}|{int(lead * 1000)},apad=pad_dur={tail}",
         "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-q:a", "2", str(target)],
        check=True, capture_output=True,
    )
    return target.read_bytes()


@pytest.fixture
def ragged(tmp_path) -> list[bytes]:
    """Three passages with different dead air and very different levels."""
    return [
        _clip(300, lead=0.6, tail=0.8, volume=0.9, tmp_path=tmp_path),
        _clip(400, lead=0.5, tail=0.7, volume=0.15, tmp_path=tmp_path),
        _clip(500, lead=0.7, tail=0.6, volume=0.5, tmp_path=tmp_path),
    ]


def test_dead_air_is_removed_and_the_pauses_are_the_ones_we_chose(ragged) -> None:
    raw = sum(audio_master.duration_seconds(clip) or 0 for clip in ragged)
    assert raw > 6.0, "the clips should start out padded with silence"

    gap_ms = 350
    joined = audio_master.join_passages(ragged, gap_ms=gap_ms)
    assert joined.mastered, joined.detail

    length = audio_master.duration_seconds(joined.audio) or 0
    # Three passages of speech, with a chosen pause after the first two.
    expected = 3 * SPEECH_SECONDS + 2 * gap_ms / 1000
    assert abs(length - expected) < 0.45, f"{length:.2f}s against an expected {expected:.2f}s"
    assert length < raw - 2.0, "most of the provider's padding should be gone"


def test_the_pause_length_is_what_was_asked_for(ragged) -> None:
    short = audio_master.join_passages(ragged, gap_ms=200)
    long = audio_master.join_passages(ragged, gap_ms=900)
    shorter = audio_master.duration_seconds(short.audio) or 0
    longer = audio_master.duration_seconds(long.audio) or 0
    # Two gaps, 700 ms longer each.
    assert abs((longer - shorter) - 1.4) < 0.2, f"{shorter:.2f}s then {longer:.2f}s"


def test_every_passage_reports_where_its_speech_sits(ragged) -> None:
    gap_ms = 350
    joined = audio_master.join_passages(ragged, gap_ms=gap_ms)
    assert len(joined.clips) == len(ragged)

    # Positions must march forward and never overlap, or a DAISY player would
    # highlight the wrong sentence.
    previous_end = 0.0
    for start, end in joined.clips:
        assert start >= previous_end - 0.01, joined.clips
        assert end > start, joined.clips
        previous_end = end

    # The speech of each passage, excluding its pause, is about a second.
    for start, end in joined.clips:
        assert abs((end - start) - SPEECH_SECONDS) < 0.3, joined.clips
    # The last clip ends at the end of the audio, because nothing follows it.
    total = audio_master.duration_seconds(joined.audio) or 0
    assert abs(joined.clips[-1][1] - total) < 0.2


def test_the_pause_is_not_counted_as_speech(ragged) -> None:
    gap_ms = 900
    joined = audio_master.join_passages(ragged, gap_ms=gap_ms)
    first_end, second_start = joined.clips[0][1], joined.clips[1][0]
    # The gap between one passage's speech and the next is the pause we chose,
    # so the text stops being highlighted while nobody is speaking.
    assert abs((second_start - first_end) - gap_ms / 1000) < 0.15


def test_loudness_is_brought_to_the_target(ragged) -> None:
    joined = audio_master.join_passages(ragged, gap_ms=350)
    for target in (-18.0, -23.0):
        result = audio_master.normalize_loudness(joined.audio, target_lufs=target)
        assert result.mastered, result.detail
        measured = audio_master.measure_loudness_lufs(result.audio)
        assert measured is not None
        # Within 3 dB on synthetic tones: loudnorm's gating and the meter
        # disagree on a continuous tone with long silences. Measured against
        # real narration from the provider the drift was 0.0 dB, which is the
        # case that matters and is checked in docs/pipeline-and-output.md.
        assert abs(measured - target) < 3.0, f"{measured} against a target of {target}"


def test_passages_levelled_separately_end_up_at_the_same_loudness(ragged) -> None:
    levels = []
    for clip in ragged:
        result = audio_master.normalize_loudness(clip, target_lufs=-18.0)
        if not result.mastered:
            pytest.skip("this clip's loudness could not be measured")
        levels.append(audio_master.measure_loudness_lufs(result.audio))
    # The point of normalising: the listener never reaches for the volume.
    assert max(levels) - min(levels) < 2.0, levels
    # And they started far apart.
    raw = [audio_master.measure_loudness_lufs(clip) for clip in ragged]
    assert max(raw) - min(raw) > 3.0, raw


def test_a_chapter_is_trimmed_paced_and_levelled_in_one_step(ragged) -> None:
    options = audio_master.MasteringOptions(target_lufs=-20.0, paragraph_gap_ms=400)
    result = audio_master.master_chapter(ragged, options)
    assert result.mastered, result.detail
    assert len(result.clips) == len(ragged)
    measured = audio_master.measure_loudness_lufs(result.audio)
    # Wider on tones than on speech, for the gating reason above.
    assert measured is not None and abs(measured - (-20.0)) < 3.0
    length = audio_master.duration_seconds(result.audio) or 0
    assert abs(length - (3 * SPEECH_SECONDS + 2 * 0.4)) < 0.5


def test_a_book_puts_a_longer_pause_between_chapters(ragged) -> None:
    options = audio_master.MasteringOptions(paragraph_gap_ms=300, chapter_gap_ms=1200)
    chapters = [audio_master.master_chapter(ragged, options).audio for _ in range(2)]
    book = audio_master.master_book(chapters, options)
    assert book.mastered, book.detail
    chapter_length = audio_master.duration_seconds(chapters[0]) or 0
    book_length = audio_master.duration_seconds(book.audio) or 0
    # Two chapters plus one pause between them.
    assert abs(book_length - (2 * chapter_length + 1.2)) < 0.4


def test_mastering_can_be_turned_off_without_losing_the_audio(ragged) -> None:
    options = audio_master.MasteringOptions(enabled=False)
    assert not options.active
    result = audio_master.master_chapter(ragged, options)
    assert not result.mastered
    assert result.audio == b"".join(ragged)
    assert "desactivado" in result.detail


def test_empty_input_is_refused_rather_than_producing_silence() -> None:
    with pytest.raises(ValueError):
        audio_master.join_passages([])
    with pytest.raises(ValueError):
        audio_master.normalize_loudness(b"")


def test_audio_that_cannot_be_measured_is_left_alone(ragged) -> None:
    # Silence has no loudness to correct towards, so it must come back
    # untouched rather than being amplified into noise.
    silence = subprocess.run(
        [audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2",
         "-c:a", "libmp3lame", "-q:a", "9", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    result = audio_master.normalize_loudness(silence, target_lufs=-18.0)
    assert not result.mastered
    assert result.audio == silence
    assert result.detail


def test_a_quiet_passage_is_kept_rather_than_trimmed_away(tmp_path, ragged) -> None:
    # Trimming a passage that is quiet throughout would delete a paragraph of
    # the book. It must survive, with only its pause added.
    quiet = subprocess.run(
        [audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1.5",
         "-c:a", "libmp3lame", "-q:a", "9", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    joined = audio_master.join_passages([ragged[0], quiet, ragged[1]], gap_ms=300)
    assert joined.mastered, joined.detail
    assert len(joined.clips) == 3
    # The quiet paragraph still occupies real time, so the text that goes with
    # it is still highlighted for a listener.
    start, end = joined.clips[1]
    assert end - start > 1.0, joined.clips
