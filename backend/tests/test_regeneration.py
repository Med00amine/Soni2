"""Regenerating the ZIP after an edit: what is redone, what is reused, what is removed.

Each scenario changes the saved draft the way step 2 does and produces again
through the endpoints the page uses. The checks are on what the reader gets
(the ZIP, read back and audited on its own) and on what was paid for (every
narration and transcription the provider would bill is counted), so a change
that made regeneration cheaper by dropping work it needed fails here.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audio_cache as audio_cache_module
from app import audio_integrity, audio_master, book_edits, main, package_integrity
from app import jobs as job_store

from tests.test_drafts_and_edits import BOOK, _save, _section, _slot_units, _texts, _upload, offline  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not audio_master.available(), reason="needs ffmpeg")


def _create(client: TestClient, draft_id: str, **data) -> dict:
    response = client.post("/api/books/jobs", data={"draft_id": draft_id, **data})
    assert response.status_code == 202, response.text
    return response.json()


def _produce(client: TestClient, draft_id: str, **data) -> dict:
    created = _create(client, draft_id, **data)
    if created["state"] != "completed":
        asyncio.run(main.run_production_job(created["job_id"]))
    job = job_store.get_job(main.database_path, created["job_id"])
    assert job["state"] == "completed", job["error"]
    return {**job, "created": created}


def _zip_text(job: dict) -> str:
    with zipfile.ZipFile(job["output_path"]) as archive:
        return "".join(archive.read(name).decode("utf-8") for name in archive.namelist()
                       if name.startswith("DAISY 3/") and name.endswith((".xml", ".ncx", ".opf", ".smil")))


def _audit(job: dict) -> dict:
    return package_integrity.audit_zip(Path(job["output_path"]),
                                       expected_titles=[chapter["title"] for chapter in job["chapters"]])


def _job_log(job: dict) -> str:
    return (Path(job["source_path"]).parent / "job.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Scenario A: removing a chapter
# --------------------------------------------------------------------------- #

def test_removing_a_chapter_takes_it_out_of_the_zip_and_pays_nothing(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])
    assert "Tercer capítulo, que se eliminará entero." in _zip_text(first)

    three = _section(view["outline"], "Capítulo 3")
    edits = {"slots": {unit["source"]["slot"]: [] for unit in three["units"] if (unit.get("source") or {}).get("slot")},
             "deleted_titles": [three["title_source"]["section"]]}
    _save(client, view, edits=edits)
    second = _produce(client, view["draft"]["draft_id"])

    # Nothing narrated or transcribed: every remaining unit was reused at once.
    assert len(offline["tts"]) == narrated and len(offline["asr"]) == transcribed
    assert all(unit["reused_audio"] and unit["state"] == "verified" for unit in second["sentences"])
    assert "units.reuse" in _job_log(second)
    text = _zip_text(second)
    assert "Tercer capítulo, que se eliminará entero." not in text and "Capítulo 3" not in text
    # The package is coherent on its own: no orphaned audio, every reference
    # resolves, and exactly the remaining sections in order.
    audit = _audit(second)
    assert audit["status"] == "passed", json.dumps(audit, ensure_ascii=False, indent=1)
    assert audit["package"]["sections"] == [chapter["title"] for chapter in second["chapters"]]
    assert len(second["chapters"]) == len(first["chapters"]) - 1
    with zipfile.ZipFile(second["output_path"]) as archive:
        chapters = [name for name in archive.namelist() if re.search(r"/chapter-\d{3}\.mp3$", name)]
    assert len(chapters) == len(second["chapters"])
    # The chapter's audio stays in the shared cache: bringing it back costs nothing.
    for unit in first["sentences"]:
        if unit["chapter_title"] == "Capítulo 3":
            key = main.narration_cache_key(unit["synthesis_text"], first["voice_speed"], first["voice_id"])
            assert main.audio_cache.contains(key)


@needs_ffmpeg
def test_only_the_files_a_deletion_changes_are_mastered_again(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    _produce(client, view["draft"]["draft_id"])
    two = _section(view["outline"], "Capítulo 2")
    edits = {"slots": {unit["source"]["slot"]: [] for unit in two["units"] if (unit.get("source") or {}).get("slot")},
             "deleted_titles": [two["title_source"]["section"]]}
    _save(client, view, edits=edits)
    second = _produce(client, view["draft"]["draft_id"])
    log = _job_log(second)
    rebuilt = [line for line in log.splitlines() if "rebuilt chapter audio" in line]
    # The index lists the chapters, so it changes; every other file is reused.
    assert len(rebuilt) == 1 and "ch=003" in rebuilt[0]
    pipeline = main._pipeline_view(second)
    assert pipeline["chapters"]["rebuilt"] == 1 and pipeline["chapters"]["reused"] == len(second["chapters"]) - 1
    assert pipeline["units"]["reused"] == len(second["sentences"]) and pipeline["units"]["to_narrate"] == 0


# --------------------------------------------------------------------------- #
# Scenarios C and D: removing, adding and correcting a sentence
# --------------------------------------------------------------------------- #

def test_correcting_one_phrase_narrates_and_checks_only_that_phrase(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])
    one = _section(view["outline"], "Capítulo 1")
    slot = one["units"][1]["source"]["slot"]
    _save(client, view, edits={"slots": {slot: ["Primer párrafo del uno, corregido. Tiene dos frases."]}})
    second = _produce(client, view["draft"]["draft_id"])
    assert offline["tts"][narrated:] == ["Primer párrafo del uno, corregido. Tiene dos frases."]
    assert len(offline["asr"]) == transcribed + 1
    changed = [unit for unit in second["sentences"] if not unit["reused_audio"]]
    assert [unit["text"] for unit in changed] == ["Primer párrafo del uno, corregido. Tiene dos frases."]
    text = _zip_text(second)
    assert "Primer párrafo del uno, corregido." in text and "Primer párrafo del uno. Tiene dos frases." not in text
    assert _audit(second)["status"] == "passed"
    assert len(first["sentences"]) == len(second["sentences"])


def test_adding_a_phrase_narrates_it_where_it_was_added(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    _produce(client, view["draft"]["draft_id"])
    narrated = len(offline["tts"])
    two = _section(view["outline"], "Capítulo 2")
    slot = two["units"][1]["source"]["slot"]
    saved = _save(client, view, edits={"slots": {f"{slot}+": ["Una frase nueva, añadida después."]}})
    second = _produce(client, view["draft"]["draft_id"])
    assert offline["tts"][narrated:] == ["Una frase nueva, añadida después."]
    texts = [unit["text"] for unit in second["sentences"]]
    assert texts.index("Una frase nueva, añadida después.") == texts.index("Segundo capítulo, primer párrafo.") + 1
    assert texts == _texts(saved["outline"])
    assert _audit(second)["status"] == "passed"


def test_removing_a_phrase_leaves_no_trace_of_it_in_the_zip(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])
    two = _section(view["outline"], "Capítulo 2")
    slot = two["units"][2]["source"]["slot"]
    _save(client, view, edits={"slots": {slot: []}})
    second = _produce(client, view["draft"]["draft_id"])
    assert len(offline["tts"]) == narrated and len(offline["asr"]) == transcribed
    assert "que se borrará" not in _zip_text(second)
    assert len(second["sentences"]) == len(first["sentences"]) - 1
    audit = _audit(second)
    assert audit["status"] == "passed"
    # The package's audio is exactly what its SMIL files play: the removed
    # phrase's audio is in no file of the ZIP.
    assert {check["id"]: check["status"] for check in audit["checks"]}["zip_audio"] == "passed"


# --------------------------------------------------------------------------- #
# Scenario B: adding a chapter
# --------------------------------------------------------------------------- #

def _added(after: str, title: str = "Capítulo añadido", paragraphs: list[str] | None = None,
           identifier: str = "added-1a2b3c4d") -> dict:
    return {"id": identifier, "after": after, "title": title,
            "paragraphs": paragraphs or ["Primer párrafo del capítulo nuevo.", "Segundo párrafo, también nuevo."]}


def test_adding_a_chapter_narrates_it_in_its_place_with_an_index_entry(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    _produce(client, view["draft"]["draft_id"])
    narrated = len(offline["tts"])
    one = _section(view["outline"], "Capítulo 1")
    anchor = one["title_source"]["section"]
    saved = _save(client, view, edits={"slots": {}, "titles": {}, "deleted_titles": [],
                                       "added_sections": [_added(anchor)]})
    titles = [section["title"] for section in saved["outline"]["sections"]]
    assert titles.index("Capítulo añadido") == titles.index("Capítulo 1") + 1
    index = next(section for section in saved["outline"]["sections"] if section["kind"] == "once_index")
    assert any("Capítulo añadido" in unit["text"] for unit in index["units"])

    second = _produce(client, view["draft"]["draft_id"])
    new = offline["tts"][narrated:]
    # Its heading, its two paragraphs, and the index entry that now lists it.
    assert "Primer párrafo del capítulo nuevo." in new and "Segundo párrafo, también nuevo." in new
    assert "Capítulo añadido" in new
    added = [chapter["title"] for chapter in second["chapters"]]
    assert added.index("Capítulo añadido") == added.index("Capítulo 1") + 1
    audit = _audit(second)
    assert audit["status"] == "passed", audit["detail"]
    assert "Capítulo añadido" in audit["package"]["sections"]


def test_an_added_chapter_is_edited_like_any_other_and_can_be_removed(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    anchor = _section(view["outline"], "Capítulo 2")["title_source"]["section"]
    added = _added(anchor)
    saved = _save(client, view, edits={"slots": {}, "titles": {}, "deleted_titles": [], "added_sections": [added]})
    section = _section(saved["outline"], "Capítulo añadido")
    # Its paragraphs have slots of their own, like the book's.
    slot = section["units"][1]["source"]["slot"]
    assert slot == "added-1a2b3c4d:0"
    revised = {"slots": {slot: ["Primer párrafo corregido."]}, "titles": {"added-1a2b3c4d": "Capítulo renombrado"},
               "deleted_titles": [], "added_sections": [added]}
    saved = _save(client, saved, edits=revised)
    assert "Primer párrafo corregido." in _texts(saved["outline"])
    assert any(section["title"] == "Capítulo renombrado" for section in saved["outline"]["sections"])
    job = _produce(client, view["draft"]["draft_id"])
    assert "Capítulo renombrado" in _zip_text(job) and "Primer párrafo corregido." in _zip_text(job)
    # Taken out again: no longer in the book.
    saved = _save(client, saved, edits={"slots": {}, "titles": {}, "deleted_titles": []})
    assert not any("Capítulo" in section["title"] and "renombrado" in section["title"]
                   for section in saved["outline"]["sections"])
    job = _produce(client, view["draft"]["draft_id"])
    assert "Capítulo renombrado" not in _zip_text(job) and _audit(job)["status"] == "passed"


def test_added_chapters_are_checked_before_they_are_saved(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    anchor = _section(view["outline"], "Capítulo 1")["title_source"]["section"]
    for added in ([_added("section-99")],                              # after a section the book does not have
                  [_added(anchor, paragraphs=["  "])],                 # no text
                  [{**_added(anchor), "id": "nuevo"}],                 # not an added id
                  [_added("added-bbbbbbbb", identifier="added-aaaaaaaa"),
                   _added("added-aaaaaaaa", identifier="added-bbbbbbbb")]):  # placed after each other in a circle
        state = {**view["draft"]["state"], "edits": {"slots": {}, "added_sections": added}}
        response = client.put(f"/api/drafts/{view['draft']['draft_id']}",
                              json={"state": state, "revision": view["draft"]["revision"]})
        assert response.status_code == 422, added


def test_a_chapter_added_after_a_left_out_section_keeps_that_place() -> None:
    from app.narration_plan import SelectedChapter

    def chapter(section: str, title: str) -> SelectedChapter:
        selected = SelectedChapter(title, [f"Texto de {title}."])
        selected.section_id = section
        return selected

    edits = book_edits.parse(json.dumps({"added_sections": [
        _added("section-2", "Tras el dos", identifier="added-00000002"),
        _added("", "Al principio", identifier="added-00000000"),
        _added("added-00000002", "Tras el añadido", identifier="added-00000003")]}))
    # section-2 is not selected: what was added after it goes where it would be.
    chapters, _ = book_edits.apply([chapter("section-1", "Uno"), chapter("section-3", "Tres")], edits,
                                   order=["section-1", "section-2", "section-3"])
    assert [title for title, _ in chapters] == ["Al principio", "Uno", "Tras el dos", "Tras el añadido", "Tres"]


# --------------------------------------------------------------------------- #
# The two buttons of step 3: reuse a ZIP, rebuild it, never twice at once
# --------------------------------------------------------------------------- #

def test_downloading_again_hands_back_the_same_zip_and_regenerating_reuses_everything(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])

    # «Descargar ZIP»: the ZIP of exactly this text is already there.
    again = _create(client, view["draft"]["draft_id"])
    assert again["job_id"] == first["job_id"] and again["reused_package"] and again["state"] == "completed"

    # «Regenerar ZIP»: a new package from the latest text, reusing every unit.
    rebuilt = _produce(client, view["draft"]["draft_id"], force="true")
    assert rebuilt["job_id"] != first["job_id"]
    assert len(offline["tts"]) == narrated and len(offline["asr"]) == transcribed
    assert _audit(rebuilt)["status"] == "passed"
    # The new package replaces the old one, which is removed from the disk.
    old = job_store.get_job(main.database_path, first["job_id"])
    assert not old["output_path"] and not Path(first["output_path"]).exists()
    assert client.get(f"/api/jobs/{first['job_id']}/artifacts", params={"diagnostic": "true"}).status_code == 409
    # And the draft points at the job that has its ZIP.
    assert client.get(f"/api/drafts/{view['draft']['draft_id']}").json()["draft"]["last_job"]["job_id"] == rebuilt["job_id"]


def test_two_requests_for_the_same_book_share_one_production(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _create(client, view["draft"]["draft_id"])
    second = _create(client, view["draft"]["draft_id"], force="true")
    assert second["job_id"] == first["job_id"] and second["reused_existing_job"]
    with __import__("sqlite3").connect(main.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0] == 1


def test_a_production_waiting_for_a_decision_is_not_paid_for_again(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    real = main._transcribe_sentence

    async def mishears(audio, sentence_id, text=""):
        heard, engine = await real(audio, sentence_id, text)
        return ("Algo muy distinto que no se dijo nunca." if "Primer párrafo" in text else heard), engine

    monkeypatch.setattr(main, "_transcribe_sentence", mishears)
    created = _create(client, view["draft"]["draft_id"])
    asyncio.run(main.run_production_job(created["job_id"]))
    assert job_store.get_job(main.database_path, created["job_id"])["state"] == "partially_completed"
    transcribed = len(offline["asr"])
    # Both buttons answer with the job that waits for the reader's decision.
    for data in ({}, {"force": "true"}):
        again = _create(client, view["draft"]["draft_id"], **data)
        assert again["job_id"] == created["job_id"] and again["state"] == "partially_completed"
    assert len(offline["asr"]) == transcribed


def test_a_changed_text_is_never_answered_with_the_old_zip(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    slot = _section(view["outline"], "Capítulo 2")["units"][1]["source"]["slot"]
    _save(client, view, edits={"slots": {slot: ["Otro texto distinto."]}})
    created = _create(client, view["draft"]["draft_id"])
    assert created["job_id"] != first["job_id"] and not created.get("reused_package")


# --------------------------------------------------------------------------- #
# What is reused is only what cannot have changed
# --------------------------------------------------------------------------- #

def test_an_integrity_report_is_reused_only_for_the_same_audio_text_and_analysis(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "audio_cache", audio_cache_module.AudioCache(tmp_path / "cache"))
    monkeypatch.setattr(main, "active_tts_provider", lambda: "elevenlabs")
    calls: list[tuple[bytes, str]] = []

    def assess(audio: bytes, text: str, **_kwargs) -> dict:
        calls.append((audio, text))
        return {"status": "passed", "signal": {"duration_seconds": 1.0}, "stream": {}, "reasons": []}

    monkeypatch.setattr(audio_integrity, "assess", assess)
    key = "a" * 64

    def analyze(audio: bytes, text: str) -> dict:
        return asyncio.run(main._analyze_audio(audio, text, cache_key=key))

    analyze(b"audio-one", "Una frase.")
    analyze(b"audio-one", "Una frase.")
    assert len(calls) == 1
    analyze(b"audio-one", "Otra frase.")      # other text
    analyze(b"audio-two", "Una frase.")       # other audio (a retake)
    assert len(calls) == 3
    monkeypatch.setattr(audio_integrity, "ANALYSIS_VERSION", "another-analysis")
    analyze(b"audio-one", "Una frase.")       # another analysis
    assert len(calls) == 4


def test_an_analysis_without_ffmpeg_is_not_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "audio_cache", audio_cache_module.AudioCache(tmp_path / "cache"))
    monkeypatch.setattr(main, "active_tts_provider", lambda: "elevenlabs")
    calls = []

    def assess(audio: bytes, text: str, **_kwargs) -> dict:
        calls.append(audio)
        return {"status": "passed", "signal": {"status": "unavailable", "reason": "ffmpeg no está instalado"},
                "stream": {}, "reasons": []}

    monkeypatch.setattr(audio_integrity, "assess", assess)
    for _ in range(2):
        asyncio.run(main._analyze_audio(b"audio", "Texto.", cache_key="b" * 64))
    assert len(calls) == 2


def test_another_voice_narrates_everything_again(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated = len(offline["tts"])
    _save(client, view, voice_id="mock-es-mx-male")
    monkeypatch_voice = main.settings.model_copy(update={"require_peninsular_voice": False})
    main.settings, saved_settings = monkeypatch_voice, main.settings
    try:
        response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"],
                                                        "voice_id": "mock-es-mx-male"})
        assert response.status_code == 202, response.text
        asyncio.run(main.run_production_job(response.json()["job_id"]))
    finally:
        main.settings = saved_settings
    second = job_store.get_job(main.database_path, response.json()["job_id"])
    assert second["state"] == "completed"
    # Every distinct text narrated again with the new voice; nothing of the old voice reused.
    assert len(offline["tts"]) - narrated == len({unit["synthesis_text"] for unit in first["sentences"]})
    texts = [unit["synthesis_text"] for unit in second["sentences"]]
    # Only a text the book says twice (the ONCE files repeat the title) comes
    # from the cache, filled moments before by its first occurrence.
    assert all(texts.count(unit["synthesis_text"]) > 1 for unit in second["sentences"] if unit["reused_audio"])


# --------------------------------------------------------------------------- #
# What the page polls, and the record of a build
# --------------------------------------------------------------------------- #

def test_the_page_polls_a_light_status_that_names_the_real_stages(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    job = _produce(client, view["draft"]["draft_id"])
    full = client.get(f"/api/jobs/{job['job_id']}").json()
    live = client.get(f"/api/jobs/{job['job_id']}", params={"view": "live"}).json()
    assert "events" in full and "events" not in live
    assert len(json.dumps(live)) < len(json.dumps(full)) / 2
    unit = live["sentences"][0]
    assert {"sentence_id", "text", "state", "audio_path", "issues", "updated_at"} <= set(unit)
    assert "metrics" not in unit and "normalization" not in unit
    pipeline = live["pipeline"]
    assert pipeline["stage"] == "completed" and not pipeline["running"]
    assert {"checking", "narrating", "chapters", "package", "validating", "zipping", "verifying"} <= set(pipeline["timings"])
    assert live["package"]["audit"] == "passed" and live["package"]["bytes"] > 0
    log = _job_log(job)
    assert "perf.stage: chapters finished" in log and "perf.summary: build timings" in log


# --------------------------------------------------------------------------- #
# The ZIP audit on its own
# --------------------------------------------------------------------------- #

def test_the_zip_audit_finds_what_a_broken_package_lacks_or_carries(offline, tmp_path) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    job = _produce(client, view["draft"]["draft_id"])
    good = Path(job["output_path"])
    titles = [chapter["title"] for chapter in job["chapters"]]
    assert package_integrity.audit_zip(good, expected_titles=titles)["status"] == "passed"

    def rewrite(name: str, drop: str | None = None, add: tuple[str, bytes] | None = None,
                change: tuple[str, str, str] | None = None) -> dict:
        target = tmp_path / name
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(target, "w") as archive:
            for item in source.infolist():
                if drop and item.filename.endswith(drop):
                    continue
                data = source.read(item.filename)
                if change and item.filename.endswith(change[0]):
                    data = data.decode("utf-8").replace(change[1], change[2]).encode("utf-8")
                archive.writestr(item, data)
            if add:
                archive.writestr(*add)
        result = package_integrity.audit_zip(target, expected_titles=titles)
        return {check["id"]: check["status"] for check in result["checks"]} | {"status": result["status"]}

    assert rewrite("missing.zip", drop="/chapter-002.mp3")["zip_references"] == "failed"
    stray = rewrite("stray.zip", add=("DAISY 3/chapter-099.mp3", b"\xff\xfb\x90\x64" + bytes(500)))
    assert stray["zip_manifest"] == "failed" and stray["zip_audio"] == "failed"
    renamed = rewrite("renamed.zip", change=(".ncx", "Capítulo 2", "Capítulo fantasma"))
    assert renamed["zip_chapters"] == "failed"
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(good.read_bytes()[:-4096])
    result = package_integrity.audit_zip(truncated)
    assert result["status"] == "failed" and not result["readable"]


# --------------------------------------------------------------------------- #
# Edges measured on a window give what a full decode gives
# --------------------------------------------------------------------------- #

@needs_ffmpeg
def test_edges_measured_on_windows_match_a_full_decode(tmp_path, monkeypatch) -> None:
    ffmpeg = audio_master.ffmpeg_path()
    path = tmp_path / "long.mp3"
    # 1.3 s of silence, 30 s of tone, 1.7 s of silence: longer than two windows.
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100:duration=30",
                    "-af", "adelay=1300:all=1,apad=pad_dur=1.7", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k",
                    "-write_xing", "0", str(path)], check=True, capture_output=True)
    audio = path.read_bytes()
    windowed = audio_master.edge_silence(audio)
    monkeypatch.setattr(audio_master, "EDGE_WINDOW_SECONDS", 1000.0)
    full = audio_master.edge_silence(audio)
    assert windowed is not None and full is not None
    assert windowed.leading == pytest.approx(full.leading, abs=0.002)
    assert windowed.trailing == pytest.approx(full.trailing, abs=0.002)
    assert windowed.leading == pytest.approx(1.3, abs=0.05) and windowed.trailing == pytest.approx(1.7, abs=0.06)
    # A file silent all along its first window is measured in full instead.
    silent = tmp_path / "quiet.mp3"
    subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100:duration=10", "-af", "adelay=12000:all=1",
                    "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0", str(silent)],
                   check=True, capture_output=True)
    monkeypatch.setattr(audio_master, "EDGE_WINDOW_SECONDS", 8.0)
    edges = audio_master.edge_silence(silent.read_bytes())
    assert edges.leading == pytest.approx(12.0, abs=0.05)


# --------------------------------------------------------------------------- #
# The direct download: the same reuse, the same checks
# --------------------------------------------------------------------------- #

def _direct(client: TestClient, draft_id: str, **data) -> dict:
    response = client.post("/api/books/package-tasks", data={"draft_id": draft_id, **data})
    assert response.status_code == 202, response.text
    status = response.json()
    while status["state"] == "running":
        status = client.get(f"/api/books/package-tasks/{status['task_id']}").json()
    assert status["state"] == "completed", status
    return status


def test_the_direct_zip_is_reused_rebuilt_from_cached_chapters_and_audited(offline) -> None:
    with TestClient(main.app) as client:
        view = _upload(client)
        _save(client, view, once_enabled=False)
        first = _direct(client, view["draft"]["draft_id"])
        assert first["package"]["audit"] == "passed"
        narrated = len(offline["tts"])
        # The same request: the same task and ZIP.
        again = _direct(client, view["draft"]["draft_id"])
        assert again["task_id"] == first["task_id"] and again["reused_package"]
        # Built again: nothing narrated; with ffmpeg, every chapter from the cache.
        rebuilt = _direct(client, view["draft"]["draft_id"], force="true")
        assert rebuilt["task_id"] != first["task_id"] and len(offline["tts"]) == narrated
        if audio_master.available():
            assert rebuilt["chapters_reused"] == rebuilt["chapters_total"] and rebuilt["chapters_rebuilt"] == 0
        download = client.get(f"/api/books/package-tasks/{rebuilt['task_id']}/download")
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            names = archive.namelist()
        # Its quality status sits beside the package, not inside it.
        assert "quality-status.json" in names and "DAISY 3/quality-status.json" not in names
