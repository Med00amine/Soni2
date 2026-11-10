"""SMIL timing and synchronisation, checked against the real audio of a built package.

Every SMIL audio reference must point at an existing audio file and text
element, with valid clock values, clipEnd after clipBegin and never past the
decoded end of the file; the clips must tile each file with no gap or overlap;
dtb:totalElapsedTime, the seq dur and dtb:totalTime must be the sums of the
clips; the audio must be constant bitrate. Each rule is shown to pass on a
built package and to fail when the package is broken in exactly that way.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audio_master, daisy_timing, main, providers
from app import jobs as job_store
from app.audio_cache import AudioCache
from app.daisy_export import build_daisy_package, mp3_duration, smil_boundaries
from app.daisy_timing import parse_clock

SMIL = "{http://www.w3.org/2001/SMIL20/}"
needs_ffmpeg = pytest.mark.skipif(not audio_master.available(), reason="needs ffmpeg")
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413  # one silent 128 kbit/s frame


# --------------------------------------------------------------------------- #
# Clock values and clip geometry
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, seconds", [
    ("0:00:01.567", 1.567), ("12:01:02.5", 43262.5), ("01:02.250", 62.25), ("npt=3.5s", 3.5),
    ("1500ms", 1.5), ("2min", 120.0), ("0.5h", 1800.0), ("47", 47.0),
])
def test_smil_clock_values_are_read_as_the_standard_writes_them(value, seconds) -> None:
    assert parse_clock(value) == pytest.approx(seconds)


@pytest.mark.parametrize("value", ["", "1:2:3", "0:61:00", "00:00:60", "1,5s", "abc", None, "0:00:01. 5"])
def test_invalid_clock_values_are_refused(value) -> None:
    assert parse_clock(value) is None


def test_clips_tile_the_file_from_its_first_sample_to_its_last() -> None:
    # Speech of three passages inside a 9.4567 s file: 1.0-2.5, 3.5-5.0, 6.0-8.2.
    boundaries = smil_boundaries([(1.0, 2.5), (3.5, 5.0), (6.0, 8.2)], 9.4567)
    assert boundaries == [(0, 3500), (3500, 6000), (6000, 9456)]
    # The end is rounded down: never a millisecond past the audio.
    assert boundaries[-1][1] <= 9.4567 * 1000


def test_clip_boundaries_stay_strictly_increasing_even_for_odd_input() -> None:
    boundaries = smil_boundaries([(0.0, 0.1), (0.0, 0.1), (0.05, 0.2)], 0.3)
    assert boundaries == [(0, 1), (1, 50), (50, 300)]
    with pytest.raises(ValueError):
        smil_boundaries([(0, 0.001)] * 5, 0.003)


# --------------------------------------------------------------------------- #
# A built package passes every check
# --------------------------------------------------------------------------- #

def _tone(tmp_path: Path, name: str, seconds: float, frequency: int = 440) -> bytes:
    target = tmp_path / f"{name}.mp3"
    subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={frequency}:duration={seconds}",
                    "-af", "adelay=300:all=1,apad=pad_dur=0.4", "-ar", "44100", "-ac", "2",
                    "-c:a", "libmp3lame", "-q:a", "2", str(target)], check=True, capture_output=True)
    return target.read_bytes()


def _targets() -> dict:
    options = audio_master.MasteringOptions()
    lead, tail = options.edges
    return {"leading": lead, "trailing": tail, "bitrate_kbps": options.bitrate_kbps,
            "chapter_gap": options.chapter_gap_ms / 1000}


@pytest.fixture
def built(tmp_path) -> Path:
    if not audio_master.available():
        pytest.skip("needs ffmpeg")
    # Passages of different lengths, in three chapters.
    lengths = [0.8, 1.6, 0.5, 2.2, 1.1, 0.9]
    audio = [_tone(tmp_path, f"p{index}", length, 300 + 50 * index) for index, length in enumerate(lengths)]
    chapters = [("Capítulo 1", ["Capítulo 1", "Uno.", "Dos."]), ("Capítulo 2", ["Capítulo 2", "Tres."]),
                ("Capítulo 3", ["Cuatro."])]
    return build_daisy_package("Libro de tiempos", [text for _, body in chapters for text in body], audio,
                               tmp_path / "pkg", chapters=chapters, mastering=audio_master.MasteringOptions())


def _status(checks: list[dict]) -> dict[str, str]:
    return {check["id"]: check["status"] for check in checks}


@needs_ffmpeg
def test_a_built_package_passes_every_timing_check(built) -> None:
    checks = daisy_timing.verify(built, **_targets())
    assert set(_status(checks).values()) == {"passed"}, json.dumps(checks, ensure_ascii=False, indent=1)
    totals = next(check for check in checks if check["id"] == "timing_totals")
    chapters = sorted((built / "audio").glob("chapter-*.mp3"))
    # The playing time of the book is the playing time of its audio files.
    assert totals["total_seconds"] == pytest.approx(sum(mp3_duration(path.read_bytes()) for path in chapters),
                                                    abs=0.004)


@needs_ffmpeg
def test_every_clip_ends_within_its_audio_however_its_length_is_measured(built) -> None:
    for smil in sorted((built / "smil").glob("*.smil")):
        audio = list(ET.parse(smil).getroot().iter(f"{SMIL}audio"))
        path = (smil.parent / audio[0].get("src")).resolve()
        data = path.read_bytes()
        frames = mp3_duration(data)
        # Constant bitrate: size over bitrate gives the same length as the frames.
        estimate = len(data) * 8 / 128_000
        decoded = audio_master.edge_silence(data).duration
        for item in audio:
            end = parse_clock(item.get("clipEnd"))
            assert end <= frames and end <= decoded + 0.001 and end <= estimate + 0.03
        assert parse_clock(audio[0].get("clipBegin")) == 0


# --------------------------------------------------------------------------- #
# Each rule fails when the package is broken that way
# --------------------------------------------------------------------------- #

def _edit_smil(package: Path, name: str, change) -> None:
    path = package / "smil" / name
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")


def _failed(package: Path) -> set[str]:
    return {check["id"] for check in daisy_timing.verify(package, **_targets()) if check["status"] == "failed"}


@needs_ffmpeg
def test_a_gap_between_clips_is_reported(built) -> None:
    def gap(text: str) -> str:
        begins = re.findall(r'clipBegin="([^"]+)"', text)
        return text.replace(f'clipBegin="{begins[1]}"', 'clipBegin="0:00:02.900"', 1)

    _edit_smil(built, "chapter-0001.smil", gap)
    failed = _failed(built)
    assert "timing_contiguity" in failed


@needs_ffmpeg
def test_a_clip_ending_after_its_audio_is_reported(built) -> None:
    def overrun(text: str) -> str:
        ends = re.findall(r'clipEnd="([^"]+)"', text)
        return text.replace(f'clipEnd="{ends[-1]}"', 'clipEnd="0:00:59.000"')

    _edit_smil(built, "chapter-0002.smil", overrun)
    assert {"timing_clips", "timing_totals"} <= _failed(built)


@needs_ffmpeg
def test_clip_end_before_its_begin_and_bad_clock_values_are_reported(built) -> None:
    _edit_smil(built, "chapter-0003.smil", lambda text: re.sub(r'clipEnd="[^"]+"', 'clipEnd="0:00:00.000"', text))
    assert "timing_clips" in _failed(built)
    _edit_smil(built, "chapter-0003.smil", lambda text: re.sub(r'clipEnd="[^"]+"', 'clipEnd="1,5s"', text))
    assert "timing_clips" in _failed(built)


@needs_ffmpeg
def test_wrong_elapsed_time_and_total_time_are_reported(built) -> None:
    _edit_smil(built, "chapter-0002.smil",
               lambda text: re.sub(r'(dtb:totalElapsedTime" content=")[^"]+', r"\g<1>0:00:00.000", text))
    checks = {check["id"]: check for check in daisy_timing.verify(built, **_targets())}
    assert checks["timing_totals"]["status"] == "failed"
    assert any("totalElapsedTime" in problem["problem"] for problem in checks["timing_totals"]["problems"])
    opf = next(built.glob("*.opf"))
    opf.write_text(re.sub(r'(dtb:totalTime" content=")[^"]+', r"\g<1>9:00:00.000", opf.read_text(encoding="utf-8")),
                   encoding="utf-8")
    checks = {check["id"]: check for check in daisy_timing.verify(built, **_targets())}
    assert any("totalTime" in problem["problem"] for problem in checks["timing_totals"]["problems"])


@needs_ffmpeg
def test_a_missing_audio_file_or_text_element_is_reported(built) -> None:
    _edit_smil(built, "chapter-0001.smil", lambda text: text.replace("#p-0002", "#no-such-paragraph"))
    (built / "audio" / "chapter-003.mp3").unlink()
    checks = {check["id"]: check for check in daisy_timing.verify(built, **_targets())}
    problems = " ".join(problem["problem"] for problem in checks["timing_clips"]["problems"])
    assert "no-such-paragraph" in problems and "falta el audio" in problems


@needs_ffmpeg
def test_variable_bitrate_and_missing_edges_are_reported(tmp_path) -> None:
    # Built the old way: no finishing, so the provider's VBR clips are joined as they are.
    audio = [_tone(tmp_path, f"v{index}", 1.0 + index) for index in range(2)]
    package = build_daisy_package("Viejo", ["Uno.", "Dos."], audio, tmp_path / "old",
                                  chapters=[("Uno", ["Uno.", "Dos."])], use_ffmpeg=False)
    checks = {check["id"]: check for check in daisy_timing.verify(package, **_targets())}
    assert checks["timing_audio_format"]["status"] == "failed"
    assert "VBR" in checks["timing_audio_format"]["problems"][0]["problem"]
    assert checks["timing_edges"]["status"] == "failed"


def test_the_old_geometry_with_pauses_outside_the_clips_is_reported(tmp_path) -> None:
    """The delivered layout before this change: a second of pause between clips."""
    package = build_daisy_package("Viejo", ["Uno.", "Dos."], [FRAME * 40, FRAME * 40], tmp_path / "pkg",
                                  chapters=[("Uno", ["Uno.", "Dos."])])

    def old(text: str) -> str:
        # The first clip stops when its speech stops (0.5 s); the pause after
        # it, up to where the second begins, belongs to no clip.
        first_end = re.findall(r'clipEnd="([^"]+)"', text)[0]
        return text.replace(f'clipEnd="{first_end}"', 'clipEnd="0:00:00.500"', 1)

    _edit_smil(package, "chapter-0001.smil", old)
    checks = {check["id"]: check for check in daisy_timing.verify(package)}
    assert checks["timing_contiguity"]["status"] == "failed"
    assert checks["timing_totals"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# A whole production job, with audio that is not silent
# --------------------------------------------------------------------------- #

BOOK = """Capítulo 1

Amanecía despacio sobre el río cuando Marta abrió la ventana. El aire olía a tierra mojada.

Llegó el cartero con una carta.

Capítulo 2

La casa tenía 147 ventanas y 12 puertas.
"""


@pytest.fixture
def audible(tmp_path, monkeypatch):
    """The offline mock provider, but each clip is a tone rather than silence.

    The clip still carries its text for the mock recognizer; ffmpeg skips that
    tag, so mastering, the silence at the edges and the timing checks work on
    a real waveform.
    """
    if not audio_master.available():
        pytest.skip("needs ffmpeg")
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "quality_review_enabled": True, "require_peninsular_voice": True}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    folder = tmp_path / "tones"
    folder.mkdir()

    def tone_audio(text: str) -> bytes:
        encoded = text.encode("utf-8")
        seconds = max(0.4, min(6.0, len(text) * 0.05))
        frequency = 200 + int(hashlib.sha256(encoded).hexdigest()[:2], 16)
        return (providers.MOCK_MARKER + len(encoded).to_bytes(4, "big") + encoded
                + _tone(folder, hashlib.sha256(encoded).hexdigest()[:12], seconds, frequency))

    monkeypatch.setattr(providers, "mock_audio", tone_audio)
    return tmp_path


def test_a_production_job_delivers_a_package_that_passes_every_timing_check(audible) -> None:
    client = TestClient(main.app)
    response = client.post("/api/books/jobs",
                           files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job.get("error")
    job_dir = Path(job["source_path"]).parent
    chain = json.loads((job_dir / "completeness.json").read_text(encoding="utf-8"))
    timing = {check["id"]: check["status"] for check in chain["checks"] if check["id"].startswith("timing_")}
    assert timing and set(timing.values()) == {"passed"}, json.dumps(chain, ensure_ascii=False, indent=1)
    validation = json.loads((job_dir / "daisy-validation.json").read_text(encoding="utf-8"))
    assert validation["timing"] == timing
    # The working chapter files are the delivered ones: finished, CBR, with their edges.
    for path in sorted((job_dir / "chapter-audio").glob("chapter-*.mp3")):
        edges = audio_master.edge_silence(path.read_bytes())
        assert edges.leading >= 0.97 and edges.trailing >= 1.17, (path.name, edges)
    # The ZIP holds the same package; audit it from the archive as ONCE would receive it.
    from tools.audit_package import audit
    import zipfile

    unpacked = audible / "unpacked"
    with zipfile.ZipFile(job_dir / f"{job_id}-once-delivery.zip") as archive:
        archive.extractall(unpacked)
    report = audit(unpacked / "DAISY 3")
    assert report["structural"] == "passed" and report["dtd"]["status"] == "passed"
    assert report["timing"]["status"] == "passed", report["timing"]
    shutil.rmtree(unpacked)
