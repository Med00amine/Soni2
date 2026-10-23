"""End-to-end production with the offline mock provider.

This exercises upload, segmentation, narration, quality review, delivery
assembly, logging and reporting without any paid API, and then checks the
claim that matters most for cost: correcting one sentence regenerates one
sentence and rebuilds one chapter.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs as job_store
from app import main
from app import providers
from app.audio_cache import AudioCache

BOOK = """Capítulo 1

Amanecía despacio sobre el río cuando Marta abrió la ventana. El aire olía a tierra mojada.

Capítulo 2

La casa tenía 147 ventanas y 12 puertas. El camino hasta el pueblo medía 18 km.
"""


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """A fully offline Soni2 with mock narration and mock speech review."""
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "quality_review_enabled": True, "require_peninsular_voice": True,
        "once_intro_template": "", "once_bibliographic_template": "",
        "once_identification_statement": "", "once_legal_statement": "",
    }))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))

    calls: list[str] = []
    contexts: list[tuple[str, str]] = []
    real_synthesize = main.synthesize

    async def counting_synthesize(text, voice_speed=1.0, voice_id="", **kwargs):
        # Every call here is one the provider would have charged for.
        calls.append(text)
        contexts.append(kwargs.get("context", ("", "")))
        return await real_synthesize(text, voice_speed, voice_id, **kwargs)

    monkeypatch.setattr(main, "synthesize", counting_synthesize)
    # Start jobs explicitly so each test controls when the worker runs.
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    return {"calls": calls, "contexts": contexts, "tmp": tmp_path}


def _create_job(client: TestClient) -> str:
    response = client.post(
        "/api/books/jobs",
        files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _job_dir(job_id: str) -> Path:
    job = job_store.get_job(main.database_path, job_id)
    return Path(job["source_path"]).parent


# --------------------------------------------------------------------------- #
# Full run
# --------------------------------------------------------------------------- #

def test_offline_run_completes_without_any_paid_api(offline) -> None:
    client = TestClient(main.app)
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["tts_provider"] == "mock"
    assert capabilities["offline_mock_mode"] is True
    assert capabilities["tts_configured"] is True
    assert capabilities["voice_is_peninsular"] is True
    assert capabilities["official_daisy_pipeline_validation"] is False

    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))

    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    assert {item["state"] for item in job["sentences"]} == {"verified"}
    assert Path(job["output_path"]).is_file()


def test_delivery_has_one_copy_of_each_audio_artifact(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)

    with zipfile.ZipFile(job["output_path"]) as archive:
        names = set(archive.namelist())

    # Everything a reviewer needs is easy to locate, named after the book.
    base = _package_base(names)
    assert base != "book"
    assert f"DAISY 3/{base}.xml" in names
    assert f"DAISY 3/{base}.mp3" in names
    assert "DAISY 3/audio/chapter-001/chapter-001.mp3" in names
    assert "DAISY 3/audio/chapter-002/chapter-002.mp3" in names
    assert "DAISY 3/smil/chapter-0001.smil" in names
    # Logs end in .log; the JSON Lines twin of job.log is not delivered.
    assert {"job.log", "tts.log", "review-decisions.log", "quality-report.html", "quality-report.json",
            "reviewed-source.json", "daisy-validation.json"} <= names
    assert not {"job-events.jsonl", "tts-log.json", "review-corrections.json"} & names
    assert any(name.startswith("source/") for name in names)

    # Audio is delivered once per chapter plus the full book, nothing else.
    audio = sorted(name for name in names if name.startswith("DAISY 3/") and name.endswith(".mp3"))
    assert audio == ["DAISY 3/audio/chapter-001/chapter-001.mp3",
                     "DAISY 3/audio/chapter-002/chapter-002.mp3", f"DAISY 3/{base}.mp3"]


def _package_base(names: set[str]) -> str:
    """The book-derived name of the DAISY files, from the one OPF in the ZIP."""
    opf = [name for name in names if name.startswith("DAISY 3/") and name.count("/") == 1 and name.endswith(".opf")]
    assert len(opf) == 1, opf
    return opf[0].removeprefix("DAISY 3/").removesuffix(".opf")


def test_job_log_is_human_readable_text_not_json_lines(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))

    log = (_job_dir(job_id) / "job.log").read_text(encoding="utf-8")
    first = log.splitlines()[0]
    # A readable .log line, not a JSON object.
    assert not first.lstrip().startswith("{")
    assert first.startswith("20")
    assert " INFO " in log
    for expected in ("job.queue", "tts.synthesize", "qa.compare", "sentence.finish",
                     "assemble.chapter", "daisy.validate", "job.complete"):
        assert expected in log, expected
    assert "provider=mock" in log
    assert "sentence=" in log
    assert "ch=001" in log
    # The structured stream is kept beside it, not instead of it.
    events = (_job_dir(job_id) / "job-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(events[0])["operation"]


def test_job_log_never_records_credentials(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    log = (_job_dir(job_id) / "job.log").read_text(encoding="utf-8")
    assert "Bearer" not in log and "Token " not in log
    assert "api_key" not in log


def test_quality_report_separates_correct_from_review_and_names_the_voice(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job_dir = _job_dir(job_id)

    report = json.loads((job_dir / "quality-report.json").read_text(encoding="utf-8"))
    assert report["narration"]["tts_provider"] == "mock"
    assert report["narration"]["asr_provider"] == "mock"
    assert report["narration"]["voice_language"] == "es-ES"
    assert report["totals"]["sentences"] > 0
    assert report["totals"]["correct"] == report["totals"]["sentences"]
    assert report["totals"]["requires_review"] == 0
    assert report["validation"]["status"] == "passed"
    assert report["validation"]["official_daisy_pipeline"] == "not_run"
    # Unmeasured metrics stay unmeasured rather than being invented.
    assert report["metrics"]["real_error_detection"] is None

    html = (job_dir / "quality-report.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "Informe de calidad" in html
    # Self-contained: no external stylesheet, script or font.
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "Requieren revisión" in html
    assert "ciento cuarenta y siete" in html or "147" in html
    assert 'data-outcome="correct"' in html


def test_report_explains_that_normalized_numbers_are_not_errors(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    html = (_job_dir(job_id) / "quality-report.html").read_text(encoding="utf-8")
    assert "ciento cuarenta y siete" in html
    assert "no se han medido" in html


# --------------------------------------------------------------------------- #
# Cost control: the behaviour the credits depend on
# --------------------------------------------------------------------------- #

def test_rerunning_an_unchanged_book_pays_for_nothing(offline) -> None:
    client = TestClient(main.app)
    first = _create_job(client)
    asyncio.run(main.run_production_job(first))
    paid_first = len(offline["calls"])
    assert paid_first > 0

    offline["calls"].clear()
    second = _create_job(client)
    asyncio.run(main.run_production_job(second))

    job = job_store.get_job(main.database_path, second)
    assert job["state"] == "completed"
    # Identical text, voice and speed: every sentence comes from the cache.
    assert offline["calls"] == []
    assert all(item["reused_audio"] for item in job["sentences"])


def test_correcting_one_sentence_regenerates_only_that_sentence(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    total = len(job["sentences"])
    assert total >= 4

    target = next(item for item in job["sentences"] if item["chapter_index"] == 1)
    offline["calls"].clear()
    response = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "correct",
        "corrected_text": "La casa tenía 148 ventanas y 12 puertas.",
    })
    assert response.status_code == 200, response.text
    asyncio.run(main.run_production_job(job_id))

    # Exactly one sentence was paid for, not the whole book.
    assert len(offline["calls"]) == 1
    assert offline["calls"][0] == "La casa tenía 148 ventanas y 12 puertas."
    finished = job_store.get_job(main.database_path, job_id)
    assert finished["state"] == "completed"
    assert len(finished["sentences"]) == total


def test_correcting_a_sentence_rebuilds_only_its_chapter(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    job_dir = _job_dir(job_id)

    target = next(item for item in job["sentences"] if item["chapter_index"] == 1)
    client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "correct",
        "corrected_text": "La casa tenía 148 ventanas y 12 puertas.",
    })
    log_before = (job_dir / "job.log").read_text(encoding="utf-8")
    asyncio.run(main.run_production_job(job_id))
    new_log = (job_dir / "job.log").read_text(encoding="utf-8")[len(log_before):]

    assert "reused unchanged chapter audio" in new_log
    assert "rebuilt chapter audio" in new_log
    # Chapter 1 was untouched, so its audio is reused; chapter 2 is rebuilt.
    reused = [line for line in new_log.splitlines() if "reused unchanged chapter audio" in line]
    rebuilt = [line for line in new_log.splitlines() if "rebuilt chapter audio" in line]
    assert any("ch=001" in line for line in reused)
    assert any("ch=002" in line for line in rebuilt)
    assert not any("ch=001" in line for line in rebuilt)


def test_a_duplicate_submission_does_not_start_a_second_paid_job(offline) -> None:
    client = TestClient(main.app)
    first = _create_job(client)
    # The same book, voice and speed while the first job is still queued.
    response = client.post(
        "/api/books/jobs",
        files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["reused_existing_job"] is True
    assert payload["job_id"] == first


def test_changing_the_voice_invalidates_audio_but_text_edits_do_not(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    baseline = len(offline["calls"])
    assert baseline > 0

    # A different voice is genuinely different audio, so it must be regenerated.
    offline["calls"].clear()
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-mx-male",
        "require_peninsular_voice": False, "quality_review_enabled": True,
        "once_intro_template": "", "once_bibliographic_template": "",
    }))
    other = _create_job(client)
    asyncio.run(main.run_production_job(other))
    assert len(offline["calls"]) == baseline


def test_preview_audio_is_cached_so_repeating_a_preview_costs_nothing(offline) -> None:
    client = TestClient(main.app)
    form = {"voice_id": "mock-es-es-female"}
    first = client.post("/api/books/preview",
                        files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
                        data=form)
    assert first.status_code == 200, first.text
    assert first.json()["audio_base64"]
    paid = len(offline["calls"])
    assert paid > 0

    offline["calls"].clear()
    second = client.post("/api/books/preview",
                         files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
                         data=form)
    assert second.status_code == 200
    assert second.json()["audio_base64"] == first.json()["audio_base64"]
    assert offline["calls"] == []


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #

def test_an_interrupted_job_resumes_without_regenerating_verified_audio(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)

    # Simulate a container restart mid-sentence.
    first, last = job["sentences"][0], job["sentences"][-1]
    job_store.update_sentence(main.database_path, job_id, last["sentence_id"], state="generating")
    job_store.update_job(main.database_path, job_id, state="running")
    assert job_store.reset_interrupted_jobs(main.database_path) == [job_id]

    recovered = job_store.get_job(main.database_path, job_id)
    assert recovered["state"] == "queued"
    states = {item["sentence_id"]: item["state"] for item in recovered["sentences"]}
    assert states[first["sentence_id"]] == "verified"
    assert states[last["sentence_id"]] == "pending"

    offline["calls"].clear()
    asyncio.run(main.run_production_job(job_id))
    assert job_store.get_job(main.database_path, job_id)["state"] == "completed"
    # The reset sentence came back from the cache instead of the provider.
    assert offline["calls"] == []


def test_daisy_validation_result_is_recorded_and_honest(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))

    validation = json.loads((_job_dir(job_id) / "daisy-validation.json").read_text(encoding="utf-8"))
    assert validation["status"] == "passed"
    assert validation["method"] == "soni2_structural_validation"
    # The official validator was not run, and the record says so plainly.
    assert validation["official_daisy_pipeline"] == "not_run"
    assert "no sustituye al validador oficial" in validation["note"].lower()


def test_the_job_directory_keeps_one_working_copy_not_a_staged_duplicate(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job_dir = _job_dir(job_id)

    # Per-sentence audio for targeted retakes, and chapter audio for
    # incremental rebuilds. The staged DAISY tree is gone: the ZIP holds it.
    assert list(job_dir.glob("audio/chapter-001/segment-*.mp3"))
    assert list(job_dir.glob("chapter-audio/chapter-*.mp3"))
    assert not (job_dir / "delivery" / "daisy").exists()
    # The incremental-rebuild fingerprints survive so the next run can reuse audio.
    assert (job_dir / "delivery" / "chapter-state.json").is_file()
    assert (job_dir / "quality-report.html").is_file()

    chapter_copies = [path for path in job_dir.rglob("chapter-0*.mp3")]
    assert len(chapter_copies) == len(set(path.name for path in chapter_copies))


def test_a_direct_download_reuses_the_audio_a_reviewed_run_already_paid_for(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    paid_for_job = len(offline["calls"])
    assert paid_for_job > 0

    # Both paths narrate the same paragraph units with the same synthesis text,
    # so the keys match and nothing already paid for is generated again.
    offline["calls"].clear()
    response = client.post(
        "/api/books/package",
        files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 200, response.text
    assert zipfile.is_zipfile(io.BytesIO(response.content))
    assert offline["calls"] == [], "a direct download must not re-narrate cached paragraphs"


def test_narration_is_one_request_per_paragraph_and_the_preview_is_reused(offline) -> None:
    client = TestClient(main.app)
    book = {"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")}
    preview = client.post("/api/books/preview", files=book)
    assert preview.status_code == 200, preview.text
    preview_calls = list(offline["calls"])
    assert preview_calls, "the preview must narrate something"
    # Whole paragraphs: a request carries several sentences, not one.
    assert any(text.count(". ") >= 1 for text in preview_calls)

    offline["calls"].clear()
    book = {"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")}
    response = client.post("/api/books/package", files=book)
    assert response.status_code == 200, response.text
    # The download pays only for what the preview did not already narrate.
    assert not set(offline["calls"]) & set(preview_calls)


def test_a_direct_download_applies_the_same_narration_rules(offline, monkeypatch) -> None:
    # Numbers the voice misreads must be spelled out on this path too, and a
    # reviewer's lexicon entry must still apply.
    client = TestClient(main.app)
    client.put("/api/pronunciation-lexicon", json={"entries": {"ONCE": "once"}})
    book = "Capítulo 1\n\nLa ONCE pagó 3.500 euros.\n"
    response = client.post(
        "/api/books/package",
        files={"file": ("libro.txt", io.BytesIO(book.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 200, response.text
    narrated = " ".join(offline["calls"])
    assert "tres mil quinientos" in narrated
    assert "3.500" not in narrated
    assert "La once pagó" in narrated


def test_audio_is_stored_not_deflated_in_the_delivery(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)

    with zipfile.ZipFile(job["output_path"]) as archive:
        entries = {info.filename: info.compress_type for info in archive.infolist()}

    # MP3 does not compress: deflating a book-sized delivery cost minutes for
    # about 2% of size, so audio is stored and only the text is compressed.
    audio = {name: mode for name, mode in entries.items() if name.endswith(".mp3")}
    assert audio, "the delivery must contain audio"
    assert set(audio.values()) == {zipfile.ZIP_STORED}, audio
    for name in ("quality-report.html", "quality-report.json", "job.log", f"DAISY 3/{_package_base(set(entries))}.xml"):
        assert entries[name] == zipfile.ZIP_DEFLATED, name
    # The archive must still open normally.
    with zipfile.ZipFile(job["output_path"]) as archive:
        assert archive.testzip() is None


def test_the_direct_download_archive_uses_the_same_compression_rules(offline) -> None:
    client = TestClient(main.app)
    response = client.post(
        "/api/books/package",
        files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")), "text/plain")},
    )
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        entries = {info.filename: info.compress_type for info in archive.infolist()}
        assert archive.testzip() is None
    assert {mode for name, mode in entries.items() if name.endswith(".mp3")} == {zipfile.ZIP_STORED}
    assert entries[f"DAISY 3/{_package_base(set(entries))}.xml"] == zipfile.ZIP_DEFLATED


def test_review_differences_carry_offsets_for_highlighting(offline) -> None:
    from app import qa_review

    source = "El camino medía 18 km."
    heard = "El camino mide 18 kilómetros."
    _verdict, issues, _confidence = qa_review.evaluate(source, heard)
    assert issues
    issue = issues[0]
    # The offsets must select exactly the words the reviewer needs to see.
    assert source[issue["expected_start"]:issue["expected_end"]] == issue["expected"]
    assert heard[issue["recognized_start"]:issue["recognized_end"]] == issue["recognized"]


def test_an_omission_reports_the_position_where_words_are_missing(offline) -> None:
    from app import qa_review

    source = "El tren llegó al pueblo justo antes del amanecer."
    heard = "El tren llegó al pueblo."
    _verdict, issues, _confidence = qa_review.evaluate(source, heard)
    issue = next(item for item in issues if item["difference_type"] == "omission")
    assert source[issue["expected_start"]:issue["expected_end"]] == issue["expected"]
    # Nothing was recognized there, so the span collapses to a single point the
    # interface can mark.
    assert issue["recognized_start"] == issue["recognized_end"]
    assert 0 <= issue["recognized_start"] <= len(heard)


# --------------------------------------------------------------------------- #
# Speed: several paragraphs are narrated at once
# --------------------------------------------------------------------------- #

# Twenty paragraphs with unique markers, so the order of the audio can be checked.
LONG_BOOK = "\n\n".join(f"Marca {index:02d}. Un párrafo de prueba con algo de texto." for index in range(20))


def _long_book() -> dict:
    return {"file": ("largo.txt", io.BytesIO(LONG_BOOK.encode("utf-8")), "text/plain")}


@pytest.fixture
def slow_voice(offline, monkeypatch):
    """A mock voice that takes a moment per request and records how many overlap."""
    from app import providers

    state = {"in_flight": 0, "peak": 0, "calls": [], "fail_on": None, "rate_limit_once": None, "delay": 0.01}

    async def slow_synthesize(text, voice_speed=1.0, voice_id="", **_kwargs):
        state["calls"].append(text)
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        try:
            await asyncio.sleep(state["delay"])
            if state["fail_on"] and state["fail_on"] in text:
                raise providers.ProviderError("credit", "La cuenta no tiene saldo.", permanent=True)
            if state["rate_limit_once"] and state["rate_limit_once"] in text:
                state["rate_limit_once"] = None
                raise providers.ProviderError("rate_limit", "Demasiadas peticiones (HTTP 429).", permanent=False)
            return providers.mock_audio(text)
        finally:
            state["in_flight"] -= 1

    monkeypatch.setattr(main, "synthesize", slow_synthesize)
    return state


def test_a_direct_download_narrates_several_paragraphs_at_once_in_order(slow_voice, monkeypatch) -> None:
    # This is about ordering, not about audio processing. Without ffmpeg the
    # chapter is a plain byte join, so each paragraph's text is still findable
    # in it; the mastered path re-encodes and deliberately discards those tags.
    monkeypatch.setattr(main.filesystem, "which", lambda _name: None)
    response = TestClient(main.app).post("/api/books/package", files=_long_book())
    assert response.status_code == 200, response.text
    # Several requests overlapped, and never more than the provider's limit.
    assert 1 < slow_voice["peak"] <= main.narration_concurrency()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        chapter = archive.read("DAISY 3/audio/chapter-001/chapter-001.mp3")
    # Finishing out of order must not reorder the book.
    positions = [chapter.index(f"Marca {index:02d}".encode()) for index in range(20)]
    assert positions == sorted(positions)


def test_the_concurrency_can_be_configured(slow_voice, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"tts_concurrency": 2}))
    response = TestClient(main.app).post("/api/books/package", files=_long_book())
    assert response.status_code == 200, response.text
    assert slow_voice["peak"] == 2


def test_a_permanent_failure_stops_the_remaining_requests(slow_voice) -> None:
    # An empty balance on one paragraph must not keep paying for the others.
    slow_voice["fail_on"] = "Marca 03"
    slow_voice["delay"] = 0.05
    response = TestClient(main.app).post("/api/books/package", files=_long_book())
    assert response.status_code == 503
    assert "no tiene saldo" in response.json()["detail"]
    assert len(slow_voice["calls"]) < 20


def test_a_brief_rate_limit_is_waited_out_instead_of_failing_the_book(slow_voice, monkeypatch) -> None:
    monkeypatch.setattr(main, "retry_delays", (0, 0))
    slow_voice["rate_limit_once"] = "Marca 05"
    response = TestClient(main.app).post("/api/books/package", files=_long_book())
    assert response.status_code == 200, response.text
    # The refused paragraph was asked for twice; every other paragraph once.
    assert sum("Marca 05" in text for text in slow_voice["calls"]) == 2
    assert len(slow_voice["calls"]) == 21


def test_a_reviewed_run_also_narrates_several_units_at_once(slow_voice) -> None:
    client = TestClient(main.app)
    created = client.post("/api/books/jobs", files=_long_book())
    assert created.status_code == 202, created.text
    asyncio.run(main.run_production_job(created.json()["job_id"]))
    job = job_store.get_job(main.database_path, created.json()["job_id"])
    assert job["state"] == "completed", job["error"]
    assert slow_voice["peak"] > 1


# --------------------------------------------------------------------------- #
# The page's download: a background task with real progress
# --------------------------------------------------------------------------- #

def _wait_for_task(client: TestClient, task_id: str) -> dict:
    for _ in range(500):
        status = client.get(f"/api/books/package-tasks/{task_id}").json()
        if status["state"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError(f"the download did not finish: {status}")


def test_the_page_download_reports_progress_and_serves_the_zip(slow_voice) -> None:
    with TestClient(main.app) as client:
        started = client.post("/api/books/package-tasks", files=_long_book())
        assert started.status_code == 202, started.text
        task_id = started.json()["task_id"]
        status = _wait_for_task(client, task_id)
        assert status["state"] == "completed", status
        assert status["done"] == status["total"] == 20
        assert status["reused"] == 0
        download = client.get(f"/api/books/package-tasks/{task_id}/download")
        assert download.status_code == 200
        assert zipfile.is_zipfile(io.BytesIO(download.content))
        assert "-daisy.zip" in download.headers["content-disposition"]

        # Downloading the same book again pays for nothing, and says so.
        calls_before = len(slow_voice["calls"])
        again = _wait_for_task(client, client.post("/api/books/package-tasks", files=_long_book()).json()["task_id"])
        assert again["state"] == "completed"
        assert again["reused"] == again["total"] == 20
        assert len(slow_voice["calls"]) == calls_before


def test_the_page_download_explains_a_failure(slow_voice) -> None:
    slow_voice["fail_on"] = "Marca 00"
    with TestClient(main.app) as client:
        task_id = client.post("/api/books/package-tasks", files=_long_book()).json()["task_id"]
        status = _wait_for_task(client, task_id)
        assert status["state"] == "failed"
        assert "no tiene saldo" in status["detail"]
        assert client.get(f"/api/books/package-tasks/{task_id}/download").status_code == 409


def test_a_cancelled_download_stops_narrating(slow_voice, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"tts_concurrency": 1}))
    slow_voice["delay"] = 0.05
    with TestClient(main.app) as client:
        task_id = client.post("/api/books/package-tasks", files=_long_book()).json()["task_id"]
        for _ in range(200):
            if client.get(f"/api/books/package-tasks/{task_id}").json()["done"]:
                break
            time.sleep(0.01)
        cancelled = client.post(f"/api/books/package-tasks/{task_id}/cancel").json()
        assert cancelled["state"] == "cancelled"
        calls = len(slow_voice["calls"])
        time.sleep(0.3)
        # Nothing more is narrated once nobody is waiting for the book.
        assert len(slow_voice["calls"]) == calls < 20
        assert client.get(f"/api/books/package-tasks/{task_id}/download").status_code == 409


def test_an_unknown_download_says_how_to_recover() -> None:
    response = TestClient(main.app).get("/api/books/package-tasks/missing")
    assert response.status_code == 404
    assert "Descargar audiolibro ahora" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Review decisions are heard at once, and fewer reach a person
# --------------------------------------------------------------------------- #

LONG_PARAGRAPH = (
    "Amanecía despacio sobre el río cuando Marta abrió la ventana y miró el jardín, donde los pájaros "
    "cantaban entre las ramas mojadas del viejo almendro, y el olor a tierra húmeda llenaba la casa "
    "entera mientras la ciudad empezaba a despertar sin ninguna prisa aquel domingo de octubre."
)


def _job_from(client: TestClient, book: str) -> str:
    response = client.post("/api/books/jobs", files={"file": ("otro.txt", io.BytesIO(book.encode("utf-8")), "text/plain")})
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _unit(job_id: str, contains: str) -> dict:
    job = job_store.get_job(main.database_path, job_id)
    return next(item for item in job["sentences"] if contains in item["text"])


def test_correcting_a_paragraph_narrates_it_at_once_and_returns_the_new_reading(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    target = _unit(job_id, "147 ventanas")
    corrected = "La casa tenía 148 ventanas y 12 puertas. El camino hasta el pueblo medía 18 km."
    offline["calls"].clear()

    response = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "correct", "corrected_text": corrected,
    })
    assert response.status_code == 200, response.text
    # Narrated inside the request: no job had to run before it could be heard.
    assert offline["calls"] == [main.build_synthesis_text(corrected, {})]
    sentence = response.json()["sentence"]
    assert sentence["text"] == corrected
    assert sentence["state"] == "verified"
    assert sentence["updated_at"] != target["updated_at"]
    audio = client.get(f"/api/jobs/{job_id}/sentences/{target['sentence_id']}/audio")
    assert audio.status_code == 200
    # The URL stays the same while the reading changes, so it must never be cached.
    assert audio.headers["cache-control"] == "no-store"
    assert main.build_synthesis_text(corrected, {}).encode("utf-8") in audio.content


def test_a_new_reading_that_still_differs_stays_with_its_audio_for_the_reviewer(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    target = _unit(job_id, "147 ventanas")

    async def mishearing(_audio, _sentence_id, _text=""):
        return "Algo completamente distinto que no se parece en nada al texto del libro.", "mock"

    monkeypatch.setattr(main, "_transcribe_sentence", mishearing)
    offline["calls"].clear()
    response = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "retry",
    })
    assert response.status_code == 200, response.text
    sentence = response.json()["sentence"]
    assert sentence["state"] == "needs_human_review"
    assert sentence["audio_path"] and "retakes" in sentence["audio_path"]
    # A person asked for one new reading: no automatic retakes were paid for.
    assert len(offline["calls"]) == 1


def test_a_failed_narration_leaves_the_paragraph_exactly_as_it_was(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    target = _unit(job_id, "147 ventanas")

    async def no_credit(*_args, **_kwargs):
        raise main.ProviderError("credit", "La cuenta no tiene créditos suficientes.", permanent=True)

    monkeypatch.setattr(main, "synthesize", no_credit)
    response = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "correct", "corrected_text": "Un texto nuevo.",
    })
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "créditos" in detail and "No se ha cambiado nada" in detail
    after = _unit(job_id, "147 ventanas")
    assert (after["text"], after["audio_path"], after["state"]) == (target["text"], target["audio_path"], target["state"])


def test_a_paragraph_being_narrated_is_never_narrated_twice(offline) -> None:
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    target = _unit(job_id, "147 ventanas")
    key = (job_id, target["sentence_id"])
    main.units_in_progress.add(key)
    try:
        response = client.post(f"/api/jobs/{job_id}/review", json={
            "sentence_id": target["sentence_id"], "decision": "retry",
        })
        assert response.status_code == 409
        # The job worker leaves it alone as well.
        job_store.update_sentence(main.database_path, job_id, target["sentence_id"], state="pending")
        offline["calls"].clear()
        asyncio.run(main.run_production_job(job_id))
        assert offline["calls"] == []
    finally:
        main.units_in_progress.discard(key)


def test_minor_recognizer_slips_are_accepted_and_reported_not_queued(offline, monkeypatch) -> None:
    async def slips(_audio, _sentence_id, text=""):
        return text.replace("miró", "vio").replace("viejo", "bello"), "mock"

    monkeypatch.setattr(main, "_transcribe_sentence", slips)
    client = TestClient(main.app)
    job_id = _job_from(client, f"Capítulo 1\n\n{LONG_PARAGRAPH}\n")
    asyncio.run(main.run_production_job(job_id))

    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    paragraph = _unit(job_id, "almendro")
    assert paragraph["state"] == "verified"
    assert paragraph["quality_retakes"] == 0
    assert paragraph["last_error"].startswith("Aceptada automáticamente")
    report = json.loads((_job_dir(job_id) / "quality-report.json").read_text(encoding="utf-8"))
    assert report["totals"]["auto_accepted"] == 1


def test_a_stutter_is_retaken_automatically(offline, monkeypatch) -> None:
    heard: dict[str, int] = {}

    async def stutters_once(_audio, sentence_id, text=""):
        heard[sentence_id] = heard.get(sentence_id, 0) + 1
        if "Amanecía" in text and heard[sentence_id] == 1:
            # "el el": a function word said twice used to pass as a recognizer slip.
            return text.replace("sobre el río", "sobre el el río"), "mock"
        return text, "mock"

    monkeypatch.setattr(main, "_transcribe_sentence", stutters_once)
    client = TestClient(main.app)
    job_id = _create_job(client)
    asyncio.run(main.run_production_job(job_id))
    paragraph = _unit(job_id, "Amanecía")
    assert paragraph["state"] == "verified"
    assert paragraph["quality_retakes"] == 1
    assert heard[paragraph["sentence_id"]] == 2
    assert job_store.get_job(main.database_path, job_id)["state"] == "completed"


def test_a_real_error_gets_at_most_two_paid_retakes_then_a_person(offline, monkeypatch) -> None:
    takes = iter(range(100))

    async def wrong_each_time(_audio, _sentence_id, text=""):
        if "Amanecía" not in text:
            return text, "mock"
        # Heard differently every take, so the identical-transcript stop never fires.
        return f"Una lectura totalmente distinta del libro, toma {next(takes)}.", "mock"

    monkeypatch.setattr(main, "_transcribe_sentence", wrong_each_time)
    client = TestClient(main.app)
    job_id = _create_job(client)
    offline["calls"].clear()
    asyncio.run(main.run_production_job(job_id))
    paragraph = _unit(job_id, "Amanecía")
    assert paragraph["state"] == "needs_human_review"
    assert paragraph["quality_retakes"] == main.MAX_QUALITY_RETAKES == 2
    assert sum("Amanecía" in call for call in offline["calls"]) == 3


def test_english_paragraphs_are_checked_in_english(offline, monkeypatch) -> None:
    languages: list[str] = []
    real_transcribe = main.providers.transcribe

    async def recording(audio, **kwargs):
        languages.append(kwargs.get("language", "es"))
        return await real_transcribe(audio, **kwargs)

    monkeypatch.setattr(main.providers, "transcribe", recording)
    client = TestClient(main.app)
    job_id = _job_from(client, "Capítulo 1\n\nBefore we dive into some Spanish, I want to congratulate you "
                               "on the work you have done with the language.\n\n"
                               "Amanecía despacio sobre el río cuando Marta abrió la ventana.\n")
    asyncio.run(main.run_production_job(job_id))
    assert "en" in languages and "es" in languages


def test_each_paragraph_is_narrated_with_its_neighbours_as_free_context(offline) -> None:
    response = TestClient(main.app).post("/api/books/package", files=_long_book())
    assert response.status_code == 200, response.text
    contexts = dict(zip(offline["calls"], offline["contexts"]))
    middle = next(context for text, context in contexts.items() if "Marca 05" in text)
    assert "Marca 04" in middle[0] and "Marca 06" in middle[1]
    first = next(context for text, context in contexts.items() if "Marca 00" in text)
    last = next(context for text, context in contexts.items() if "Marca 19" in text)
    assert first[0] == "" and last[1] == ""


def test_a_limit_reported_mid_run_takes_effect_at_once() -> None:
    limit = {"value": 1}
    state = {"running": 0, "peak": 0}

    async def work(index: int) -> int:
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.01)
        if index == 0:
            limit["value"] = 3  # the first reply reports the account's real limit
        state["running"] -= 1
        return index

    results = asyncio.run(main._run_limited([functools.partial(work, index) for index in range(8)],
                                            lambda: limit["value"]))
    assert results == list(range(8))
    assert state["peak"] == 3
