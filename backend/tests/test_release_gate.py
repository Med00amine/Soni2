"""The release gate, the repair loop's audit trail and the endpoints around them.

Offline: the mock provider and monkeypatched recognizers stand in for paid
services. Nothing here establishes audio quality.
"""

from __future__ import annotations

import asyncio
import io
import json
from urllib.parse import unquote
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs as job_store
from app import main, providers, release_gate
from app.audio_cache import AudioCache

BOOK = """Capítulo 1

Amanecía despacio sobre el río cuando Marta abrió la ventana. El aire olía a tierra mojada.

Capítulo 2

La casa tenía 147 ventanas y el Sr. Ruiz pagó 3,50 € el 12/03/2024. El camino medía 18 km.
"""


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "asr_secondary_provider": "none"}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    return tmp_path


def _job(client: TestClient, text: str = BOOK, name: str = "libro.txt") -> str:
    response = client.post("/api/books/jobs", files={"file": (name, io.BytesIO(text.encode("utf-8")), "text/plain")})
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _unit(job_id: str, contains: str) -> dict:
    return next(item for item in job_store.get_job(main.database_path, job_id)["sentences"] if contains in item["text"])


# --------------------------------------------------------------------------- #
# The gate on its own
# --------------------------------------------------------------------------- #

def _gate_job(states, job_state="completed", **unit):
    return {"state": job_state, "sentences": [
        {"sentence_id": f"s{index}", "state": state, "acoustic": {"status": "passed"}, **unit}
        for index, state in enumerate(states)]}


PASSING = {"completeness": {"status": "passed", "checks": []}, "validation": {"status": "passed"},
           "dtd": {"status": "passed"}, "extraction": {"issues": [], "coverage": {"ratio": 1.0}}}


def test_a_book_passes_only_when_every_check_passed() -> None:
    result = release_gate.evaluate(_gate_job(["verified", "verified"]), simulated_audio=False, **PASSING)
    assert result["state"] == release_gate.PASSED and result["releasable"]
    assert result["dimensions"]["package"]["official_daisy_pipeline"] == "not_run"
    # Even a passing book says what nobody verified.
    assert any("no demuestra" in note for note in result["uncertainty"])


@pytest.mark.parametrize("change,state", [
    ({"sentences": ["verified", "failed"]}, release_gate.BLOCKED),
    ({"sentences": ["verified", "needs_human_review"], "job_state": "partially_completed"}, release_gate.NEEDS_REVIEW),
    ({"simulated": True}, release_gate.BLOCKED),
    ({"completeness": {"status": "failed", "checks": [{"status": "failed", "label": "Unidades → audio",
                                                         "detail": "falta audio"}]}}, release_gate.BLOCKED),
    ({"dtd": {"status": "failed", "errors": ["x"]}}, release_gate.BLOCKED),
    ({"extraction": {"issues": [{"severity": "blocking", "code": "ocr_required", "message": "página 3"}]}},
     release_gate.BLOCKED),
    ({"extraction": {"issues": [{"severity": "warning", "code": "unaccounted_words", "message": "faltan"}]}},
     release_gate.NEEDS_REVIEW),
    ({"job_state": "cancelled"}, release_gate.BLOCKED),
    ({"job_state": "running"}, release_gate.GENERATING),
])
def test_anything_unresolved_keeps_a_book_from_passing(change, state) -> None:
    options = dict(PASSING)
    for key in ("completeness", "dtd", "extraction"):
        if key in change:
            options[key] = change[key]
    job = _gate_job(change.get("sentences", ["verified", "verified"]), change.get("job_state", "completed"))
    result = release_gate.evaluate(job, simulated_audio=change.get("simulated", False), **options)
    assert result["state"] == state
    assert not result["releasable"]
    assert result["blockers"] or result["reviews"] or state == release_gate.GENERATING


def test_an_acknowledged_warning_no_longer_holds_the_book() -> None:
    issue = {"severity": "warning", "code": "unaccounted_words", "message": "faltan", "location": "", "text": "x"}
    options = {**PASSING, "extraction": {"issues": [issue]}}
    job = _gate_job(["verified"])
    held = release_gate.evaluate(job, simulated_audio=False, **options)
    issue_id = held["dimensions"]["source"]["open_warnings"][0]["id"]
    freed = release_gate.evaluate(job, simulated_audio=False, acknowledgements=[{"id": issue_id}], **options)
    assert (held["state"], freed["state"]) == (release_gate.NEEDS_REVIEW, release_gate.PASSED)


# --------------------------------------------------------------------------- #
# Through the API
# --------------------------------------------------------------------------- #

def test_a_mock_book_is_assembled_but_never_released(offline) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed" and job["release_state"] == release_gate.BLOCKED
    blocked = client.get(f"/api/jobs/{job_id}/artifacts")
    assert blocked.status_code == 409
    assert any("simulado" in reason for reason in blocked.json()["detail"]["blockers"])
    diagnostic = client.get(f"/api/jobs/{job_id}/artifacts", params={"diagnostic": "true"})
    assert diagnostic.status_code == 200 and diagnostic.headers["x-soni2-release-state"] == "BLOCKED"
    assert unquote(diagnostic.headers["content-disposition"]).endswith("libro (SIN VALIDAR).zip")
    refused = client.post(f"/api/jobs/{job_id}/release", json={"reviewer": "Ana", "confirm": True})
    assert refused.status_code == 409
    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["release"]["state"] == "BLOCKED"


def test_the_report_of_a_blocked_book_never_calls_it_ready(offline) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    job_dir = Path(job_store.get_job(main.database_path, job_id)["source_path"]).parent
    html = (job_dir / "quality-report.html").read_text(encoding="utf-8")
    report = json.loads((job_dir / "quality-report.json").read_text(encoding="utf-8"))
    assert report["release"]["state"] == "BLOCKED"
    assert "Bloqueado: no se puede publicar" in html and "Listo para entregar" not in html
    assert "Estado de calidad" in html and "Lo que estas comprobaciones no demuestran" in html


def test_a_book_that_passes_can_be_released_by_a_named_person(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    # Pretend the audio were real, to exercise the release path itself.
    monkeypatch.setattr(main, "compute_release", _real_audio(main.compute_release))
    assert client.post(f"/api/jobs/{job_id}/release", json={"reviewer": "Ana"}).status_code == 422
    released = client.post(f"/api/jobs/{job_id}/release", json={"reviewer": "Ana", "confirm": True, "note": "Escuchado"})
    assert released.status_code == 200, released.text
    assert released.json()["state"] == release_gate.RELEASED
    assert released.json()["released"]["by"] == "Ana"
    assert client.get(f"/api/jobs/{job_id}/artifacts").status_code == 200


def _real_audio(original):
    def compute(job_id, *, assume_state=None):
        job = job_store.get_job(main.database_path, job_id)
        job_store.update_job(main.database_path, job_id, release_state="")
        sentences = [{**unit, "acoustic": {"status": "passed"}} for unit in job["sentences"]]
        result = release_gate.evaluate(
            {**job, "sentences": sentences, "state": assume_state or job["state"]},
            extraction=json.loads((Path(job["source_path"]).parent / "extraction-report.json").read_text("utf-8")),
            completeness={"status": "passed", "checks": []}, validation={"status": "passed"},
            dtd={"status": "passed"}, simulated_audio=False, released=(job.get("release") or {}).get("released"))
        return result
    return compute


def test_units_carry_their_spoken_form_its_derivation_and_their_source(offline) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    unit = _unit(job_id, "147 ventanas")
    assert unit["text"] == "La casa tenía 147 ventanas y el Sr. Ruiz pagó 3,50 € el 12/03/2024. El camino medía 18 km."
    assert unit["spoken_text"] == ("La casa tenía 147 ventanas y el señor Ruiz pagó tres coma cincuenta euros el doce "
                                   "de marzo de dos mil veinticuatro. El camino medía dieciocho kilómetros.")
    assert [change["rule"] for change in unit["normalization"]["changes"]] == ["abbreviation", "currency", "date", "unit"]
    assert unit["provenance"]["chapter"] == 1 and unit["provenance"]["paragraph"] == 0
    assert (Path(job_store.get_job(main.database_path, job_id)["source_path"]).parent
            / "extraction-report.json").is_file()


def test_an_invented_title_is_never_narrated(offline) -> None:
    client = TestClient(main.app)
    # No chapter headings: the parser labels the whole text with the file name.
    job_id = _job(client, "Un único párrafo sin capítulos que se narra entero.", "Mi libro.txt")
    texts = [item["text"] for item in job_store.get_job(main.database_path, job_id)["sentences"]]
    assert texts == ["Un único párrafo sin capítulos que se narra entero."]


def test_every_take_is_kept_with_its_evidence_and_a_second_recognizer_can_overrule(offline, monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "asr_secondary_provider", "deepgram")
    monkeypatch.setattr(main, "secondary_asr_provider", lambda: "deepgram")
    heard: dict[str, int] = {}

    async def first(_audio, sentence_id, text=""):
        heard[sentence_id] = heard.get(sentence_id, 0) + 1
        if "Amanecía" in text:
            return "Amanecía despacio sobre el río cuando Marta cerró la puerta. El aire olía a tierra mojada.", "rec-a"
        return main.build_spoken_text(text).spoken, "rec-a"

    async def second(_audio, _sentence_id, text=""):
        return main.build_spoken_text(text).spoken, "rec-b"

    monkeypatch.setattr(main, "_transcribe_sentence", first)
    monkeypatch.setattr(main, "_transcribe_secondary", second)
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    unit = _unit(job_id, "Amanecía")
    # The independent recognizer heard the text: no paid retake, recorded as such.
    assert unit["state"] == "verified" and unit["quality_retakes"] == 0
    assert unit["verification"] == "asr_secondary"
    assert unit["diagnosis"]["origin"] == "asr" and unit["diagnosis"]["action"] == "accept_flagged"
    attempts = client.get(f"/api/jobs/{job_id}/attempts", params={"sentence_id": unit["sentence_id"]}).json()
    assert attempts["total"] == 1
    attempt = attempts["attempts"][0]
    assert attempt["primary"]["engine"] == "rec-a" and attempt["secondary"]["engine"] == "rec-b"
    assert attempt["integrity"]["status"] in {"skipped", "passed"}
    job_dir = Path(job_store.get_job(main.database_path, job_id)["source_path"]).parent
    assert (job_dir / attempt["audio_path"]).is_file()
    queue = client.get(f"/api/jobs/{job_id}/review-queue").json()
    assert [item["sentence_id"] for item in queue["listening_recommended"]] == [unit["sentence_id"]]


def test_retakes_are_preserved_and_an_unresolved_unit_is_queued_with_a_diagnosis(offline, monkeypatch) -> None:
    takes = iter(range(100))

    async def wrong(_audio, _sentence_id, text=""):
        if "Amanecía" in text:
            return f"Una lectura totalmente distinta del libro, toma {next(takes)}.", "rec-a"
        return main.build_spoken_text(text).spoken, "rec-a"

    monkeypatch.setattr(main, "_transcribe_sentence", wrong)
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    unit = _unit(job_id, "Amanecía")
    attempts = job_store.get_attempts(main.database_path, job_id, unit["sentence_id"])
    assert [attempt["trigger"] for attempt in attempts] == ["first_take", "quality_retake", "quality_retake"]
    # Each retake asked for a different reading.
    assert len({attempt["provider"]["seed_requested"] for attempt in attempts}) == 3
    assert len({attempt["audio_path"] for attempt in attempts}) == 3
    queue = client.get(f"/api/jobs/{job_id}/review-queue").json()
    item = queue["items"][0]
    assert item["error_category"] == "synthesis" and item["severity"] == "critical"
    assert item["recommended_action"] and item["expected_text"] == unit["spoken_text"]
    gate = client.get(f"/api/jobs/{job_id}/release").json()
    # Simulated audio blocks the book outright; the unit waiting for a person
    # is listed as well, so nothing is hidden behind the first blocker.
    assert gate["state"] == release_gate.BLOCKED
    assert any("decisión humana" in reason for reason in gate["reviews"])


def test_a_correction_by_a_reviewer_renormalizes_the_text(offline) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    target = _unit(job_id, "147 ventanas")
    response = client.post(f"/api/jobs/{job_id}/review", json={
        "sentence_id": target["sentence_id"], "decision": "correct", "corrected_text": "Tenía 2,5 km de largo."})
    assert response.status_code == 200
    sentence = response.json()["sentence"]
    assert sentence["spoken_text"] == "Tenía dos coma cinco kilómetros de largo."
    assert sentence["normalization"]["changes"][0]["rule"] == "unit"
    attempts = job_store.get_attempts(main.database_path, job_id, target["sentence_id"])
    assert attempts[-1]["trigger"] == "human_correction"


def test_an_extraction_warning_can_be_acknowledged_by_name(offline) -> None:
    client = TestClient(main.app)
    job_id = _job(client, BOOK.replace("El aire olía", "Los distribui- dores llegaron. El aire olía"))
    status = client.get(f"/api/jobs/{job_id}/release").json()
    warning = status["dimensions"]["source"]["open_warnings"][0]
    assert warning["code"] == "broken_word"
    assert client.post(f"/api/jobs/{job_id}/acknowledge", json={"issue_id": warning["id"]}).status_code == 422
    after = client.post(f"/api/jobs/{job_id}/acknowledge", json={"issue_id": warning["id"], "reviewer": "Ana",
                                                                 "note": "Comprobado en el original"}).json()
    assert after["dimensions"]["source"]["open_warnings"] == []
    assert after["dimensions"]["source"]["acknowledged"] == 1


# --------------------------------------------------------------------------- #
# Provider behaviour that protects the budget
# --------------------------------------------------------------------------- #

def test_a_rate_limit_waits_as_long_as_the_provider_asks(monkeypatch) -> None:
    error = providers.ProviderError("rate_limit", "ocupado", permanent=False, retry_after=12)
    assert main.retry_delay(0, error) == 12
    delays = {round(main.retry_delay(2), 2) for _ in range(20)}
    assert all(4 <= delay <= 8 for delay in delays) and len(delays) > 1  # jittered


def test_a_text_longer_than_the_model_accepts_is_refused_before_it_is_sent() -> None:
    voice = providers.elevenlabs_voice("v", "Gabo", "es-ES", "eleven_v3")
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(providers.synthesize("a" * 5001, voice, api_key="k", model="eleven_v3"))
    assert error.value.permanent and error.value.kind == "bad_audio"


def test_the_voice_accent_is_taken_from_what_elevenlabs_verified(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "elevenlabs", "elevenlabs_api_key": "k", "elevenlabs_voice_id": "o0SveC0zgHFuCsEO3vHR",
        "elevenlabs_voice_language": "", "elevenlabs_model_id": "eleven_v4"}))
    monkeypatch.setattr(main, "voice_profiles", {})
    with pytest.raises(providers.ProviderError):
        main.resolve_narration_voice()  # unknown accent: refused
    main.voice_profiles["o0SveC0zgHFuCsEO3vHR"] = providers._voice_profile(
        "o0SveC0zgHFuCsEO3vHR", "Gabo", "peninsular",
        [{"model_id": "eleven_v4", "locale": "es-ES", "accent": "peninsular"},
         {"model_id": "eleven_turbo_v2_5", "locale": "es-ES", "accent": "es-andalusian"}], "shared_library")
    voice = main.resolve_narration_voice()
    assert voice.is_peninsular and voice.accent_known and voice.name == "Gabo"
    assert main.voice_profiles["o0SveC0zgHFuCsEO3vHR"]["peninsular_models"] == ["eleven_v4"]


def test_blank_voice_settings_from_docker_compose_mean_unset(monkeypatch) -> None:
    # Compose passes unset variables as "", which used to stop the server.
    monkeypatch.setenv("ELEVENLABS_STABILITY", "")
    monkeypatch.setenv("ELEVENLABS_STYLE", "0.3")
    configured = main.Settings()
    assert configured.elevenlabs_stability is None and configured.elevenlabs_style == 0.3


def test_downloads_are_named_after_the_book_title() -> None:
    assert main.book_download_name("El faro de Cabo Gris", "faro.xml") == "El faro de Cabo Gris.zip"
    # Accents stay; only what file systems forbid goes.
    assert main.book_download_name('¿Qué pasó: "aquí"? / II', "x.txt") == "¿Qué pasó aquí II.zip"
    assert main.book_download_name("", "becquer-los-ojos-verdes.txt") == "becquer-los-ojos-verdes.zip"
    assert main.book_download_name("Libro", "x", " (SIN VALIDAR)") == "Libro (SIN VALIDAR).zip"


def test_the_reviewed_delivery_downloads_with_the_book_title(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    monkeypatch.setattr(main, "compute_release", _real_audio(main.compute_release))
    download = client.get(f"/api/jobs/{job_id}/artifacts")
    assert download.status_code == 200
    assert 'filename="libro.zip"' in download.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# Where each difference is heard
# --------------------------------------------------------------------------- #

def _timed(text: str, start: float = 0.5, step: float = 0.4) -> list[dict]:
    return [{"text": word, "start": round(start + index * step, 2), "end": round(start + index * step + 0.3, 2)}
            for index, word in enumerate(text.split())]


def test_a_difference_is_located_on_the_words_heard_and_an_omission_on_its_gap() -> None:
    from app import qa_review

    expected = "El faro estaba muy lejos del pueblo."
    heard = "El faro estaba lejos del puerto."
    _verdict, issues, _confidence, _reference = qa_review.evaluate_against(expected, expected, heard)
    qa_review.locate_issues(issues, heard, _timed(heard))
    by_kind = {issue["kind"]: issue for issue in issues}
    # "muy" is missing between "estaba" (ends 1.6 s) and "lejos" (starts 1.7 s).
    assert (by_kind["delete"]["audio_start"], by_kind["delete"]["audio_end"]) == (1.6, 1.7)
    # "puerto" is the sixth word: 0.5 + 5 × 0.4 = 2.5 s.
    assert by_kind["replace"]["audio_start"] == 2.5 and by_kind["replace"]["audio_end"] == 2.8


def test_differences_are_placed_in_the_chapter_file_and_the_whole_book(offline, monkeypatch) -> None:
    async def heard(_audio, _sentence_id, text=""):
        spoken = main.build_spoken_text(text).spoken
        if "Amanecía" in text:
            spoken = spoken.replace("Marta", "Martha")  # same sound: accepted, but located
        # Mock audio lasts well under a second, so the word times are scaled to fit inside it.
        return main.TranscriptText(spoken, _timed(spoken, start=0.05, step=0.02)), "rec-a"

    monkeypatch.setattr(main, "_transcribe_sentence", heard)
    client = TestClient(main.app)
    job_id = _job(client)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed"
    job_dir = Path(job["source_path"]).parent
    locations = json.loads((job_dir / "fault-locations.json").read_text(encoding="utf-8"))
    assert len(locations) == 1
    location = locations[0]
    assert location["expected"] == "Marta" and location["heard"] == "Martha"
    # "Martha" is word 7 of the paragraph: 0.05 + 6 × 0.02 = 0.17 s into its own audio.
    assert location["paragraph_time"] == 0.17
    assert location["chapter"] == 1 and location["chapter_file"] == "audio/chapter-001/chapter-001.mp3"
    assert location["book_file"].endswith(".mp3") and location["book_time"] is not None
    # The paragraph follows the chapter heading, so it starts after 0 in the chapter file.
    assert location["chapter_time"] > 0.17 and location["book_time"] >= location["chapter_time"]
    assert location["book_clock"] == main.clock_label(location["book_time"])
    issue = next(item for item in _unit(job_id, "Amanecía")["issues"] if item.get("audio_start"))
    assert issue["book_time"] == location["book_time"]
    assert "Dónde se oyen las diferencias" in (job_dir / "quality-report.html").read_text(encoding="utf-8")
    assert client.get(f"/api/jobs/{job_id}").json()["fault_locations"] == locations


def test_book_clock_labels() -> None:
    assert main.clock_label(3.34) == "0:03.3"
    assert main.clock_label(125.06) == "2:05.1"
    assert main.clock_label(3723.4) == "1:02:03.4"
