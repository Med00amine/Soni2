"""Audio integrity, DAISY DTD validity and the source-to-sync completeness chain."""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import audio_integrity, completeness, daisy_dtd
from app.audio_master import MasteringOptions
from app.daisy_export import build_daisy_package

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required to make real audio")
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413  # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz


def _tone(path: Path, seconds: float, *, silence_after: float = 0.3, gap: float = 0.0, frequency: int = 440,
          volume: float = 0.3) -> bytes:
    """Speech-like audio: a tone, an optional silent gap inside it, a quiet tail.

    Silent parts are only added when they have a length: an atrim of zero on
    an endless anullsrc means "no limit" and ffmpeg would never stop.
    """
    def tone(label: str, length: float) -> str:
        return f"sine=sample_rate=44100:frequency={frequency}:duration={length},volume={volume}[{label}]"

    def silence(label: str, length: float) -> str:
        return f"anullsrc=r=44100:cl=mono,atrim=duration={length}[{label}]"

    parts, labels = [tone("a", seconds / 2)], ["a"]
    if gap > 0:
        parts.append(silence("g", gap))
        labels.append("g")
    parts.append(tone("b", seconds / 2))
    labels.append("b")
    if silence_after > 0:
        parts.append(silence("t", silence_after))
        labels.append("t")
    graph = ";".join(parts) + ";" + "".join(f"[{label}]" for label in labels) + \
        f"concat=n={len(labels)}:v=0:a=1[out]"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-filter_complex", graph,
                    "-map", "[out]", "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
                   check=True, timeout=60)
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# Stream checks (no ffmpeg needed)
# --------------------------------------------------------------------------- #

def test_a_valid_stream_is_counted_frame_by_frame() -> None:
    stream = audio_integrity.inspect_stream(FRAME * 100)
    assert stream["frames"] == 100 and stream["errors"] == []
    assert stream["sample_rates"] == [44100] and stream["duration_seconds"] == pytest.approx(2.612, abs=0.001)


# MPEG-2 Layer III, 80 kbit/s, 22.05 kHz: 261-byte frames.
FRAME_22K = b"\xff\xf3\x90\x64" + b"\x00" * 257


@pytest.mark.parametrize("payload,error", [
    (b"", "empty"),
    (b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"not audio at all" * 10, "no_audio_frames"),
    (FRAME * 10 + b"\x13" * 2000 + FRAME * 10, "corrupt_frames"),
    (FRAME * 10 + FRAME_22K * 10, "mixed_sample_rates"),
], ids=["empty", "no-frames", "junk-between-frames", "mixed-rates"])
def test_corrupt_streams_fail(payload, error) -> None:
    assert error in audio_integrity.inspect_stream(payload)["errors"]
    assert audio_integrity.assess(payload, "Hola.", decode=False)["status"] == "failed"


def test_a_truncated_last_frame_is_reported() -> None:
    assert "truncated_final_frame" in audio_integrity.inspect_stream(FRAME * 10 + FRAME[:100])["errors"]


# --------------------------------------------------------------------------- #
# Signal checks (ffmpeg)
# --------------------------------------------------------------------------- #

@needs_ffmpeg
def test_speech_like_audio_of_the_right_length_passes(tmp_path) -> None:
    text = "Aquella mañana de otoño la niebla subía despacio."  # 41 letters, ~3.5 s at 12/s
    result = audio_integrity.assess(_tone(tmp_path / "ok.mp3", 3.4), text)
    assert result["status"] == "passed", result
    assert 8 <= result["chars_per_second"] <= 16


@needs_ffmpeg
def test_a_long_silence_inside_a_passage_is_flagged(tmp_path) -> None:
    result = audio_integrity.assess(_tone(tmp_path / "gap.mp3", 3.0, gap=2.9), "Hola, ¿qué tal estás hoy, amigo mío?")
    assert "abnormal_silence" in result["reasons"]


@needs_ffmpeg
def test_audio_far_too_short_for_its_text_is_flagged(tmp_path) -> None:
    # Two seconds for a whole paragraph: words are missing.
    text = "Cuando por fin llegó la carta, tres semanas después de lo prometido, nadie se atrevió a abrirla."
    result = audio_integrity.assess(_tone(tmp_path / "short.mp3", 2.0), text)
    assert "speech_too_fast_for_text" in result["reasons"]


@needs_ffmpeg
def test_audio_cut_off_mid_word_is_flagged(tmp_path) -> None:
    result = audio_integrity.assess(_tone(tmp_path / "cut.mp3", 3.0, silence_after=0.0, volume=0.9),
                                    "Aquella mañana de otoño la niebla.")
    assert "abrupt_end" in result["reasons"]


# --------------------------------------------------------------------------- #
# DAISY: DTDs and the completeness chain
# --------------------------------------------------------------------------- #

CHAPTERS = [
    {"title": "Capítulo 1. La llegada", "paragraphs": ["Amanecía despacio.", "El aire olía a tierra."]},
    {"title": "Página 2", "title_generated": True, "paragraphs": ["La casa tenía ventanas."]},
]


def _units(audio: list[bytes], job_dir: Path) -> list[dict]:
    texts = [(0, "Capítulo 1. La llegada"), (0, "Amanecía despacio."), (0, "El aire olía a tierra."),
             (1, "La casa tenía ventanas.")]
    units = []
    for index, ((chapter, text), payload) in enumerate(zip(texts, audio)):
        path = job_dir / f"u{index}.mp3"
        path.write_bytes(payload)
        units.append({"sentence_id": f"s{index}", "chapter_index": chapter, "text": text, "state": "verified",
                      "audio_path": path.name})
    return units


def _package(tmp_path: Path, audio: list[bytes]) -> Path:
    chapters = [("Capítulo 1. La llegada", ["Capítulo 1. La llegada", "Amanecía despacio.", "El aire olía a tierra."]),
                ("Página 2", ["La casa tenía ventanas."])]
    return build_daisy_package("El faro", [p for _, body in chapters for p in body], audio, tmp_path / "pkg",
                               chapters=chapters, mastering=MasteringOptions(enabled=bool(FFMPEG)))


@needs_ffmpeg
def test_a_built_package_passes_the_dtds_and_the_whole_completeness_chain(tmp_path) -> None:
    audio = [_tone(tmp_path / f"t{index}.mp3", 1.5) for index in range(4)]
    package = _package(tmp_path, audio)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    result = completeness.verify(CHAPTERS, _units(audio, tmp_path), tmp_path, package, set())
    assert result["status"] == "passed", json.dumps(result, ensure_ascii=False, indent=1)
    assert [check["id"] for check in result["checks"]] == [
        "source_to_units", "units_to_audio", "units_to_dtbook", "smil_sync", "audio_format", "navigation"]


def test_a_missing_duplicated_or_reordered_paragraph_breaks_the_chain(tmp_path) -> None:
    units = _units([FRAME * 40] * 4, tmp_path)
    assert completeness.source_to_units(CHAPTERS, units, set())["status"] == "passed"
    missing = completeness.source_to_units(CHAPTERS, units[:2] + units[3:], set())
    assert missing["status"] == "failed" and missing["evidence"]["problems"][0]["kind"] == "missing"
    duplicated = completeness.source_to_units(CHAPTERS, units + [units[1]], set())
    assert duplicated["status"] == "failed"
    reordered = completeness.source_to_units(CHAPTERS, [units[0], units[2], units[1], units[3]], set())
    assert reordered["status"] == "failed"
    # A reviewer's logged correction is not a discrepancy.
    corrected = [dict(unit) for unit in units]
    corrected[1]["text"] = "Amanecía muy despacio."
    assert completeness.source_to_units(CHAPTERS, corrected, {"s1"})["status"] == "passed"
    assert completeness.source_to_units(CHAPTERS, corrected, set())["status"] == "failed"


def test_an_invented_title_is_not_expected_to_be_narrated() -> None:
    expected = [text for _chapter, text, _heading in completeness.expected_units(CHAPTERS)]
    assert "Página 2" not in expected and "Capítulo 1. La llegada" in expected


def test_unverified_or_missing_audio_breaks_the_chain(tmp_path) -> None:
    units = _units([FRAME * 40] * 4, tmp_path)
    units[2]["state"] = "needs_human_review"
    (tmp_path / units[3]["audio_path"]).unlink()
    check = completeness.units_to_audio(units, tmp_path)
    assert check["status"] == "failed"
    assert {problem["sentence_id"] for problem in check["evidence"]["problems"]} == {"s2", "s3"}


def test_a_clip_beyond_its_audio_or_a_dtbook_out_of_order_is_caught(tmp_path) -> None:
    units = _units([FRAME * 40] * 4, tmp_path)
    package = _package(tmp_path, [FRAME * 40] * 4)
    smil = package / "smil" / "chapter-0001.smil"
    smil.write_text(smil.read_text(encoding="utf-8").replace('clipEnd="0:00:01.045"', 'clipEnd="0:00:09.000"', 1),
                    encoding="utf-8")
    checks = {check["id"]: check for check in completeness.units_to_package(units, package)}
    assert checks["smil_sync"]["status"] == "failed"
    dtbook = package / "El_faro.xml"
    dtbook.write_text(dtbook.read_text(encoding="utf-8").replace("Amanecía despacio.", "El aire olía a tierra.", 1),
                      encoding="utf-8")
    checks = {check["id"]: check for check in completeness.units_to_package(units, package)}
    assert checks["units_to_dtbook"]["status"] == "failed"


def test_the_dtds_catch_a_package_a_reader_could_not_parse(tmp_path) -> None:
    package = _package(tmp_path, [FRAME * 40] * 4)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    dtbook = package / "El_faro.xml"
    root = ET.parse(dtbook).getroot()
    # The defect this upgrade fixed: every element written as <ns0:…>.
    # ElementTree, with no default namespace registered, writes the prefix ns0.
    dtbook.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
                      encoding="utf-8")
    result = daisy_dtd.validate_package(package)
    assert result["status"] == "failed" and any("El_faro.xml" in error for error in result["errors"])


@needs_ffmpeg
def test_joining_without_ffmpeg_still_gives_one_clean_stream(tmp_path) -> None:
    # Provider files carry an ID3 tag and a Xing/Info length frame each, as
    # ElevenLabs' do. Glued byte by byte they put tags inside the audio, which
    # the integrity check rightly rejects; only the frames are joined now.
    from app.daisy_export import _join_mp3, mp3_duration

    tagged = []
    for index in range(3):
        path = tmp_path / f"t{index}.mp3"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-metadata", "title=Soni2 prueba", "-id3v2_version", "3",
                        "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True, timeout=60)
        tagged.append(path.read_bytes())
    assert tagged[0][:3] == b"ID3"
    assert audio_integrity.inspect_stream(b"".join(tagged))["errors"] == ["corrupt_frames"]
    joined = _join_mp3(tagged, use_ffmpeg=False)
    stream = audio_integrity.inspect_stream(joined)
    assert stream["errors"] == [] and stream["skipped_bytes"] == 0
    assert abs(stream["duration_seconds"] - 3.0) < 0.2
    assert abs(mp3_duration(joined) - stream["duration_seconds"]) < 0.01
