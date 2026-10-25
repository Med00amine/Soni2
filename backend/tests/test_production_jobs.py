from __future__ import annotations

import asyncio
import json
import subprocess
import zipfile
from pathlib import Path

import httpx

from app import jobs, main, sentences

SPAIN_VOICE = "3f065d660212478a9c697fa965b4d098"  # Carmen, es-ES


def _create_job(database: Path, job_dir: Path, sentence_rows: list[dict], *, chapters=None) -> str:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "reviewed-source.json").write_text("{}", encoding="utf-8")
    jobs.initialize_store(database)
    job_id = "job-test"
    jobs.create_job(database, {
        "job_id": job_id, "source_name": "sample.txt", "source_path": str(job_dir / "sample.txt"),
        "source_hash": "source-hash", "chapters": chapters or [{"title": "Chapter 1", "paragraphs": [row["text"] for row in sentence_rows]}],
        "metadata": {"title": "Sample", "author": "Test Author", "edition": "", "publication_year": "", "publisher": ""},
        "voice_id": SPAIN_VOICE, "voice_speed": 1.0,
    }, sentence_rows)
    jobs.add_event(database, job_id, "INFO", "test job created")
    return job_id


def _sentence_rows(texts: list[str], chapter_index: int = 0) -> list[dict]:
    rows = []
    for index, text in enumerate(texts):
        rows.append({"sentence_id": f"sentence-{chapter_index}-{index}", "chapter_index": chapter_index,
                     "chapter_title": f"Chapter {chapter_index + 1}", "sequence": index,
                     "text": text, "synthesis_text": text, "text_start": 0, "text_end": len(text)})
    return rows


def test_lexicon_changes_tts_copy_without_changing_reviewed_source() -> None:
    source = "La ONCE ayuda a los lectores."
    row = sentences.segment_sentences(source, "source-hash", 0)[0]
    rendered = sentences.apply_pronunciation_lexicon(source, {"ONCE": "once"})
    assert row["text"] == source
    assert rendered == "La once ayuda a los lectores."
    assert row["sentence_id"] == sentences.segment_sentences(source, "source-hash", 0)[0]["sentence_id"]


def test_recovery_resets_only_interrupted_sentence_and_keeps_verified_audio(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    rows = _sentence_rows(["Uno.", "Dos.", "Tres."])
    job_id = _create_job(database, job_dir, rows)
    jobs.update_sentence(database, job_id, rows[0]["sentence_id"], state="verified", audio_path="audio/one.mp3")
    jobs.update_sentence(database, job_id, rows[1]["sentence_id"], state="generating")
    jobs.update_sentence(database, job_id, rows[2]["sentence_id"], state="failed")
    jobs.update_job(database, job_id, state="running")

    assert jobs.reset_interrupted_jobs(database) == [job_id]
    recovered = jobs.get_job(database, job_id)
    assert [row["state"] for row in recovered["sentences"]] == ["verified", "pending", "failed"]
    assert recovered["sentences"][0]["audio_path"] == "audio/one.mp3"
    assert recovered["state"] == "queued"


def test_a_restart_stops_an_unfinished_job_instead_of_resuming_it(tmp_path: Path) -> None:
    # Resuming unattended spent credit on a job nobody was watching any more.
    database = tmp_path / "jobs.sqlite3"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    rows = _sentence_rows(["Uno.", "Dos."])
    job_id = _create_job(database, job_dir, rows)
    jobs.update_sentence(database, job_id, rows[0]["sentence_id"], state="verified", audio_path="audio/one.mp3")
    jobs.update_sentence(database, job_id, rows[1]["sentence_id"], state="generating")
    jobs.update_job(database, job_id, state="running")

    assert jobs.stop_interrupted_jobs(database) == [job_id]
    stopped = jobs.get_job(database, job_id)
    assert stopped["state"] == "cancelled"
    assert stopped["cancel_requested"] == 1
    # Paid-for audio is kept, and the interrupted sentence can be resumed later.
    assert [row["state"] for row in stopped["sentences"]] == ["verified", "pending"]
    assert stopped["sentences"][0]["audio_path"] == "audio/one.mp3"


def test_transient_synthesis_retries_with_configured_exponential_delays(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "jobs.sqlite3"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    row = _sentence_rows(["Hola."])[0]
    job_id = _create_job(database, job_dir, [row])
    monkeypatch.setattr(main, "database_path", database)
    calls = 0
    delays = []

    async def transient(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 6:
            raise RuntimeError("temporary provider outage")
        return b"ID3audio"

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(main, "synthesize", transient)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    audio, attempts, error, reused = asyncio.run(
        main._synthesize_with_retries("Hola.", 1.0, SPAIN_VOICE, row, job_id, job_dir)
    )
    assert audio == b"ID3audio"
    assert attempts == calls == 6
    assert error == ""
    assert reused is False
    # Exponential backoff with jitter: each wait is between half and all of
    # 2/4/8/16/32 s, so parallel requests do not retry in lockstep.
    assert len(delays) == 5
    for delay, base in zip(delays, (2, 4, 8, 16, 32)):
        assert base * 0.5 <= delay <= base


def test_a_provider_asking_to_wait_longer_is_obeyed() -> None:
    error = main.ProviderError("rate_limit", "busy", permanent=False, retry_after=20)
    assert main.retry_delay(0, error) == 20
    assert 1 <= main.retry_delay(0, None) <= 2


def test_permanent_synthesis_error_fails_without_retry(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "jobs.sqlite3"
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    row = _sentence_rows(["Hola."])[0]
    job_id = _create_job(database, job_dir, [row])
    monkeypatch.setattr(main, "database_path", database)
    calls = 0

    async def unauthorized(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://provider.invalid")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(main, "synthesize", unauthorized)
    audio, attempts, error, reused = asyncio.run(
        main._synthesize_with_retries("Hola.", 1.0, SPAIN_VOICE, row, job_id, job_dir)
    )
    assert audio is None
    assert attempts == calls == 1
    assert reused is False
    assert "401" in error


def test_acoustic_check_flags_long_silence_and_accepts_short_tone(tmp_path: Path) -> None:
    ffmpeg = main.filesystem.which("ffmpeg")
    if not ffmpeg:
        import pytest
        pytest.skip("ffmpeg is required for the acoustic analysis test")
    tone = tmp_path / "tone.mp3"
    silence = tmp_path / "silence.mp3"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=0.5", "-q:a", "9", "-y", str(tone)], check=True)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=8000:cl=mono", "-t", "2.5", "-q:a", "9", "-y", str(silence)], check=True)
    tone_result = asyncio.run(main._analyze_audio(tone.read_bytes(), "Hola."))
    silence_result = asyncio.run(main._analyze_audio(silence.read_bytes(), "Hola."))
    assert tone_result["status"] == "passed"
    assert silence_result["status"] == "needs_review"
    assert "abnormal_silence" in silence_result["reasons"]


def test_mocked_multichapter_job_builds_ordered_delivery_and_logs(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = main.filesystem.which("ffmpeg")
    if not ffmpeg:
        import pytest
        pytest.skip("ffmpeg is required for the production MP3 assembly integration test")
    source_mp3 = tmp_path / "voice.mp3"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=0.12", "-q:a", "9", "-y", str(source_mp3)], check=True)
    audio_bytes = source_mp3.read_bytes()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "sample.txt").write_text("Chapter 1 text.\n\nChapter 2 text.", encoding="utf-8")
    rows = _sentence_rows(["Chapter 1 sentence."])
    second_chapter = _sentence_rows(["Chapter 2 sentence."], 1)
    rows.extend(second_chapter)
    chapters = [{"title": "First chapter", "paragraphs": [rows[0]["text"]]},
                {"title": "Second chapter", "paragraphs": [rows[1]["text"]]}]
    database = tmp_path / "jobs.sqlite3"
    job_id = _create_job(database, job_dir, rows, chapters=chapters)
    monkeypatch.setattr(main, "database_path", database)
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "quality_review_enabled": True, "once_intro_template": "", "once_bibliographic_template": "",
        "once_identification_statement": "", "once_legal_statement": "",
    }))
    transcription_calls = {row["sentence_id"]: 0 for row in rows}

    async def mock_transcribe(_audio, sentence_id, _text=""):
        transcription_calls[sentence_id] += 1
        expected = next(item["text"] for item in rows if item["sentence_id"] == sentence_id)
        if sentence_id == rows[0]["sentence_id"] and transcription_calls[sentence_id] == 1:
            return "Incorrect sentence.", "mock-scribe"
        return expected, "mock-scribe"

    monkeypatch.setattr(main, "_transcribe_sentence", mock_transcribe)
    monkeypatch.setattr(main, "_analyze_audio", lambda *_args: asyncio.sleep(0, result={"status": "passed", "engine": "test"}))
    monkeypatch.setattr(main, "synthesize", lambda *_args, **_kwargs: asyncio.sleep(0, result=audio_bytes))

    asyncio.run(main.run_production_job(job_id))
    finished = jobs.get_job(database, job_id)
    assert finished["state"] == "completed"
    assert [item["state"] for item in finished["sentences"]] == ["verified", "verified"]
    assert finished["sentences"][0]["quality_retakes"] == 1
    assert transcription_calls[rows[0]["sentence_id"]] == 2
    archive_path = Path(finished["output_path"])
    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        opf = [name for name in names if name.count("/") == 1 and name.endswith(".opf")]
        assert len(opf) == 1 and opf[0] != "DAISY 3/book.opf"
        base = opf[0].removesuffix(".opf")
        assert f"{base}.xml" in names and f"{base}.mp3" in names
        assert "DAISY 3/audio/chapter-001/chapter-001.mp3" in names
        assert "DAISY 3/audio/chapter-002/chapter-002.mp3" in names
        assert "DAISY 3/smil/chapter-0001.smil" in names
        assert "DAISY 3/smil/chapter-0002.smil" in names
        assert "job.log" in names and "tts.log" in names and "quality-report.json" in names
        assert "review-decisions.log" in names and "daisy-validation.json" in names
        assert "reviewed-source.json" in names and "job-events.jsonl" not in names
        assert "source/sample.txt" in names
        report = json.loads(archive.read("quality-report.json"))
        assert report["job_id"] == job_id
        # The sentence that needed a retake is reported as corrected, not merely correct.
        assert report["totals"]["corrected"] == 1
        assert report["totals"]["correct"] == 1
        assert report["totals"]["requires_review"] == 0
        assert len(report["sentences"]) == 2
        assert report["validation"]["status"] == "passed"
        assert report["validation"]["official_daisy_pipeline"] == "not_run"
    # job.log is readable text now, not JSON lines.
    log = (job_dir / "job.log").read_text(encoding="utf-8")
    assert "job.complete: delivery assembled" in log
    assert not log.lstrip().startswith("{")
