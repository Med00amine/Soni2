"""What regenerating a book costs after an edit, stage by stage, measured for free.

    cd backend
    python -m tools.benchmark_regeneration --out <scratch folder> [--chapters 8 --paragraphs 30]

A synthetic DTBook (Spanish prose, chapters, pages) is uploaded and produced
with the ONCE structure through the same endpoints the page uses. Then each
scenario changes the saved draft the way the editor does and produces again:

    unchanged   produce the same draft again
    delete      delete one chapter, produce again   (the slow case reported)
    edit        correct one phrase, produce again
    insert      add a phrase, produce again
    remove      delete a phrase, produce again
    direct      the direct (unverified) download of the edited draft

Nothing paid is called, and nothing is faked where it costs time: narration is
real MPEG audio (shaped noise with pauses, as long as a voice takes to read
the text) served under the ElevenLabs provider name, so the integrity
analysis, mastering, packaging and validation all run for real; recognition
is the mock recognizer reading back the text the audio carries. Any real
network call fails the run.

Each scenario reports its wall time, the time spent in every pipeline stage
(the functions below are wrapped with timers, so the same tool measures the
code before and after a change), the provider calls it made, and an
independent check of the ZIP it produced.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import functools
import inspect
import io
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (audio_integrity, audio_master, completeness, daisy_dtd, daisy_export, daisy_timing,  # noqa: E402
                 main, once_validation, providers, quality_report)
from app import jobs as job_store  # noqa: E402
from app.audio_cache import AudioCache  # noqa: E402
from app.daisy_validation import validate_daisy_package  # noqa: E402

VOICE = "benchvoice0000000000"
WORDS = ("casa camino río montaña noche mañana viento mar ciudad pueblo puerta ventana libro carta "
         "historia memoria silencio palabra jardín invierno verano luz sombra fuego agua tierra cielo "
         "hombre mujer niño anciana amigo hermano madre padre maestro viajero pescador farero "
         "caminaba miraba escuchaba recordaba esperaba volvía encontró dijo pensó sabía quería "
         "lentamente siempre nunca entonces después antes todavía apenas mientras cuando aunque "
         "grande pequeño viejo nuevo oscuro claro frío cálido largo breve tranquilo lejano").split()


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #

def sentence(rng: random.Random) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(8, 18))]
    if rng.random() < 0.5:
        words.insert(rng.randint(3, len(words) - 2), words.pop() + ",")
    text = " ".join(words)
    return text[0].upper() + text[1:] + rng.choice([".", ".", ".", "?", "!"])


def synthetic_dtbook(chapters: int, paragraphs: int, seed: int = 7) -> bytes:
    rng = random.Random(seed)
    page = 1
    body = []
    for number in range(1, chapters + 1):
        parts = [f'<level1 id="cap{number}">', f'<pagenum page="normal" id="page-{page}">{page}</pagenum>',
                 f"<h1>Capítulo {number}. {escape(rng.choice(WORDS).capitalize())} y "
                 f"{escape(rng.choice(WORDS))}</h1>"]
        page += 1
        for index in range(paragraphs):
            if index and index % 6 == 0:
                parts.append(f'<pagenum page="normal" id="page-{page}">{page}</pagenum>')
                page += 1
            parts.append("<p>" + escape(" ".join(sentence(rng) for _ in range(rng.randint(1, 4)))) + "</p>")
        parts.append("</level1>")
        body.append("\n".join(parts))
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">
<dtbook xmlns="http://www.daisy.org/z3986/2005/dtbook/" version="2005-3" xml:lang="es-ES">
<head><meta name="dtb:uid" content="soni2-benchmark"/><meta name="dc:Title" content="Libro de prueba"/>
<meta name="dc:Creator" content="Banco de pruebas"/><meta name="dc:Publisher" content="Soni2"/>
<meta name="dc:Date" content="2026"/><meta name="dc:Language" content="es-ES"/></head>
<book><frontmatter><doctitle>Libro de prueba</doctitle><docauthor>Banco de pruebas</docauthor></frontmatter>
<bodymatter>
""" + "\n".join(body) + "\n</bodymatter></book></dtbook>\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# Narration that costs nothing but sounds (to ffmpeg) like narration
# --------------------------------------------------------------------------- #

class FakeVoice:
    """Real MP3 audio, as long as reading the text takes, carrying the text for the mock recognizer."""

    def __init__(self, pool: Path, latency: float = 0.0) -> None:
        self.pool = pool
        self.pool.mkdir(parents=True, exist_ok=True)
        self.latency = latency
        self.calls = 0
        self.characters = 0
        self._lock = threading.Lock()

    def _path(self, seconds: float) -> Path:
        return self.pool / f"speech-{round(seconds * 10):04d}.mp3"

    def _generate(self, seconds: float) -> None:
        path = self._path(seconds)
        if path.is_file():
            return
        ffmpeg = audio_master.ffmpeg_path()
        if not ffmpeg:
            raise SystemExit("ffmpeg is required: put .venv/tools on PATH")
        # Speech-like: pink noise in phrases of 2.8 s with 0.4 s pauses, a
        # syllable tremolo, a quarter of a second of silence before and after.
        temporary = path.with_suffix(".tmp.mp3")
        subprocess.run([
            ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
            "-i", f"anoisesrc=d={seconds:.2f}:c=pink:r=44100:a=0.25",
            "-af", "volume='if(lt(mod(t,3.2),2.8),1,0)':eval=frame,tremolo=f=4:d=0.5,"
                   "adelay=250:all=1,apad=pad_dur=0.3",
            "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", "-write_xing", "0",
            "-id3v2_version", "0", str(temporary)], check=True, capture_output=True)
        os.replace(temporary, path)

    def prepare(self, longest: float = 60.0) -> None:
        """Every clip length the book can ask for, made before anything is timed."""
        from concurrent.futures import ThreadPoolExecutor

        lengths = [step / 2 for step in range(2, int(longest * 2) + 1)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self._generate, lengths))

    def clip(self, seconds: float) -> bytes:
        with self._lock:
            self._generate(seconds)
            return self._path(seconds).read_bytes()

    async def synthesize(self, text: str, voice, **kwargs) -> bytes:
        with self._lock:
            self.calls += 1
            self.characters += len(text)
        if self.latency:
            await asyncio.sleep(self.latency)
        meta = kwargs.get("meta")
        if meta is not None:
            meta.update({"provider": "elevenlabs", "characters_billed": len(text), "request_id": "benchmark"})
        seconds = max(1.0, round(len(text) / 15 * 2) / 2)
        encoded = text.encode("utf-8")
        return providers.MOCK_MARKER + len(encoded).to_bytes(4, "big") + encoded + self.clip(seconds)


class Recognizer:
    def __init__(self, original) -> None:
        self.original = original
        self.calls = 0

    async def transcribe(self, audio: bytes, **kwargs):
        self.calls += 1
        return await self.original(audio, **kwargs)


def block_network() -> None:
    def refuse(*_args, **_kwargs):
        raise RuntimeError("The benchmark never calls a real service.")
    httpx.HTTPTransport.handle_request = refuse  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = refuse  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# Timers around the pipeline
# --------------------------------------------------------------------------- #

class Timers:
    def __init__(self) -> None:
        self.totals: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def add(self, label: str, seconds: float) -> None:
        with self._lock:
            entry = self.totals.setdefault(label, [0, 0.0])
            entry[0] += 1
            entry[1] += seconds

    def wrap(self, owner, name: str, label: str | None = None) -> None:
        original = getattr(owner, name, None)
        if original is None or getattr(original, "_benchmark_wrapped", False):
            return
        label = label or name
        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def timed(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.add(label, time.perf_counter() - started)
        else:
            @functools.wraps(original)
            def timed(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    self.add(label, time.perf_counter() - started)
        timed._benchmark_wrapped = True  # type: ignore[attr-defined]
        setattr(owner, name, timed)

    def snapshot(self) -> dict[str, list[float]]:
        with self._lock:
            return copy.deepcopy(self.totals)


STAGES = [
    # (owner, function, label). Nested stages are reported as they are measured:
    # mastering includes its own finish_file, the package includes the finishing.
    # Units run several at a time, so per-unit sums exceed the wall time; the
    # phase timer is the wall time of the whole unit pass.
    (main, "_run_limited", "phase: units (wall time)"),
    (main, "_produce_unit", "unit: produce (all work per unit)"),
    (audio_integrity, "assess", "unit: integrity analysis"),
    (main, "_write_job_reports", "reports: render quality report"),
    (main, "compute_release", "reports: release gate"),
    (job_store, "get_job", "db: load whole job"),
    (job_store, "get_events", "db: load events"),
    (job_store, "update_sentence", "db: update unit"),
    (job_store, "add_event", "db: add event"),
    (job_store, "add_attempt", "db: add attempt"),
    (main, "_assemble_job_delivery", "assembly: total"),
    (audio_master, "master_chapter", "assembly: master chapters"),
    (audio_master, "finish_file", "audio: finish_file (edges+CBR, all calls)"),
    (audio_master, "join_book", "assembly: join complete book"),
    (main, "build_daisy_package", "package: build DAISY (incl. chapter work)"),
    (main, "validate_daisy_package", "validate: structural"),
    (daisy_dtd, "validate_package", "validate: DTD"),
    (completeness, "verify", "validate: completeness chain (incl. timing)"),
    (daisy_timing, "verify", "validate: timing (decodes audio)"),
    (once_validation, "verify", "validate: ONCE structure"),
    (main, "add_to_archive", "zip: add files"),
    (main, "_zip_package", "zip: direct package"),
    (main, "_build_direct_package", "direct: total"),
]


def instrument(timers: Timers) -> None:
    # Names main imported from other modules (build_daisy_package,
    # add_to_archive, validate_daisy_package) are wrapped where main calls them.
    for owner, name, label in STAGES:
        timers.wrap(owner, name, label)


# --------------------------------------------------------------------------- #
# Editing the draft the way the page does (frontend/src/bookEdits.ts)
# --------------------------------------------------------------------------- #

INITIAL_KINDS = {"once_title", "once_bibliographic", "once_index"}


def slot_units(sections: list[dict], slot: str) -> list[str]:
    return [unit["text"] for section in sections for unit in section["units"]
            if (unit.get("source") or {}).get("slot") == slot and (unit.get("source") or {}).get("mode") == "units"]


def section_family(sections: list[dict], index: int) -> list[int]:
    family = [index]
    section = sections[index]
    if section["level"] == 1 and section["kind"] not in INITIAL_KINDS:
        following = index + 1
        while following < len(sections) and sections[following]["level"] == 2 \
                and sections[following]["kind"] not in INITIAL_KINDS:
            family.append(following)
            following += 1
    return family


def delete_section(edits: dict, sections: list[dict], index: int) -> dict:
    edits = copy.deepcopy(edits)
    for position in section_family(sections, index):
        section = sections[position]
        if not section.get("deletable"):
            continue
        for unit in section["units"]:
            source = unit.get("source") or {}
            if source.get("slot") and source.get("mode") in {"units", "text"}:
                edits["slots"][source["slot"]] = []
        title = section.get("title_source") or {}
        if title.get("mode") == "text" and title.get("slot"):
            edits["slots"][title["slot"]] = []
        if title.get("mode") == "title" and title.get("section") and title["section"] not in edits["deleted_titles"]:
            edits["deleted_titles"].append(title["section"])
    return edits


def change_phrase(edits: dict, sections: list[dict], unit: dict, how: str, text: str = "") -> dict:
    edits = copy.deepcopy(edits)
    source = unit["source"]
    phrases = slot_units(sections, source["slot"])
    part = source.get("part") or 0
    if how == "edit":
        phrases[part] = text
    elif how == "insert":
        phrases.insert(part + 1, text)
    elif how == "remove":
        phrases.pop(part)
    edits["slots"][source["slot"]] = phrases
    return edits


def body_units(sections: list[dict]) -> list[tuple[int, dict]]:
    return [(index, unit) for index, section in enumerate(sections) if section["kind"] not in INITIAL_KINDS
            for unit in section["units"] if (unit.get("source") or {}).get("mode") == "units" and unit["role"] == "text"]


# --------------------------------------------------------------------------- #
# Checking the ZIP on its own
# --------------------------------------------------------------------------- #

def check_zip(path: Path, scratch: Path, absent_titles: list[str], expected_chapters: int | None) -> dict:
    target = scratch / ("unzipped-" + path.stem)
    shutil.rmtree(target, ignore_errors=True)
    result: dict = {"bytes": path.stat().st_size}
    with zipfile.ZipFile(path) as archive:
        result["crc"] = "passed" if archive.testzip() is None else "failed"
        archive.extractall(target)
    package = target / "DAISY 3"
    try:
        validate_daisy_package(package)
        result["structural"] = "passed"
    except ValueError as exc:
        result["structural"] = f"failed: {exc}"
    result["dtd"] = daisy_dtd.validate_package(package)["status"]
    timing = daisy_timing.verify(package, **main.timing_targets())
    result["timing"] = daisy_timing.summary(timing)["status"]
    chapters = sorted(package.rglob("chapter-*.mp3"))
    result["chapter_files"] = len(chapters)
    if expected_chapters is not None:
        result["chapter_files_expected"] = expected_chapters
    text = "".join(item.read_text(encoding="utf-8") for item in package.iterdir()
                   if item.suffix in {".xml", ".ncx", ".opf"})
    result["deleted_titles_found"] = [title for title in absent_titles if title in text]
    try:
        from app import package_integrity  # added with the regeneration work; absent before it
        result["integrity"] = package_integrity.audit_zip(path)["status"]
    except ImportError:
        result["integrity"] = "not available"
    shutil.rmtree(target, ignore_errors=True)
    return result


# --------------------------------------------------------------------------- #
# Running a scenario
# --------------------------------------------------------------------------- #

class Poller(threading.Thread):
    """The page asks for the job every second while it runs, and for its structure."""

    def __init__(self, client: TestClient, job_id: str) -> None:
        super().__init__(daemon=True)
        self.client, self.job_id = client, job_id
        self.stop = threading.Event()
        self.samples: list[tuple[float, int]] = []

    def run(self) -> None:
        while not self.stop.wait(1.0):
            # What the page asks for (a server before ?view=live ignores it and answers in full).
            for path in (f"/api/jobs/{self.job_id}?view=live", f"/api/jobs/{self.job_id}/structure"):
                started = time.perf_counter()
                response = self.client.get(path)
                self.samples.append((time.perf_counter() - started, len(response.content)))


def produce(client: TestClient, draft_id: str, label: str, out: Path, timers: Timers, voice: FakeVoice,
            recognizer: Recognizer, poll: bool, absent: list[str], chapters_expected: int | None,
            force: bool = False) -> dict:
    before = timers.snapshot()
    calls, characters, recognitions = voice.calls, voice.characters, recognizer.calls
    started = time.perf_counter()
    response = client.post("/api/books/jobs", data={"draft_id": draft_id, "voice_id": VOICE,
                                                    **({"force": "true"} if force else {})})
    if response.status_code != 202:
        raise SystemExit(f"{label}: job refused: HTTP {response.status_code} {response.text[:400]}")
    created = response.json()
    request_seconds = time.perf_counter() - started
    job_id = created["job_id"]
    poller = Poller(client, job_id) if poll else None
    if poller:
        poller.start()
    run_started = time.perf_counter()
    if created.get("state") not in {"completed"}:
        asyncio.run(main.run_production_job(job_id))
    run_seconds = time.perf_counter() - run_started
    if poller:
        poller.stop.set()
        poller.join()
    download_started = time.perf_counter()
    download = client.get(f"/api/jobs/{job_id}/artifacts", params={"diagnostic": "true"})
    download_seconds = time.perf_counter() - download_started
    total = time.perf_counter() - started
    job = job_store.get_job(main.database_path, job_id)
    report = {
        "scenario": label, "job_id": job_id, "state": job["state"], "units": len(job["sentences"]),
        "reused_existing_job": bool(created.get("reused_existing_job")),
        "seconds": {"total": round(total, 2), "create_request": round(request_seconds, 2),
                    "production_and_assembly": round(run_seconds, 2), "download": round(download_seconds, 2)},
        "narration_calls": voice.calls - calls, "narrated_characters": voice.characters - characters,
        "recognition_calls": recognizer.calls - recognitions,
        "stages": stage_delta(before, timers.snapshot()),
        "chapters": chapter_log(Path(job["source_path"]).parent / "job.log"),
    }
    if poller and poller.samples:
        latencies = sorted(sample[0] for sample in poller.samples)
        report["polling"] = {"requests": len(latencies), "median_ms": round(latencies[len(latencies) // 2] * 1000, 1),
                             "max_ms": round(latencies[-1] * 1000, 1),
                             "max_bytes": max(sample[1] for sample in poller.samples)}
    if download.status_code == 200:
        archive = out / f"{label}.zip"
        archive.write_bytes(download.content)
        report["zip"] = check_zip(archive, out, absent, chapters_expected)
    else:
        report["zip"] = {"error": f"HTTP {download.status_code} {download.text[:200]}"}
    report["perf_log"] = perf_lines(Path(job["source_path"]).parent / "job.log")
    return report


def stage_delta(before: dict, after: dict) -> dict:
    result = {}
    for label, (count, seconds) in after.items():
        previous = before.get(label, [0, 0.0])
        if count - previous[0]:
            result[label] = {"calls": int(count - previous[0]), "seconds": round(seconds - previous[1], 3)}
    return result


def chapter_log(path: Path) -> dict:
    counts = {"reused": 0, "shared": 0, "rebuilt": 0}
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if "assemble.chapter:" not in line:
            continue
        if "reused unchanged chapter audio" in line:
            counts["reused"] += 1
        elif "mastered by an earlier job" in line or "reused mastered chapter" in line:
            counts["shared"] += 1
        elif "rebuilt chapter audio" in line:
            counts["rebuilt"] += 1
    return counts


def perf_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line.split("] ", 1)[-1] for line in path.read_text(encoding="utf-8").splitlines() if " perf." in line]


def direct(client: TestClient, draft_id: str, out: Path, timers: Timers, voice: FakeVoice, absent: list[str],
           chapters_expected: int | None) -> dict:
    before = timers.snapshot()
    calls = voice.calls
    started = time.perf_counter()
    response = client.post("/api/books/package-tasks", data={"draft_id": draft_id, "voice_id": VOICE})
    task = response.json()
    if response.status_code != 202:
        raise SystemExit(f"direct: refused: {response.status_code} {response.text[:300]}")
    # The task runs on the TestClient's own event loop: follow it as the page does.
    while True:
        status = client.get(f"/api/books/package-tasks/{task['task_id']}").json()
        if status["state"] != "running":
            break
        time.sleep(0.25)
    seconds = time.perf_counter() - started
    report = {"scenario": "direct", "state": status["state"], "detail": status.get("detail", ""),
              "seconds": {"total": round(seconds, 2)}, "narration_calls": voice.calls - calls,
              "stages": stage_delta(before, timers.snapshot())}
    if status["state"] == "completed":
        download = client.get(f"/api/books/package-tasks/{task['task_id']}/download")
        archive = out / "direct.zip"
        archive.write_bytes(download.content)
        report["zip"] = check_zip(archive, out, absent, chapters_expected)
    return report


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--chapters", type=int, default=8)
    parser.add_argument("--paragraphs", type=int, default=30)
    parser.add_argument("--latency", type=float, default=0.0, help="seconds each fake narration takes")
    parser.add_argument("--scenarios", default="unchanged,regenerate,delete,edit,insert,remove,direct")
    parser.add_argument("--no-poll", action="store_true", help="do not simulate the page asking for progress")
    parser.add_argument("--keep", action="store_true", help="reuse the storage of an earlier run (warm caches)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    storage = out / "storage"
    if not args.keep:
        shutil.rmtree(storage, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    block_network()
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    voice = FakeVoice(out / "speech-pool")
    voice.latency = args.latency
    recognizer = Recognizer(providers.transcribe)
    providers.synthesize = voice.synthesize  # type: ignore[assignment]
    providers.transcribe = recognizer.transcribe  # type: ignore[assignment]
    providers.reported_concurrency["elevenlabs"] = 5
    main.settings = main.settings.model_copy(update={
        "tts_provider": "elevenlabs", "elevenlabs_api_key": "benchmark-offline",
        "elevenlabs_voice_id": f"{VOICE}|es-ES|Voz de prueba", "elevenlabs_extra_voice_ids": "",
        "elevenlabs_model_id": "eleven_v4", "elevenlabs_voice_name": "", "elevenlabs_voice_language": "",
        "elevenlabs_stability": None, "elevenlabs_similarity_boost": None, "elevenlabs_style": None,
        "asr_provider": "mock", "asr_secondary_provider": "none", "deepgram_api_key": "", "fish_audio_api_key": "",
        "require_peninsular_voice": True, "tts_concurrency": 0, "quality_review_enabled": True,
        "once_intro_template": "", "once_bibliographic_template": "",
    })
    main.storage_root = storage
    main.database_path = storage / "soni2-jobs.sqlite3"
    main.audio_cache = AudioCache(storage / "audio-cache")

    async def no_background(job_id: str) -> None:
        return None

    main._start_or_resume_job = no_background  # the worker runs below, in the open
    timers = Timers()
    instrument(timers)
    voice.prepare()
    # As a context manager the client keeps one event loop for its lifetime,
    # so the direct download's background task survives between requests.
    with TestClient(main.app) as client:
        run_scenarios(client, args, out, timers, voice, recognizer)


def run_scenarios(client: TestClient, args, out: Path, timers: Timers, voice: FakeVoice,
                  recognizer: Recognizer) -> None:
    book = synthetic_dtbook(args.chapters, args.paragraphs)
    created = client.post("/api/drafts", files={"file": ("libro-de-prueba.xml", io.BytesIO(book), "application/xml")})
    if created.status_code != 201:
        raise SystemExit(f"draft refused: {created.status_code} {created.text[:300]}")
    view = created.json()
    draft_id = view["draft"]["draft_id"]
    sections = view["outline"]["sections"]
    print(f"book: {len(sections)} files, {sum(len(s['units']) for s in sections)} units", flush=True)

    reports = []
    seed = produce(client, draft_id, "seed", out, timers, voice, recognizer, not args.no_poll, [], None)
    reports.append(seed)
    print_report(seed)
    state = view["draft"]["state"]
    edits = {"slots": {}, "titles": {}, "deleted_titles": []}

    def save(new_edits: dict) -> dict:
        nonlocal view
        current = client.get(f"/api/drafts/{draft_id}").json()
        response = client.put(f"/api/drafts/{draft_id}", json={"state": {**current["draft"]["state"], "edits": new_edits},
                                                               "revision": current["draft"]["revision"]})
        if response.status_code != 200:
            raise SystemExit(f"edit refused: {response.status_code} {response.text[:300]}")
        view = response.json()
        return view

    absent: list[str] = []
    chapters_expected = None
    for scenario in [item.strip() for item in args.scenarios.split(",") if item.strip()]:
        sections = view["outline"]["sections"]
        if scenario == "delete":
            target = next(index for index, section in enumerate(sections)
                          if section["kind"] == "chapter" and section.get("deletable") and index > 4)
            absent = [sections[target]["title"]]
            edits = delete_section(edits, sections, target)
            save(edits)
            chapters_expected = sum(1 for section in view["outline"]["sections"])
        elif scenario in {"edit", "insert", "remove"}:
            units = body_units(sections)
            _index, unit = units[len(units) // 2 + {"edit": 0, "insert": 3, "remove": 7}[scenario]]
            text = {"edit": unit["text"].rstrip(".?!") + ", según contaba el viajero.",
                    "insert": "Y entonces, sin decir nada, el farero cerró la puerta.", "remove": ""}[scenario]
            edits = change_phrase(edits, sections, unit, scenario, text)
            save(edits)
        elif scenario == "direct":
            report = direct(client, draft_id, out, timers, voice, absent, chapters_expected)
            reports.append(report)
            print_report(report)
            continue
        # "unchanged" asks for the ZIP again (Descargar ZIP); "regenerate"
        # builds it again from the same text (Regenerar ZIP, force=true).
        report = produce(client, draft_id, scenario, out, timers, voice, recognizer, not args.no_poll, absent,
                         chapters_expected, force=scenario == "regenerate")
        reports.append(report)
        print_report(report)
    summary = {"book": {"chapters": args.chapters, "paragraphs_per_chapter": args.paragraphs,
                        "units": seed["units"], "fake_narration_latency": args.latency},
               "machine": {"platform": sys.platform, "python": sys.version.split()[0], "cpus": os.cpu_count()},
               "scenarios": reports}
    (out / "benchmark-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", out / "benchmark-report.json")


def print_report(report: dict) -> None:
    seconds = report["seconds"]
    line = f"\n== {report['scenario']}: {seconds['total']} s total"
    if "production_and_assembly" in seconds:
        line += f" (create {seconds['create_request']} s, run {seconds['production_and_assembly']} s, download {seconds['download']} s)"
    print(line, f"| narration calls {report.get('narration_calls')} | recognitions {report.get('recognition_calls', '-')}",
          f"| chapters {report.get('chapters', '-')}")
    for label, value in sorted(report["stages"].items(), key=lambda item: -item[1]["seconds"]):
        print(f"   {value['seconds']:9.2f} s  {value['calls']:6d}x  {label}")
    if report.get("polling"):
        print("   polling:", report["polling"])
    print("   zip:", report.get("zip"))
    for line in report.get("perf_log") or []:
        print("   ", line)


if __name__ == "__main__":
    main_cli()
