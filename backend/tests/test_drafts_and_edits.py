"""Step 2 edits are the book: saved, restored, produced, and paid for only once.

The workflow these tests walk through is the one a person follows: upload a
book, review it, add, correct and delete phrases, delete a chapter, leave and
come back (a reload), produce the audiobook and download it. Every change
must be in the final ZIP, nothing may fall back to the extracted text, and
producing the edited book again must pay only for what is new.
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import audio_integrity, audio_master, book_edits, main, providers
from app import jobs as job_store
from app.audio_cache import AudioCache
from app.daisy_validation import validate_daisy_package

LONG = " ".join(f"Frase larga número {number} del párrafo extenso, con algo de texto para llenarla bien."
                for number in range(1, 25))
BOOK = f"""Capítulo 1

Primer párrafo del uno. Tiene dos frases.

{LONG}

Capítulo 2

Segundo capítulo, primer párrafo.

Segundo capítulo, segundo párrafo que se borrará.

Capítulo 3

Tercer capítulo, que se eliminará entero.
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
    monkeypatch.setattr(main, "_parsed_books", {})
    tts: list[str] = []
    asr: list[str] = []
    real_synthesize = main.synthesize
    real_transcribe = main._transcribe_sentence

    async def counting_synthesize(text, voice_speed=1.0, voice_id="", **kwargs):
        tts.append(text)  # every call here is one the provider would bill
        return await real_synthesize(text, voice_speed, voice_id, **kwargs)

    async def counting_transcribe(audio, sentence_id, text=""):
        asr.append(text)
        return await real_transcribe(audio, sentence_id, text)

    monkeypatch.setattr(main, "synthesize", counting_synthesize)
    monkeypatch.setattr(main, "_transcribe_sentence", counting_transcribe)
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    return {"tts": tts, "asr": asr, "tmp": tmp_path}


def _upload(client: TestClient) -> dict:
    response = client.post("/api/drafts", files={"file": ("libro.txt", io.BytesIO(BOOK.encode("utf-8")),
                                                          "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def _save(client: TestClient, view: dict, **changes) -> dict:
    state = {**view["draft"]["state"], **changes}
    response = client.put(f"/api/drafts/{view['draft']['draft_id']}",
                          json={"state": state, "revision": view["draft"]["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def _units(outline: dict) -> list[dict]:
    return [unit for section in outline["sections"] for unit in section["units"]]


def _texts(outline: dict) -> list[str]:
    return [unit["text"] for unit in _units(outline)]


def _section(outline: dict, title: str) -> dict:
    return next(section for section in outline["sections"] if section["title"] == title)


def _slot_units(outline: dict, slot: str) -> list[str]:
    return [unit["text"] for unit in _units(outline)
            if (unit.get("source") or {}).get("slot") == slot and unit["role"] != "heading"]


def _produce(client: TestClient, draft_id: str) -> dict:
    response = client.post("/api/books/jobs", data={"draft_id": draft_id})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    return job


def _edits(view: dict) -> dict:
    """Add, correct and delete phrases, delete a chapter: what a reader does in step 2."""
    outline = view["outline"]
    one, two, three = (_section(outline, title) for title in ("Capítulo 1", "Capítulo 2", "Capítulo 3"))
    first_slot = one["units"][1]["source"]["slot"]
    long_slot = one["units"][2]["source"]["slot"]
    long_parts = _slot_units(outline, long_slot)
    assert len(long_parts) >= 3, "the long paragraph is narrated in several units"
    keep_slot, drop_slot = (unit["source"]["slot"] for unit in two["units"][1:3])
    return {
        "slots": {
            # Correct one phrase.
            first_slot: ["Primer párrafo del uno, corregido. Tiene dos frases."],
            # Delete the middle unit of a long paragraph: the others stay as they were.
            long_slot: [long_parts[0], *long_parts[2:]],
            # Add a phrase after a paragraph.
            f"{keep_slot}+": ["Una frase nueva, añadida en el paso dos."],
            # Delete a paragraph.
            drop_slot: [],
            # Delete chapter 3: its paragraphs...
            **{unit["source"]["slot"]: [] for unit in three["units"] if (unit.get("source") or {}).get("slot")},
        },
        # ...and its title.
        "deleted_titles": [three["title_source"]["section"]],
    }, long_parts


# --------------------------------------------------------------------------- #
# Editing and persistence
# --------------------------------------------------------------------------- #

def test_every_edit_is_in_the_outline_and_survives_a_reload(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    assert view["draft"]["revision"] == 1 and view["outline"]
    edits, long_parts = _edits(view)
    saved = _save(client, view, edits=edits)
    assert saved["draft"]["revision"] == 2
    texts = _texts(saved["outline"])
    assert "Primer párrafo del uno, corregido. Tiene dos frases." in texts
    assert "Primer párrafo del uno. Tiene dos frases." not in texts
    assert "Una frase nueva, añadida en el paso dos." in texts
    assert "Segundo capítulo, segundo párrafo que se borrará." not in texts
    assert long_parts[1] not in texts and long_parts[0] in texts and long_parts[2] in texts
    # ONCE is on by default: the three initial files, then the chapters left.
    sections = saved["outline"]["sections"]
    assert [section["kind"] for section in sections[:3]] == ["once_title", "once_bibliographic", "once_index"]
    assert [section["title"] for section in sections[3:]] == ["Capítulo 1", "Capítulo 2"]
    # The index no longer lists the deleted chapter, and the closing phrase moved.
    index = next(section for section in sections if section["kind"] == "once_index")
    assert not any("Capítulo 3" in unit["text"] for unit in index["units"])
    assert sections[-1]["units"][-1]["role"] == "closing"
    # The initial files cannot be deleted.
    assert [section["deletable"] for section in sections] == [False, False, False, True, True]
    # The phrase added comes right after the paragraph it was added to.
    assert texts.index("Una frase nueva, añadida en el paso dos.") == texts.index("Segundo capítulo, primer párrafo.") + 1

    # A reload (or another visit) finds the book exactly as it was left.
    restored = client.get(f"/api/drafts/{view['draft']['draft_id']}").json()
    assert restored["draft"]["state"]["edits"]["slots"] == saved["draft"]["state"]["edits"]["slots"]
    assert _texts(restored["outline"]) == texts


def test_a_stale_tab_cannot_overwrite_newer_edits(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    _save(client, view, voice_id="mock-es-es-female")
    stale = client.put(f"/api/drafts/{view['draft']['draft_id']}",
                       json={"state": view["draft"]["state"], "revision": view["draft"]["revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["draft"]["draft"]["revision"] == 2


def test_edits_for_another_version_of_the_book_are_refused_not_dropped(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    for edits in ({"slots": {"section-99:0": ["x"]}}, {"slots": {"section-1:999": ["x"]}},
                  {"slots": {"nonsense": ["x"]}}):
        state = {**view["draft"]["state"], "edits": edits}
        response = client.put(f"/api/drafts/{view['draft']['draft_id']}",
                              json={"state": state, "revision": view["draft"]["revision"]})
        assert response.status_code == 422, edits
    # Nothing was saved.
    assert client.get(f"/api/drafts/{view['draft']['draft_id']}").json()["draft"]["revision"] == 1


def test_leaving_no_section_selected_is_saved_and_explained(offline) -> None:
    # A normal moment while choosing sections, not an error worth losing.
    client = TestClient(main.app)
    view = _upload(client)
    saved = _save(client, view, selected_ids=[])
    assert saved["outline"] is None and "Selecciona al menos una sección" in saved["outline_error"]


def test_a_missing_draft_says_so(offline) -> None:
    client = TestClient(main.app)
    assert client.get("/api/drafts/does-not-exist").status_code == 404
    assert client.post("/api/books/jobs", data={"draft_id": "does-not-exist"}).status_code == 404


def test_deleting_everything_is_refused_with_a_reason(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    slots = {unit["source"]["slot"]: [] for unit in _units(view["outline"])
             if (unit.get("source") or {}).get("slot")}
    titles = [section["title_source"]["section"] for section in view["outline"]["sections"]
              if section["deletable"]]
    state = {**view["draft"]["state"], "edits": {"slots": slots, "deleted_titles": titles}}
    refused = client.put(f"/api/drafts/{view['draft']['draft_id']}",
                         json={"state": state, "revision": view["draft"]["revision"]})
    assert refused.status_code == 422 and "no queda nada que narrar" in refused.json()["detail"]
    # Nothing was saved: the book is still there to come back to.
    restored = client.get(f"/api/drafts/{view['draft']['draft_id']}").json()
    assert restored["draft"]["revision"] == 1 and restored["outline"]


# --------------------------------------------------------------------------- #
# The edited text is what is produced, and the download carries it
# --------------------------------------------------------------------------- #

def test_the_production_narrates_the_saved_edits_and_the_zip_carries_them(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    edits, long_parts = _edits(view)
    saved = _save(client, view, edits=edits)
    # Whatever the request says, a draft production narrates the saved draft.
    response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"],
                                                    "corrections_json": json.dumps({"0:0": "Otra cosa."})})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job["error"]
    assert job["draft_id"] == view["draft"]["draft_id"] and job["draft_revision"] == 2
    assert [unit["text"] for unit in job["sentences"]] == _texts(saved["outline"])

    with zipfile.ZipFile(job["output_path"]) as archive:
        assert archive.testzip() is None
        book = next(name for name in archive.namelist() if name.startswith("DAISY 3/") and name.endswith(".xml"))
        text = archive.read(book).decode("utf-8")
        reviewed = json.loads(archive.read("reviewed-source.json"))
    assert "Primer párrafo del uno, corregido." in text and "Una frase nueva, añadida en el paso dos." in text
    for gone in ("Primer párrafo del uno. Tiene", "que se borrará", "Tercer capítulo", long_parts[1]):
        assert gone not in text
    assert reviewed["draft_revision"] == 2 and reviewed["edits"]["slots"]


def test_the_direct_download_narrates_the_saved_edits_too(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    edits, _long = _edits(view)
    # Without the ONCE structure: the book's own chapters only.
    _save(client, view, edits=edits, once_enabled=False)
    response = client.post("/api/books/package", data={"draft_id": view["draft"]["draft_id"]})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        book = next(name for name in archive.namelist() if name.startswith("DAISY 3/") and name.endswith(".xml"))
        text = archive.read(book).decode("utf-8")
        audio = sorted(name for name in archive.namelist() if name.startswith("DAISY 3/audio/"))
    assert "Una frase nueva, añadida en el paso dos." in text and "Tercer capítulo" not in text
    assert audio == ["DAISY 3/audio/chapter-001.mp3", "DAISY 3/audio/chapter-002.mp3"]


def test_the_preview_reads_the_edited_text(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    edits, _long = _edits(view)
    _save(client, view, edits=edits)
    preview = client.post("/api/books/preview", data={"draft_id": view["draft"]["draft_id"]}).json()
    assert "Primer párrafo del uno, corregido." in preview["transcript"]
    assert "Primer párrafo del uno. Tiene" not in preview["transcript"]


def test_a_running_job_over_the_old_text_is_not_reused_for_the_edited_one(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]}).json()
    # Still queued: a second click with nothing changed joins it.
    again = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]}).json()
    assert again["job_id"] == first["job_id"] and again["reused_existing_job"]
    edits, _long = _edits(view)
    _save(client, view, edits=edits)
    edited = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]}).json()
    assert edited["job_id"] != first["job_id"] and not edited.get("reused_existing_job")


# --------------------------------------------------------------------------- #
# Paying only for what changed
# --------------------------------------------------------------------------- #

def test_producing_the_edited_book_again_pays_only_for_new_and_changed_phrases(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])
    # One narration per distinct text: the ONCE files say the title twice, and
    # the second time comes from the cache.
    assert narrated == len({unit["synthesis_text"] for unit in first["sentences"]})
    assert narrated <= transcribed <= len(first["sentences"])

    edits, _long = _edits(view)
    saved = _save(client, view, edits=edits)
    second = _produce(client, view["draft"]["draft_id"])
    new_tts = offline["tts"][narrated:]
    new_asr = offline["asr"][transcribed:]
    # One phrase corrected, one added: two narrations, two transcriptions.
    # Deleting a phrase, a paragraph and a chapter cost nothing; every other
    # phrase came from the cache with its earlier verdict.
    assert sorted(new_tts) == sorted(["Primer párrafo del uno, corregido. Tiene dos frases.",
                                      "Una frase nueva, añadida en el paso dos."])
    assert len(new_asr) == 2
    reused = [unit for unit in second["sentences"] if unit["reused_audio"]]
    assert len(reused) == len(second["sentences"]) - 2
    assert [unit["text"] for unit in second["sentences"]] == _texts(saved["outline"])

    # Producing it a third time, unchanged, costs nothing at all.
    _produce(client, view["draft"]["draft_id"])
    assert len(offline["tts"]) == narrated + 2 and len(offline["asr"]) == transcribed + 2


@pytest.mark.skipif(not audio_master.available(), reason="mastering needs ffmpeg")
def test_an_untouched_chapter_is_not_mastered_again_in_the_next_production(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    _produce(client, view["draft"]["draft_id"])
    two = _section(view["outline"], "Capítulo 2")
    slot = two["units"][2]["source"]["slot"]
    _save(client, view, edits={"slots": {slot: []}})
    second = _produce(client, view["draft"]["draft_id"])
    log = (Path(second["source_path"]).parent / "job.log").read_text(encoding="utf-8")
    shared = [line for line in log.splitlines() if "reused chapter mastered by an earlier job" in line]
    rebuilt = [line for line in log.splitlines() if "rebuilt chapter audio" in line]
    # Only chapter 2 changed: every other file (the three initial ONCE files,
    # chapters 1 and 3) is the same passages as before and is taken as it was.
    assert len(rebuilt) == 1 and "ch=005" in rebuilt[0]
    assert len(shared) == len(second["chapters"]) - 1


def test_a_reviewer_acceptance_is_remembered_for_the_next_production(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    real = main._transcribe_sentence

    async def mishears_first(audio, sentence_id, text=""):
        heard, engine = await real(audio, sentence_id, text)
        return (("Algo muy distinto que no se dijo nunca." if "Primer párrafo" in text else heard), engine)

    monkeypatch.setattr(main, "_transcribe_sentence", mishears_first)
    response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]})
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    flagged = next(unit for unit in job_store.get_job(main.database_path, job_id)["sentences"]
                   if unit["state"] == "needs_human_review")
    accepted = client.post(f"/api/jobs/{job_id}/review",
                           json={"sentence_id": flagged["sentence_id"], "decision": "accept"})
    assert accepted.status_code == 200, accepted.text
    # The next production of the same text: the person is not asked again.
    _save(client, view, voice_id="mock-es-es-female", metadata={"title": "Otro título"})
    second = _produce(client, view["draft"]["draft_id"])
    again = next(unit for unit in second["sentences"] if unit["text"] == flagged["text"])
    assert again["state"] == "verified" and again["verification"] == "human"


def test_a_paragraph_that_cannot_be_narrated_does_not_crash_the_job(offline, monkeypatch) -> None:
    """A technical failure used to crash the report, and with it the whole job."""
    client = TestClient(main.app)
    view = _upload(client)
    real = main.synthesize

    async def fails_once(text, voice_speed=1.0, voice_id="", **kwargs):
        if "Segundo capítulo, primer" in text:
            raise providers.ProviderError("credit", "Sin saldo.", permanent=True)
        return await real(text, voice_speed, voice_id, **kwargs)

    monkeypatch.setattr(main, "synthesize", fails_once)
    response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]})
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "partially_completed", job["error"]
    assert [unit["state"] for unit in job["sentences"]].count("failed") == 1
    assert "Generación técnica" in (Path(job["source_path"]).parent / "quality-report.html").read_text(encoding="utf-8")


def test_audio_paid_for_comes_back_from_the_jobs_after_the_cache_is_emptied(offline) -> None:
    import shutil

    from tools.reseed_cache import reseed

    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    narrated, transcribed = len(offline["tts"]), len(offline["asr"])
    shutil.rmtree(main.audio_cache.root)

    assert reseed(dry_run=True)["linked"] == len(first["sentences"])
    assert not main.audio_cache.root.exists()  # a dry run changes nothing
    counts = reseed()
    assert counts["linked"] + counts["already_cached"] == len(first["sentences"]) and counts["skipped"] == 0

    # The whole book again: nothing to narrate and nothing to transcribe.
    second = _produce(client, view["draft"]["draft_id"])
    assert len(offline["tts"]) == narrated and len(offline["asr"]) == transcribed
    assert all(unit["state"] == "verified" for unit in second["sentences"])
    # Running it again adds nothing.
    assert reseed()["linked"] == 0


def test_verified_audio_replaces_a_reading_nobody_verified_but_not_the_other_way(offline) -> None:
    from tools.reseed_cache import reseed

    client = TestClient(main.app)
    view = _upload(client)
    job = _produce(client, view["draft"]["draft_id"])
    unit = job["sentences"][0]
    key = main.narration_cache_key(unit["synthesis_text"], job["voice_speed"], job["voice_id"])
    cached = main.audio_cache.path_for(key)
    accepted = (Path(job["source_path"]).parent / unit["audio_path"]).read_bytes()

    # A later retake overwrote the entry, and its verdict no longer applies.
    cached.unlink()
    cached.with_suffix(".qa.json").unlink()
    cached.write_bytes(accepted + b"retake that failed review")
    assert reseed()["replaced"] == 1
    assert cached.read_bytes() == accepted and cached.with_suffix(".qa.json").is_file()

    # An entry with its own verdict stands, even if another job has other audio.
    verdict = cached.with_suffix(".qa.json").read_bytes()
    cached.unlink()
    cached.write_bytes(accepted + b"a reading that was verified")
    assert reseed()["replaced"] == 0
    assert cached.read_bytes().endswith(b"verified") and cached.with_suffix(".qa.json").read_bytes() == verdict


def test_audio_from_another_model_is_not_taken_for_the_current_one(offline, monkeypatch) -> None:
    import shutil

    from tools.reseed_cache import reseed

    client = TestClient(main.app)
    view = _upload(client)
    first = _produce(client, view["draft"]["draft_id"])
    shutil.rmtree(main.audio_cache.root)
    monkeypatch.setattr(main, "tts_model_for", lambda provider: "another-model")
    counts = reseed()
    assert counts["linked"] == 0 and counts["skipped"] == len(first["sentences"])


def test_job_audio_files_share_the_cached_bytes_instead_of_copying_them(offline) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    job = _produce(client, view["draft"]["draft_id"])
    job_dir = Path(job["source_path"]).parent
    unit = job["sentences"][0]
    key = main.narration_cache_key(unit["synthesis_text"], job["voice_speed"], job["voice_id"])
    cached = main.audio_cache.path_for(key)
    segment = job_dir / unit["audio_path"]
    assert segment.read_bytes() == cached.read_bytes()
    if cached.stat().st_nlink > 1:  # hard links where the file system has them
        assert segment.samefile(cached)


# --------------------------------------------------------------------------- #
# The ZIP: one folder of chapter MP3s
# --------------------------------------------------------------------------- #

def test_the_delivery_zip_has_every_chapter_directly_in_audio(offline, tmp_path) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    edits, _long = _edits(view)
    _save(client, view, edits=edits)
    job = _produce(client, view["draft"]["draft_id"])
    with zipfile.ZipFile(job["output_path"]) as archive:
        names = archive.namelist()
        assert archive.testzip() is None
        archive.extractall(tmp_path / "unzipped")
    audio = [name for name in names if name.startswith("DAISY 3/audio/")]
    # Exactly the files left after the edit, in order, side by side: the three
    # initial ONCE files, then chapters 1 and 2 (chapter 3 was deleted).
    assert audio == [f"DAISY 3/audio/chapter-{number:03d}.mp3" for number in range(1, 6)]
    assert not [name for name in names if re.match(r"DAISY 3/audio/[^/]+/", name)]
    assert len(set(audio)) == len(audio)
    package = tmp_path / "unzipped" / "DAISY 3"
    for name in audio:
        stream = audio_integrity.inspect_stream((tmp_path / "unzipped" / name).read_bytes())
        assert stream["frames"] and not stream["errors"]
    # Extracted, it is still a valid DAISY book: links, SMIL clips, NCX, OPF.
    validate_daisy_package(package)
    smil = "".join(path.read_text(encoding="utf-8") for path in sorted((package / "smil").glob("*.smil")))
    assert set(re.findall(r'src="\.\./audio/([^"#]+)"', smil)) == {
        f"chapter-{number:03d}.mp3" for number in range(1, 6)}
    opf = next(package.glob("*.opf")).read_text(encoding="utf-8")
    assert all(f'href="audio/chapter-{number:03d}.mp3"' in opf for number in range(1, 6))


# --------------------------------------------------------------------------- #
# Cache identity
# --------------------------------------------------------------------------- #

def test_voice_settings_are_part_of_the_cache_key_only_when_set() -> None:
    voice = providers.elevenlabs_voice("voice-1", "Gabo", "es-ES", "eleven_v4")
    plain = providers.cache_fingerprint("elevenlabs", voice, "eleven_v4", 1.0, "Hola.")
    unset = providers.cache_fingerprint("elevenlabs", voice, "eleven_v4", 1.0, "Hola.", None)
    stable = providers.cache_fingerprint("elevenlabs", voice, "eleven_v4", 1.0, "Hola.", {"stability": 0.5})
    calmer = providers.cache_fingerprint("elevenlabs", voice, "eleven_v4", 1.0, "Hola.", {"stability": 0.7})
    # Unset settings keep the key every cached clip was stored under.
    assert plain == unset
    assert len({plain, stable, calmer}) == 3
    # The position of a phrase in the book is not part of it.
    assert plain == providers.cache_fingerprint("elevenlabs", voice, "eleven_v4", 1.0, "Hola.")


def test_the_edit_model_keeps_untouched_units_byte_identical() -> None:
    from app.daisy_export import make_paragraphs
    from app.narration_plan import SelectedChapter

    long = " ".join(f"Oración {number} con suficiente texto para que el párrafo sea muy largo." for number in range(40))
    chapter = SelectedChapter("Uno", ["Corto.", long], False)
    chapter.section_id = "section-1"
    parts = make_paragraphs(long)
    edits = book_edits.parse(json.dumps({"slots": {"section-1:1": [parts[0], "Nueva.", *parts[2:]]}}))
    (edited,), _ = book_edits.apply([chapter], edits)
    after = make_paragraphs(edited[1][1])
    assert after == [parts[0], "Nueva.", *parts[2:]]
    assert edited.slots == ["section-1:0", "section-1:1"]


def test_legacy_corrections_still_apply_by_position() -> None:
    from app.narration_plan import SelectedChapter

    chapter = SelectedChapter("Uno", ["A.", "B."], False)
    chapter.section_id = "section-3"
    edits = book_edits.parse("", corrections_json=json.dumps({"0:1": "B corregido."}), chapters=[chapter])
    assert edits.slots == {"section-3:1": "B corregido."}
    with pytest.raises(ValueError):
        book_edits.parse("", corrections_json=json.dumps({"0:5": "x"}), chapters=[chapter])


def test_a_book_that_will_not_fit_is_refused_before_anything_is_paid_for(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    view = _upload(client)
    monkeypatch.setattr(main, "disk_shortfall", lambda _needed: 300 * 1024 * 1024)
    response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]})
    assert response.status_code == 507
    assert "Amplía el disco" in response.json()["detail"]
    assert offline["tts"] == []


def _queued_job(client: TestClient) -> str:
    view = _upload(client)
    response = client.post("/api/books/jobs", data={"draft_id": view["draft"]["draft_id"]})
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_a_book_that_stops_fitting_fails_clearly_and_keeps_its_audio(offline, monkeypatch) -> None:
    job_id = _queued_job(TestClient(main.app))
    monkeypatch.setattr(main, "disk_shortfall", lambda _needed: 300 * 1024 * 1024)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    job_dir = Path(job["source_path"]).parent
    assert job["state"] == "failed"
    assert "faltan unos 301 MB" in job["error"]
    assert all((job_dir / item["audio_path"]).is_file() for item in job["sentences"])
    assert job_id not in main.assembly_progress


def test_a_full_disk_is_explained_and_the_half_built_package_is_removed(offline, monkeypatch) -> None:
    job_id = _queued_job(TestClient(main.app))
    job_dir = Path(job_store.get_job(main.database_path, job_id)["source_path"]).parent
    seen: list[dict] = []

    def full_disk(*_args, **_kwargs):
        seen.append(dict(main.assembly_progress[job_id]))
        (job_dir / "delivery" / "daisy").mkdir(parents=True, exist_ok=True)
        (job_dir / "delivery" / "daisy" / "part.mp3").write_bytes(b"x")
        (job_dir / f".{job_id}-once-delivery.zip.building").write_bytes(b"x")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(main, "build_daisy_package", full_disk)
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "failed"
    assert job["error"].startswith("El disco del servidor se ha llenado")
    assert seen and seen[0]["stage"] == "package" and seen[0]["done"] == seen[0]["total"] > 0
    assert not (job_dir / "delivery" / "daisy").exists()
    assert not (job_dir / f".{job_id}-once-delivery.zip.building").exists()
    assert list((job_dir / "chapter-audio").glob("*.mp3"))
    assert all((job_dir / item["audio_path"]).is_file() for item in job["sentences"])


def test_the_job_reports_how_far_assembly_has_got(offline, monkeypatch) -> None:
    client = TestClient(main.app)
    job_id = _queued_job(client)
    url = f"/api/jobs/{job_id}"
    assert client.get(url).json()["assembly"] is None
    seen: list[dict] = []
    real_fingerprint = main._chapter_fingerprint

    def spying_fingerprint(*args, **kwargs):
        seen.append(dict(main.assembly_progress[job_id]))
        return real_fingerprint(*args, **kwargs)

    monkeypatch.setattr(main, "_chapter_fingerprint", spying_fingerprint)
    asyncio.run(main.run_production_job(job_id))
    assert job_store.get_job(main.database_path, job_id)["state"] == "completed"
    assert seen[0] == {"stage": "chapters", "done": 0, "total": seen[0]["total"]} and seen[0]["total"] > 1
    assert [entry["done"] for entry in seen] == sorted(entry["done"] for entry in seen)
    assert client.get(url).json()["assembly"] is None


def test_the_direct_download_is_refused_too_when_the_book_will_not_fit(offline, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "disk_shortfall", lambda _needed: 300 * 1024 * 1024)
    with pytest.raises(ValueError, match="Amplía el disco"):
        asyncio.run(main._build_direct_package(
            "libro.txt", BOOK.encode("utf-8"), tmp_path / "work", voice_speed=1.0, voice_id="",
            corrections_json="{}", metadata_json="{}", narration_plan_json="", create_navigation_sections_flag=False))
    assert offline["tts"] == []
