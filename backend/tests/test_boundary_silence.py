"""ONCE: one second of digital silence before every audio file, more than one after.

Measured on the decoded waveform (silence detection and RMS of the edge
windows), never taken from what the code meant to do. Also covered: the audio
format is kept (sample rate, channels), the bitrate is constant, nothing
clips, no silence is added between sentences, and finishing a file twice adds
nothing the second time.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import audio_integrity, audio_master
from app.daisy_export import build_daisy_package, mp3_duration

pytestmark = pytest.mark.skipif(not audio_master.available(), reason="needs ffmpeg")

SILENT_DB = -60.0


def _tone(tmp_path: Path, name: str, *, seconds: float = 1.0, lead: float = 0.4, tail: float = 0.5,
          rate: int = 44100, channels: int = 2, volume: float = 0.6, vbr: bool = True) -> bytes:
    """A provider-like clip: a tone with some dead air of its own around it."""
    target = tmp_path / f"{name}.mp3"
    codec = ["-q:a", "2"] if vbr else ["-b:a", "128k"]
    subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-af", f"volume={volume},adelay={int(lead * 1000)}:all=1,apad=pad_dur={tail}",
                    "-ar", str(rate), "-ac", str(channels), "-c:a", "libmp3lame", *codec, str(target)],
                   check=True, capture_output=True)
    return target.read_bytes()


def _assert_edges(audio: bytes, lead: float, tail: float) -> None:
    duration = mp3_duration(audio)
    edges = audio_master.edge_silence(audio)
    assert edges is not None
    assert edges.leading >= lead - 0.03, edges
    assert edges.trailing >= tail - 0.03, edges
    # RMS of the edge windows: digital silence, not quiet speech.
    start = audio_master.window_level_db(audio, 0.0, lead - 0.05)
    end = audio_master.window_level_db(audio, duration - tail + 0.05, tail - 0.05)
    assert start is not None and start <= SILENT_DB, start
    assert end is not None and end <= SILENT_DB, end


def test_a_finished_file_starts_with_one_second_and_ends_with_more(tmp_path) -> None:
    clip = _tone(tmp_path, "clip", lead=0.0, tail=0.0)
    options = audio_master.MasteringOptions()
    assert options.edges == (1.0, 1.2)
    finished = audio_master.finish_file(clip, options)
    assert finished.finished, finished.detail
    _assert_edges(finished.audio, 1.0, 1.2)
    # The speech itself is still there, at its level.
    speech = audio_master.window_level_db(finished.audio, 1.1, 0.8)
    assert speech is not None and speech > -40, speech
    # Exactly what was missing was added: about 1.0 s and 1.2 s.
    assert finished.lead_added == pytest.approx(1.0, abs=0.04)
    assert finished.tail_added == pytest.approx(1.2, abs=0.04)
    assert mp3_duration(finished.audio) == pytest.approx(mp3_duration(clip) + finished.lead_added
                                                         + finished.tail_added, abs=0.06)


def test_the_silence_values_are_settings(tmp_path) -> None:
    clip = _tone(tmp_path, "clip", lead=0.0, tail=0.0)
    longer = audio_master.finish_file(clip, audio_master.MasteringOptions(leading_silence_ms=1500,
                                                                          trailing_silence_ms=2000))
    _assert_edges(longer.audio, 1.5, 2.0)
    none = audio_master.finish_file(clip, audio_master.MasteringOptions(leading_silence_ms=0,
                                                                        trailing_silence_ms=0))
    assert none.lead_added == 0 and none.tail_added == 0
    assert mp3_duration(none.audio) == pytest.approx(mp3_duration(clip), abs=0.06)


def test_finishing_twice_adds_nothing_the_second_time(tmp_path) -> None:
    clip = _tone(tmp_path, "clip", lead=0.0, tail=0.0)
    options = audio_master.MasteringOptions()
    once = audio_master.finish_file(clip, options)
    twice = audio_master.finish_file(once.audio, options)
    # Already in the delivery format with its silence: returned byte for byte.
    assert twice.audio == once.audio
    assert twice.lead_added == 0 and twice.tail_added == 0


def test_silence_already_there_is_topped_up_not_stacked(tmp_path) -> None:
    # A clip that already starts with 0.4 s and ends with 0.5 s of silence.
    clip = _tone(tmp_path, "padded", lead=0.4, tail=0.5)
    finished = audio_master.finish_file(clip, audio_master.MasteringOptions())
    assert finished.lead_added == pytest.approx(0.6, abs=0.06)
    assert finished.tail_added == pytest.approx(0.7, abs=0.06)
    edges = audio_master.edge_silence(finished.audio)
    # About the target, not 1.4 s and 1.7 s.
    assert edges.leading < 1.15 and edges.trailing < 1.35, edges


def test_the_format_is_kept_and_the_bitrate_is_constant(tmp_path) -> None:
    for rate, channels in ((44100, 2), (22050, 1)):
        clip = _tone(tmp_path, f"clip-{rate}", rate=rate, channels=channels)
        finished = audio_master.finish_file(clip, audio_master.MasteringOptions())
        stream = audio_integrity.inspect_stream(finished.audio)
        assert stream["sample_rates"] == [rate]
        assert stream["channels"] == [channels]
        assert stream["bitrates_kbps"] == [128]
        assert not stream["errors"]


def test_nothing_clips(tmp_path) -> None:
    loud = _tone(tmp_path, "loud", volume=1.0, lead=0.0, tail=0.0)
    chapter = audio_master.master_chapter([loud, loud], audio_master.MasteringOptions())
    peak = audio_master.peak_level_db(chapter.audio)
    # Levelled with a -2 dBTP ceiling; the silence added cannot raise it.
    assert peak is not None and peak < -0.5, peak


def test_no_silence_is_added_between_sentences(tmp_path) -> None:
    passages = [_tone(tmp_path, f"p{index}") for index in range(3)]
    options = audio_master.MasteringOptions(paragraph_gap_ms=1000)
    chapter = audio_master.master_chapter(passages, options)
    assert chapter.mastered, chapter.detail
    # Between paragraphs: the paragraph pause, not the file-edge silence too.
    for (_begin, end), (following, _end) in zip(chapter.clips, chapter.clips[1:]):
        assert following - end == pytest.approx(1.0, abs=0.06)
    _assert_edges(chapter.audio, 1.0, 1.2)


def test_the_chapters_and_the_complete_audiobook_are_handled_alike(tmp_path) -> None:
    passages = [_tone(tmp_path, f"p{index}") for index in range(4)]
    chapters = [("Uno", ["Uno.", "Dos."]), ("Dos", ["Tres.", "Cuatro."])]
    package = build_daisy_package("Libro", ["Uno.", "Dos.", "Tres.", "Cuatro."], passages, tmp_path / "pkg",
                                  chapters=chapters, mastering=audio_master.MasteringOptions())
    chapter_files = sorted((package / "audio").glob("chapter-*.mp3"))
    assert len(chapter_files) == 2
    for path in chapter_files + [package / "Libro.mp3"]:
        _assert_edges(path.read_bytes(), 1.0, 1.2)
    # The complete audiobook is exactly its chapters, copied.
    book = mp3_duration((package / "Libro.mp3").read_bytes())
    assert book == pytest.approx(sum(mp3_duration(path.read_bytes()) for path in chapter_files), abs=0.002)


def test_a_package_built_again_from_finished_chapters_adds_no_silence(tmp_path) -> None:
    passages = [_tone(tmp_path, f"p{index}") for index in range(2)]
    options = audio_master.MasteringOptions()
    chapter = audio_master.master_chapter(passages, options)
    package = build_daisy_package("Libro", ["Uno.", "Dos."], passages, tmp_path / "pkg",
                                  chapters=[("Uno", ["Uno.", "Dos."])], chapter_audio=[chapter.audio],
                                  chapter_clips=[chapter.clips], mastering=options, use_ffmpeg=True)
    delivered = (package / "audio" / "chapter-001.mp3").read_bytes()
    assert delivered == chapter.audio
