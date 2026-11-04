"""Natural pauses: a listener who cannot see the page hears where sentences end.

The voice narrates a whole paragraph per request and leaves its own, often
short, pauses. Mastering finds them in the audio, matches them to the
punctuation, and lengthens the ones that are too short, without narrating
anything again. These tests pin the three promises that matter:

* a pause the voice already made long enough is left alone (no doubled pauses);
* added silence goes only where there is silence already (no word is cut);
* the text/audio synchronisation stays measured, and no provider is called.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audio_integrity, audio_master, main
from app import jobs as job_store
from app.audio_cache import AudioCache

needs_ffmpeg = pytest.mark.skipif(not audio_master.available(), reason="mastering needs ffmpeg")


# --------------------------------------------------------------------------- #
# Where the pauses belong in the text
# --------------------------------------------------------------------------- #

def test_sentence_ends_commas_and_clauses_are_found_in_reading_order() -> None:
    marks = audio_master.pause_marks("Llegó tarde. Abrió la puerta, miró dentro; no había nadie: solo el mar.")
    assert [kind for _fraction, kind in marks] == ["sentence", "comma", "clause", "clause"]
    fractions = [fraction for fraction, _kind in marks]
    assert fractions == sorted(fractions) and 0 < fractions[0] < fractions[-1] < 1


def test_the_end_of_the_passage_is_not_a_pause_inside_it() -> None:
    # Its pause is the gap the passage is joined with.
    assert audio_master.pause_marks("Una sola frase.") == []


def test_abbreviations_initials_and_numbers_are_not_sentence_ends() -> None:
    text = "El Sr. Pérez trabajaba en Sonidos S.L. desde 1990 y medía 1.80 metros. Luego se fue."
    assert [kind for _fraction, kind in audio_master.pause_marks(text)] == ["sentence"]
    assert audio_master.pause_marks("J. R. R. Tolkien escribió mucho.") == []


def test_question_exclamation_ellipsis_and_closing_quotes_end_sentences() -> None:
    text = "—¿Vienes? —preguntó. «¡Ya voy!» Esperó… Nadie contestó."
    kinds = [kind for _fraction, kind in audio_master.pause_marks(text)]
    assert kinds.count("sentence") == 4


# --------------------------------------------------------------------------- #
# Which of the voice's pauses are lengthened, and by how much
# --------------------------------------------------------------------------- #

OPTIONS = audio_master.PauseOptions(sentence_ms=700, clause_ms=450, comma_ms=300, min_silence_ms=100)


def test_short_pauses_are_lengthened_to_the_target() -> None:
    # "Uno dos. Tres cuatro, cinco seis." spoken in three bursts of 0.8 s.
    plan = audio_master.plan_pauses("Uno dos. Tres cuatro, cinco seis.",
                                    [(0.8, 1.05), (1.85, 1.97)], 2.77, OPTIONS)
    assert [round(added, 2) for _at, added in plan] == [0.45, 0.18]
    # In the middle of the silence that is already there.
    assert [round(at, 3) for at, _added in plan] == [0.925, 1.91]


def test_a_pause_already_long_enough_is_left_alone() -> None:
    plan = audio_master.plan_pauses("Uno dos. Tres cuatro, cinco seis.",
                                    [(0.8, 1.9), (2.7, 3.2)], 4.0, OPTIONS)
    assert plan == []


def test_silence_is_only_added_inside_silence_that_exists() -> None:
    # The voice read the comma straight through: no silence there, nothing added.
    plan = audio_master.plan_pauses("Uno dos. Tres cuatro, cinco seis.", [(0.8, 1.0)], 2.6, OPTIONS)
    assert len(plan) == 1
    at, _added = plan[0]
    assert 0.8 <= at <= 1.0


def test_a_consonant_closure_is_not_a_pause() -> None:
    # 60 ms of quiet inside a word is below the shortest pause: never touched.
    assert audio_master.plan_pauses("Uno dos. Tres.", [(0.8, 0.86)], 2.0, OPTIONS) == []


def test_a_breath_without_punctuation_is_not_taken_for_the_full_stop() -> None:
    # A breath early in the passage, the real pause where the full stop is.
    plan = audio_master.plan_pauses("Era una mañana larga y tranquila. Nadie dijo nada.",
                                    [(0.4, 0.52), (2.2, 2.45)], 3.6, OPTIONS)
    assert len(plan) == 1 and 2.2 <= plan[0][0] <= 2.45


def test_disabled_pauses_change_nothing() -> None:
    off = audio_master.PauseOptions(enabled=False)
    assert audio_master.plan_pauses("Uno. Dos.", [(0.5, 0.6)], 1.2, off) == []


def test_a_time_in_the_narration_moves_by_the_pauses_before_it() -> None:
    pauses = [(1.0, 0.4), (3.0, 0.2)]
    assert audio_master.shift_for_pauses(0.5, pauses) == 0.5
    assert audio_master.shift_for_pauses(2.0, pauses) == pytest.approx(2.4)
    assert audio_master.shift_for_pauses(5.0, pauses) == pytest.approx(5.6)


def test_parts_of_one_paragraph_get_a_sentence_pause_not_a_paragraph_pause() -> None:
    options = audio_master.MasteringOptions(paragraph_gap_ms=1000)
    # Units 0-1 are two parts of one long paragraph, 2 starts a new one.
    assert options.gaps_for([True, False, False], 3) == [700, 1000, 0]
    assert options.gaps_for(None, 3) == [1000, 1000, 0]


def test_the_mastering_signature_follows_every_pause_setting() -> None:
    base = audio_master.MasteringOptions()
    assert base.signature() == audio_master.MasteringOptions().signature()
    changed = audio_master.MasteringOptions(pauses=audio_master.PauseOptions(comma_ms=350))
    assert changed.signature() != base.signature()
    assert audio_master.MasteringOptions(paragraph_gap_ms=900).signature() != base.signature()


# --------------------------------------------------------------------------- #
# The real thing: audio in, measured audio out
# --------------------------------------------------------------------------- #

def _speech(tmp_path: Path, words: list[float], gaps: list[float], name: str = "speech") -> bytes:
    """Tone "words" separated by silences of the given lengths, as one MP3."""
    parts = []
    for index, length in enumerate(words):
        parts.append(f"sine=frequency={300 + 40 * index}:duration={length}")
    inputs = []
    filters = []
    labels = []
    for index, source in enumerate(parts):
        inputs += ["-f", "lavfi", "-i", source]
        pad = gaps[index] if index < len(gaps) else 0.0
        filters.append(f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                       f"apad=pad_dur={pad}[w{index}]")
        labels.append(f"[w{index}]")
    graph = ";".join(filters) + ";" + "".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]"
    target = tmp_path / f"{name}.mp3"
    subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(target)],
                   check=True, capture_output=True)
    return target.read_bytes()


def _silences(audio: bytes, tmp_path: Path) -> list[tuple[float, float]]:
    source = tmp_path / "measure.mp3"
    source.write_bytes(audio)
    result = subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(source),
                             "-af", "silencedetect=noise=-40dB:d=0.1", "-f", "null", "-"],
                            capture_output=True, check=False)
    return audio_master._silences_from_log(result.stderr.decode("utf-8", "replace"))


@needs_ffmpeg
def test_a_mastered_passage_has_the_pauses_its_punctuation_asks_for(tmp_path) -> None:
    text = "Uno dos. Tres cuatro, cinco seis."
    passage = _speech(tmp_path, [0.8, 0.8, 0.8], [0.25, 0.12])
    options = audio_master.MasteringOptions(target_lufs=-18.0)
    chapter = audio_master.master_chapter([passage], options, texts=[text])
    assert chapter.mastered, chapter.detail
    gaps = [end - start for start, end in _silences(chapter.audio, tmp_path)]
    # The full stop now has about 0.7 s, the comma about 0.3 s.
    assert len(gaps) == 2, gaps
    assert gaps[0] == pytest.approx(0.70, abs=0.06)
    assert gaps[1] == pytest.approx(0.30, abs=0.06)
    assert [round(added, 2) for _at, added in chapter.pauses[0]] == [0.45, 0.18]
    # Still one valid MP3 stream.
    assert not audio_integrity.inspect_stream(chapter.audio)["errors"]


@needs_ffmpeg
def test_speech_is_never_shortened_and_long_pauses_are_kept(tmp_path) -> None:
    text = "Uno dos. Tres cuatro, cinco seis."
    passage = _speech(tmp_path, [0.8, 0.8, 0.8], [0.9, 0.5])
    chapter = audio_master.master_chapter([passage], audio_master.MasteringOptions(), texts=[text])
    gaps = [end - start for start, end in _silences(chapter.audio, tmp_path)]
    assert gaps == pytest.approx([0.9, 0.5], abs=0.06)
    assert chapter.pauses == [[]]


@needs_ffmpeg
def test_clips_stay_measured_with_pauses_inside_and_between_passages(tmp_path) -> None:
    first = _speech(tmp_path, [0.8, 0.8], [0.2], name="first")
    second = _speech(tmp_path, [0.6, 0.6], [0.15], name="second")
    options = audio_master.MasteringOptions(paragraph_gap_ms=1000)
    chapter = audio_master.master_chapter([first, second], options, texts=["Hola mundo. Adiós ya.", "Sí, claro."])
    assert chapter.mastered, chapter.detail
    (begin_1, end_1), (begin_2, end_2) = chapter.clips
    # Passage one: 1.6 s of speech, its 0.2 s pause made 0.7 s.
    assert begin_1 == 0
    assert end_1 == pytest.approx(1.6 + 0.7, abs=0.08)
    # The paragraph gap is a second, then passage two: 1.2 s of speech, comma 0.3 s.
    assert begin_2 - end_1 == pytest.approx(1.0, abs=0.05)
    assert end_2 - begin_2 == pytest.approx(1.2 + 0.3, abs=0.08)
    assert end_2 == pytest.approx(audio_master.duration_seconds(chapter.audio), abs=0.05)


@needs_ffmpeg
def test_without_texts_mastering_is_exactly_as_before(tmp_path) -> None:
    passage = _speech(tmp_path, [0.8, 0.8], [0.2])
    old = audio_master.join_passages([passage, passage], gap_ms=350)
    new = audio_master.join_passages([passage, passage], gaps_ms=[350, 0])
    assert old.audio == new.audio and old.clips == new.clips


# --------------------------------------------------------------------------- #
# In production: no provider call, and the times stay right
# --------------------------------------------------------------------------- #

BOOK = """Capítulo 1

Amanecía despacio sobre el río. El aire olía a tierra mojada, y Marta abrió la ventana.

Capítulo 2

La casa tenía doce puertas. Nadie sabía por qué.
"""


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "quality_review_enabled": True,
    }))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    calls: list[str] = []
    real = main.synthesize

    async def counting(text, voice_speed=1.0, voice_id="", **kwargs):
        calls.append(text)
        return await real(text, voice_speed, voice_id, **kwargs)

    monkeypatch.setattr(main, "synthesize", counting)
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    return calls


def _produce(client: TestClient) -> str:
    response = client.post("/api/books/jobs",
                           files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    return job_id


def test_pause_settings_never_call_the_provider(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    first = _produce(client)
    narrated = len(offline)
    assert narrated > 0
    # Other pauses, the book produced again: mastering runs again, narration does not.
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "audio_sentence_pause_ms": 900, "audio_comma_pause_ms": 400, "audio_paragraph_gap_ms": 1200}))
    second = _produce(client)
    assert len(offline) == narrated
    assert job_store.get_job(main.database_path, second)["state"] == "completed"
    assert first != second


def test_differences_are_located_after_the_pauses_added_before_them(monkeypatch) -> None:
    job = {"job_id": "j", "sentences": [{
        "sentence_id": "s1", "chapter_index": 0, "chapter_title": "Uno", "sequence": 0, "state": "verified",
        "acoustic": {"signal": {"leading_silence_seconds": 0.5}}, "diagnosis": {}, "metrics": {},
        "issues": [{"audio_start": 3.5, "expected": "río", "recognized": "frío"}], "updated_at": "t"}]}
    monkeypatch.setattr(job_store, "update_sentence", lambda *args, **kwargs: None)
    located = main._fault_locations(job, [("chapter-001.mp3", 10.0, 20.0)], [0.0], {"s1": 1},
                                    {"s1": [(1.0, 0.4), (2.9, 0.3), (3.5, 0.2)]}, "libro.mp3")
    # 3.5 s in the clip, 0.5 s of it trimmed lead, then 0.4 + 0.3 s added before it.
    assert located[0]["chapter_time"] == pytest.approx(10.0 + 3.0 + 0.7)
    assert located[0]["chapter_file"] == "audio/chapter-001.mp3"
