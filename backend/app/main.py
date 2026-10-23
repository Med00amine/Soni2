from __future__ import annotations

import base64
import asyncio
import difflib
import functools
import hashlib
import json
import logging
import os
import re
import shutil as filesystem
import tempfile
import shutil
import sqlite3
import uuid
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.background import BackgroundTask

from app.daisy_export import (add_to_archive, build_daisy_package, join_mp3 as daisy_join_mp3,
                              make_paragraphs)
from app.book_structure import extract_book_structure
from app.daisy_validation import validate_daisy_package
from app.sentences import build_synthesis_text, segment_paragraphs, segment_sentences
from app import jobs as job_store
from app import providers, qa_review, quality_report
from app.audio_cache import AudioCache, write_atomic, write_atomic_text
from app.job_logging import JobLogger
from app.narration_plan import build_narration_plan, apply_narration_selection
from app.providers import ProviderError
from app.spanish_normalization import canonical_tokens, compare_transcripts as compare_spanish_transcripts, comparison_tokens
from app.text_language import recognition_language


class Settings(BaseSettings):
    app_env: str = "development"
    # Narration (text-to-speech) and speech review (speech-to-text) are chosen
    # separately: a good transcriber is not necessarily a voice, and either
    # account can run out of credit on its own.
    tts_provider: str = "fishaudio"
    asr_provider: str = "auto"
    tts_voice_id: str = ""
    require_peninsular_voice: bool = True
    fish_audio_api_key: str = ""
    fish_audio_voice_id: str = ""
    fish_audio_extra_voice_ids: str = "fb9d5855f185451983404deb10788ad4,3f065d660212478a9c697fa965b4d098"
    fish_audio_model: str = "drama-3"
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_extra_voice_ids: str = ""
    elevenlabs_model_id: str = providers.ELEVENLABS_DEFAULT_MODEL
    # ElevenLabs only tells the API a library voice's name and accent when the
    # key may read voices, so the operator can state them here. A declared
    # language (es-ES) is what lets the voice pass REQUIRE_PENINSULAR_VOICE.
    elevenlabs_voice_name: str = ""
    elevenlabs_voice_language: str = ""
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_size_mb: int = 200
    preview_characters: int = 5000
    once_intro_template: str = ""
    once_bibliographic_template: str = ""
    once_identification_statement: str = ""
    once_legal_statement: str = ""
    once_intro_filename: str = ""
    once_bibliographic_filename: str = ""
    job_storage_dir: str = "/data"
    quality_review_enabled: bool = True
    # How many narration requests run at once. 0 picks a safe default for the
    # active provider (see narration_concurrency).
    tts_concurrency: int = 0
    # Resolve the repository .env by path, not relative to the working
    # directory: running `uvicorn app.main:app` from backend/ previously found
    # no .env and silently fell back to the defaults. Real environment
    # variables still win, so Docker Compose keeps precedence.
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parents[2] / ".env", ".env"), extra="ignore",
    )


settings = Settings()
storage_root = Path(settings.job_storage_dir)
database_path = storage_root / "soni2-jobs.sqlite3"
audio_cache = AudioCache(storage_root / "audio-cache")
active_jobs: dict[str, asyncio.Task] = {}
retry_delays = (2, 4, 8, 16, 32)
# Automatic retakes after the comparison finds a real error. Each one is a paid
# narration of the whole paragraph, and a second failure rarely clears on a third.
MAX_QUALITY_RETAKES = 2
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soni2")


def active_tts_provider() -> str:
    return settings.tts_provider.strip().casefold() or "fishaudio"


# Requests narrated at the same time, kept below each provider's documented
# concurrency limit (Deepgram Aura REST: 15; Fish Audio's starter tier: 5) so a
# book narrates several paragraphs at once without being refused with HTTP 429.
# Sending them one after another made a 20,000-character book take 18 minutes.
# ElevenLabs starts at its free-plan limit and then uses the limit it reports.
_DEFAULT_TTS_CONCURRENCY = {"deepgram": 8, "fishaudio": 4, "elevenlabs": 2, "mock": 8}

T = TypeVar("T")


def narration_concurrency() -> int:
    if settings.tts_concurrency > 0:
        return settings.tts_concurrency
    provider = active_tts_provider()
    reported = providers.reported_concurrency.get(provider)
    if reported:
        # One slot stays free, so a preview or a review click is not refused
        # while a book is being narrated.
        return max(1, reported - 1)
    return _DEFAULT_TTS_CONCURRENCY.get(provider, 2)


async def _run_limited(
    factories: list[Callable[[], Awaitable[T]]], limit: int | Callable[[], int],
) -> list[T]:
    """Run coroutine factories at most ``limit`` at a time, keeping their order.

    ``limit`` may be a function, read again whenever a slot frees up, so a
    limit the provider reports on its first reply takes effect at once.
    The first failure cancels everything still waiting or in flight, so an
    exhausted balance or a rejected key stops the book at once instead of
    spending on requests that can no longer produce a package.
    """
    current_limit = limit if callable(limit) else (lambda: limit)
    condition = asyncio.Condition()
    running = 0

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        nonlocal running
        async with condition:
            await condition.wait_for(lambda: running < max(1, current_limit()))
            running += 1
        try:
            return await factory()
        finally:
            async with condition:
                running -= 1
                condition.notify_all()

    tasks = [asyncio.ensure_future(run(factory)) for factory in factories]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# How much of each neighbouring passage goes along as context. ElevenLabs only
# needs the words next to the join, and the context is not billed.
CONTEXT_CHARACTERS = 500
NO_CONTEXT: tuple[str, str] = ("", "")


def narration_context(previous: str = "", following: str = "") -> tuple[str, str]:
    """``(previous_text, next_text)`` trimmed to the words around the join."""
    before = previous.strip()[-CONTEXT_CHARACTERS:]
    after = following.strip()[:CONTEXT_CHARACTERS]
    if len(previous.strip()) > CONTEXT_CHARACTERS and " " in before:
        before = before.split(" ", 1)[1]
    if len(following.strip()) > CONTEXT_CHARACTERS and " " in after:
        after = after.rsplit(" ", 1)[0]
    return before, after


def provider_api_key(provider: str) -> str:
    return {"fishaudio": settings.fish_audio_api_key, "deepgram": settings.deepgram_api_key,
            "elevenlabs": settings.elevenlabs_api_key, "mock": "mock"}.get(provider.casefold(), "")


def active_asr_provider() -> str:
    """Resolve the review transcriber, preferring an explicit configuration.

    ``auto`` picks whichever transcriber actually has credentials, so narration
    keeps working when one account is exhausted.
    """
    configured = settings.asr_provider.strip().casefold()
    if configured and configured != "auto":
        return configured
    if active_tts_provider() == "mock":
        return "mock"
    for candidate in ("deepgram", "fishaudio"):
        if provider_api_key(candidate):
            return candidate
    return "none"


def tts_voices(provider: str | None = None) -> tuple[providers.Voice, ...]:
    """The voices the active provider may narrate with.

    ElevenLabs has no fixed catalog: its voices are the ids configured here,
    the first one being ELEVENLABS_VOICE_ID.
    """
    provider = (provider or active_tts_provider()).casefold()
    if provider != "elevenlabs":
        return providers.voices_for(provider)
    primary = settings.elevenlabs_voice_id.strip()
    ids = [primary, *(value.strip() for value in settings.elevenlabs_extra_voice_ids.split(","))]
    voices = []
    for index, voice_id in enumerate(dict.fromkeys(value for value in ids if value)):
        is_primary = voice_id == primary
        name = (settings.elevenlabs_voice_name.strip() if is_primary else "") or f"Voz de ElevenLabs {index + 1}"
        language = settings.elevenlabs_voice_language if is_primary else ""
        voices.append(providers.elevenlabs_voice(voice_id, name, language, settings.elevenlabs_model_id))
    return tuple(voices)


def find_tts_voice(voice_id: str, provider: str | None = None) -> providers.Voice | None:
    provider = provider or active_tts_provider()
    return providers.find_voice(provider, voice_id, tts_voices(provider))


def configured_voice_id() -> str:
    """The server's default voice for the active provider."""
    provider = active_tts_provider()
    explicit = settings.tts_voice_id.strip()
    if provider == "fishaudio" and not explicit:
        explicit = settings.fish_audio_voice_id.strip()
    if provider == "elevenlabs" and not explicit:
        explicit = settings.elevenlabs_voice_id.strip()
    if explicit and find_tts_voice(explicit, provider):
        return explicit
    default = providers.default_voice(provider, tts_voices(provider))
    return default.voice_id if default else explicit


def resolve_narration_voice(voice_id: str = "", *, enforce_accent: bool | None = None) -> providers.Voice:
    """Validate the voice before anything expensive happens."""
    enforce = settings.require_peninsular_voice if enforce_accent is None else enforce_accent
    provider = active_tts_provider()
    return providers.resolve_voice(
        provider, voice_id or configured_voice_id(), require_peninsular=enforce, catalog=tts_voices(provider),
    )


def tts_model_for(provider: str) -> str:
    return {"fishaudio": settings.fish_audio_model,
            "elevenlabs": settings.elevenlabs_model_id}.get(provider, "")
app = FastAPI(title="Soni2", version="0.1.0", description="Servicio accesible para convertir documentos en audio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def recover_production_jobs() -> None:
    job_store.initialize_store(database_path)
    # A job a restart interrupted is stopped, not resumed. The page starts
    # empty, so nobody is watching it any more, and resuming it unattended spent
    # credit narrating a book (with whatever voice it had) no one would download.
    for job_id in job_store.stop_interrupted_jobs(database_path):
        logger.warning("Production job %s was interrupted by a restart and has been stopped", job_id)
    # Direct-download tasks live in memory, so their files cannot outlive a restart.
    shutil.rmtree(_package_tasks_root(), ignore_errors=True)


ALLOWED = {".pdf", ".txt", ".xml", ".docx", ".epub"}


async def _synthesize_fish_audio(text: str, voice_speed: float = 1.0, voice_id: str = "") -> bytes:
    """Synthesize one chunk with Fish Audio using the voice's own language."""
    voice = providers.resolve_voice("fishaudio", voice_id or settings.fish_audio_voice_id)
    return await providers.synthesize(
        text, voice, speed=voice_speed, api_key=settings.fish_audio_api_key, model=settings.fish_audio_model,
    )


class PreviewResult(BaseModel):
    audio_base64: str = ""
    audio_mime_type: str = "audio/mpeg"
    transcript: str
    alignment: list[dict[str, float | str]] | None = None
    audio_error: str | None = None
    diagnostic_id: str | None = None
    chapters: list[dict[str, object]] = []
    title: str = ""
    author: str = ""
    edition: str = ""
    publication_year: str = ""
    publisher: str = ""
    narration_plan: list[dict[str, object]] = []


class LexiconRequest(BaseModel):
    entries: dict[str, str]


class SentenceDecision(BaseModel):
    sentence_id: str
    decision: str
    corrected_text: str = ""


def extract_text(filename: str, content: bytes) -> str:
    """Compatibility wrapper over the canonical book parser."""
    return parse_book(filename, content).as_text()


def parse_book(filename: str, content: bytes, pages: str = ""):
    """Read a book; ``pages`` ("20-114") limits a PDF to that page range."""
    try:
        return extract_book_structure(filename, content, pages=pages or None)
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Book parsing failed for %s: %s", filename, type(exc).__name__)
        raise ValueError("No se ha podido leer este archivo. Comprueba que no este dañado y que el formato sea compatible.") from exc


def _selected_narration_ids(raw: str, plan):
    """Absent selection uses policy defaults; supplied IDs are validated exactly."""
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
        values = payload.get("selected_ids") if isinstance(payload, dict) else None
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise ValueError("La selección de contenido no es válida. Vuelve a revisar el esquema del libro.") from exc
    valid = {section.id for section in plan}
    if not set(values) <= valid:
        raise ValueError("La selección corresponde a otra versión del libro. Vuelve a generar el esquema.")
    if not values:
        raise ValueError("Selecciona al menos una sección para narrar.")
    return set(values)


def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufeff", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    blocks: list[str] = []
    current: list[str] = []
    previous_line = ""
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if not line:
            if current:
                blocks.append(" ".join(current))
                current = []
            previous_line = ""
            continue
        if line == previous_line:
            continue
        current.append(line)
        previous_line = line
    if current:
        blocks.append(" ".join(current))
    return "\n\n".join(blocks).strip()


def apply_paragraph_corrections(
    chapters: list[tuple[str, list[str]]], corrections_json: str
) -> list[tuple[str, list[str]]]:
    try:
        corrections = json.loads(corrections_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Las correcciones del libro no tienen un formato valido.") from exc
    if not isinstance(corrections, dict) or len(corrections) > 5000:
        raise ValueError("La lista de correcciones no es valida.")
    result = [(title, list(paragraphs)) for title, paragraphs in chapters]
    for key, value in corrections.items():
        match = re.fullmatch(r"(\d+):(\d+)", str(key))
        if not match or not isinstance(value, str):
            raise ValueError("Una correccion del libro no es valida.")
        chapter_index, paragraph_index = (int(part) for part in match.groups())
        if chapter_index >= len(result) or paragraph_index >= len(result[chapter_index][1]):
            raise ValueError("Una correccion hace referencia a un parrafo que no existe.")
        replacement = value.strip()
        if not replacement or len(replacement) > 20000:
            raise ValueError("Cada parrafo corregido debe contener entre 1 y 20 000 caracteres.")
        result[chapter_index][1][paragraph_index] = replacement
    return result


def job_logger(job_dir: Path, job_id: str = "", book_id: str = "") -> JobLogger:
    return JobLogger(job_dir, job_id or job_dir.name, book_id=book_id)


def _job_log(job_dir: Path, message: str, *, level: str = "INFO", operation: str = "job", **details: object) -> None:
    """Write one readable `job.log` line plus one structured event record."""
    job_logger(job_dir, str(details.pop("job_id", "")) or job_dir.name).log(
        operation, message, level=level, **details,
    )


def _normalize_for_qa(text: str) -> list[str]:
    return [token.value for token in comparison_tokens(text)]


def _sentence_quality(expected: str, recognized: str) -> tuple[str, list[dict[str, str]], float]:
    """Classify a sentence as verified, ambiguous or a likely error.

    Differences that are only spoken-versus-written Spanish never reach this
    point, and an ambiguous result goes to a person instead of burning another
    paid retake.
    """
    return qa_review.evaluate(expected, recognized)


def _provider_failure_detail(error: Exception) -> tuple[str, bool]:
    """Return a user-facing description and whether retrying can help."""
    if isinstance(error, ProviderError):
        status = f" ({error.status_code})" if error.status_code else ""
        return f"{error.kind}{status}: {error.message}", error.permanent
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return f"HTTP {status}: {str(error)[:240]}", status in {400, 401, 402, 403, 404, 413, 422}
    return f"{type(error).__name__}: {str(error)[:240]}", isinstance(error, (ValueError, TypeError))


def _permanent_provider_failure(error: Exception) -> bool:
    return _provider_failure_detail(error)[1]


async def _synthesize_with_retries(
    text: str, voice_speed: float, voice_id: str, sentence: dict, job_id: str, job_dir: Path,
    *, refresh: bool = False, context: tuple[str, str] = NO_CONTEXT,
) -> tuple[bytes | None, int, str, bool]:
    """Generate one sentence, reusing cached audio and retrying only transient faults.

    Returns ``(audio, attempts, error, reused)``. A cache hit costs no attempt
    and no provider call.
    """
    log = job_logger(job_dir, job_id)
    cache_key = ""
    try:
        cache_key = narration_cache_key(text, voice_speed, voice_id)
    except ProviderError:
        cache_key = ""
    if cache_key and not refresh and audio_cache.contains(cache_key):
        audio = audio_cache.get(cache_key)
        if audio:
            log.info("tts.cache", "reused cached narration audio", sentence_id=sentence["sentence_id"],
                     chapter_index=sentence.get("chapter_index"), cache_key=cache_key[:16], bytes=len(audio))
            return audio, 0, "", True

    attempts = 0
    last_error = ""
    provider = active_tts_provider()
    for retry_index in range(len(retry_delays) + 1):
        attempts += 1
        try:
            audio, reused = await synthesize_cached(text, voice_speed, voice_id, refresh=refresh, context=context)
            log.info("tts.synthesize", "sentence audio generated", sentence_id=sentence["sentence_id"],
                     chapter_index=sentence.get("chapter_index"), provider=provider, voice=voice_id,
                     attempt=attempts, reused=reused, bytes=len(audio))
            return audio, attempts, "", reused
        except Exception as exc:
            last_error, permanent = _provider_failure_detail(exc)
            job_store.add_event(database_path, job_id, "ERROR", "sentence synthesis failed", {
                "sentence_id": sentence["sentence_id"], "attempt": attempts, "permanent": permanent,
                "error": last_error,
            })
            log.error("tts.synthesize", "sentence synthesis failed", sentence_id=sentence["sentence_id"],
                      chapter_index=sentence.get("chapter_index"), provider=provider, attempt=attempts,
                      permanent=permanent, error=last_error)
            if permanent or retry_index >= len(retry_delays):
                break
            delay = retry_delays[retry_index]
            job_store.add_event(database_path, job_id, "WARNING", "retry scheduled", {
                "sentence_id": sentence["sentence_id"], "delay_seconds": delay,
            })
            log.warning("tts.retry", "transient failure, retry scheduled",
                        sentence_id=sentence["sentence_id"], chapter_index=sentence.get("chapter_index"),
                        attempt=attempts, delay_seconds=delay)
            await asyncio.sleep(delay)
    return None, attempts, last_error, False


def audio_review_is_configured() -> bool:
    """True when a transcriber is available for speech review."""
    provider = active_asr_provider()
    return provider != "none" and bool(provider_api_key(provider))


async def _transcription_result(
    audio: bytes, filename: str, content_type: str, *, with_timestamps: bool, language: str = "es",
) -> providers.Transcript:
    provider = active_asr_provider()
    if provider == "none":
        raise ProviderError(
            "not_configured",
            "No hay ningún servicio de transcripción configurado para revisar el audio. "
            "Configura ASR_PROVIDER y su clave de API.",
            permanent=True,
        )
    return await providers.transcribe(
        audio, provider=provider, api_key=provider_api_key(provider), filename=filename,
        content_type=content_type, with_timestamps=with_timestamps, language=language,
    )


async def transcribe_audio(
    audio: bytes, filename: str = "review.mp3", content_type: str = "audio/mpeg", language: str = "es",
) -> tuple[str, str]:
    """Transcribe narration for quality review with the configured ASR provider."""
    result = await _transcription_result(audio, filename, content_type, with_timestamps=False, language=language)
    return result.text, result.engine


async def _transcribe_sentence(audio: bytes, sentence_id: str, text: str = "") -> tuple[str, str]:
    # English passages in a Spanish book (translations, forewords) are heard as
    # English; transcribing them as Spanish sent correct audio to a person.
    return await transcribe_audio(audio, f"{sentence_id}.mp3", "audio/mpeg",
                                  language=recognition_language(text) if text else "es")


def _alignment_from_fish_segments(
    source_text: str, segments: list[dict[str, object]], max_phrase_chars: int = 180
) -> list[dict[str, float | str]]:
    spoken_parts: list[str] = []
    timed_words: list[tuple[int, int, float, float]] = []
    cursor = 0
    for segment in segments:
        word = str(segment.get("text", "")).strip()
        if not word:
            continue
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if spoken_parts:
            spoken_parts.append(" ")
            cursor += 1
        word_start = cursor
        spoken_parts.append(word)
        cursor += len(word)
        timed_words.append((word_start, cursor, start, end))

    spoken_text = "".join(spoken_parts)
    source_tokens, spoken_tokens = canonical_tokens(source_text, spoken_text)
    matcher = difflib.SequenceMatcher(
        a=[token.value for token in source_tokens],
        b=[token.value for token in spoken_tokens],
        autojunk=False,
    )
    matched: list[tuple[int, int, float, float]] = []
    for tag, source_start, source_end, spoken_start, spoken_end in matcher.get_opcodes():
        if tag != "equal":
            continue
        for source_token, spoken_token in zip(source_tokens[source_start:source_end], spoken_tokens[spoken_start:spoken_end]):
            word_times = [
                (start, end) for word_start, word_end, start, end in timed_words
                if word_start < spoken_token.end and word_end > spoken_token.start
            ]
            if word_times:
                matched.append((source_token.start, source_token.end,
                                min(item[0] for item in word_times), max(item[1] for item in word_times)))

    phrases: list[dict[str, float | str]] = []
    group: list[tuple[int, int, float, float]] = []

    def flush() -> None:
        if not group:
            return
        start, end = group[0][0], group[-1][1]
        phrase = source_text[start:end].strip()
        if phrase:
            phrases.append({"text": phrase, "start": min(item[2] for item in group),
                            "end": max(item[3] for item in group), "text_start": start, "text_end": end})
        group.clear()

    for item in matched:
        if group:
            gap = source_text[group[-1][1]:item[0]]
            if len(source_text[group[0][0]:item[1]]) > max_phrase_chars or re.search(r"[.!?;:\n]", gap):
                flush()
        group.append(item)
    flush()
    return phrases


async def _analyze_audio(audio: bytes, expected_text: str) -> dict[str, object]:
    if active_tts_provider() == "mock":
        # Mock audio is deliberately silent, so acoustic heuristics would only
        # report that fact. Say so rather than invent a quality verdict.
        return {"status": "skipped", "engine": "ffmpeg",
                "reason": "El proveedor simulado genera silencio; no se analiza la calidad acústica."}
    ffmpeg = filesystem.which("ffmpeg")
    if not ffmpeg:
        return {"status": "unavailable", "engine": "ffmpeg", "reason": "ffmpeg is not installed"}
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-nostats", "-v", "error", "-i", "pipe:0",
        "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        pcm, error = await asyncio.wait_for(process.communicate(audio), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"status": "needs_review", "engine": "ffmpeg", "reason": "audio decode timed out"}
    if process.returncode != 0 or len(pcm) < 2:
        return {"status": "needs_review", "engine": "ffmpeg", "reason": "MP3 decode failed", "detail": error.decode("utf-8", "replace")[:200]}
    import array
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    total = len(samples)
    duration = total / 8000
    reasons: list[str] = []
    window = 800
    peak = max((abs(int(value)) for value in samples), default=0)
    clipped = sum(abs(int(value)) >= 32700 for value in samples)
    silence_windows = [
        (start, min(start + window, total))
        for start in range(0, total, window)
        if not any(abs(int(value)) > 120 for value in samples[start:min(start + window, total)])
    ]
    longest_silence = 0
    current_silence = 0
    previous_silence_end = None
    for start, end in silence_windows:
        if previous_silence_end is not None and start == previous_silence_end:
            current_silence += end - start
        else:
            longest_silence = max(longest_silence, current_silence)
            current_silence = end - start
        previous_silence_end = end
    longest_silence = max(longest_silence, current_silence) / 8000
    clipped_ratio = clipped / max(1, total)
    words = max(1, len(_normalize_for_qa(expected_text)))
    if duration < max(0.2, words * 0.07):
        reasons.append("suspiciously_short_duration")
    if duration > words * 3.0 + 2:
        reasons.append("suspiciously_long_duration")
    if longest_silence > 2.0:
        reasons.append("abnormal_silence")
    if clipped_ratio > 0.005:
        reasons.append("possible_clipping")
    return {"status": "needs_review" if reasons else "passed", "engine": "ffmpeg",
            "duration_seconds": round(duration, 3), "peak": peak,
            "clipped_sample_ratio": round(clipped_ratio, 6), "longest_silence_seconds": round(longest_silence, 3),
            "reasons": reasons}


def _narration_context() -> dict[str, str]:
    """Provider and voice details recorded with every report."""
    provider = active_tts_provider()
    voice = find_tts_voice(configured_voice_id(), provider)
    return {"provider": provider, "asr_provider": active_asr_provider(),
            "voice_name": voice.name if voice else "", "voice_language": voice.language if voice else ""}


def _quality_report(
    job: dict, events: list[dict], *, validation: dict | None = None,
) -> dict:
    context = _narration_context()
    job_voice = find_tts_voice(job.get("voice_id", ""), context["provider"])
    return quality_report.build_report(
        job, events,
        provider=context["provider"],
        asr_provider=context["asr_provider"],
        voice_name=job_voice.name if job_voice else context["voice_name"],
        voice_language=job_voice.language if job_voice else context["voice_language"],
        validation=validation,
        cache_stats={**audio_cache.stats.as_dict(), **audio_cache.usage()},
    )


def _html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _write_job_reports(
    job_id: str, *, final_state: str | None = None, validation: dict | None = None,
    audio_hrefs: dict[str, str] | None = None,
) -> dict | None:
    """Write `quality-report.json` and `quality-report.html` atomically."""
    job = job_store.get_job(database_path, job_id)
    if not job:
        return None
    job_dir = Path(job["source_path"]).parent
    if final_state:
        job["state"] = final_state
    if validation is None:
        validation = _read_validation_state(job_dir)
    report = _quality_report(job, job_store.get_events(database_path, job_id), validation=validation)
    write_atomic_text(job_dir / "quality-report.json", quality_report.render_json(report))
    log_text = (job_dir / "job.log").read_text(encoding="utf-8") if (job_dir / "job.log").is_file() else ""
    write_atomic_text(
        job_dir / "quality-report.html",
        quality_report.render_html(report, audio_hrefs=audio_hrefs, log_excerpt=log_text[-20000:]),
    )
    return report


def _validation_state_path(job_dir: Path) -> Path:
    return job_dir / "daisy-validation.json"


def _read_validation_state(job_dir: Path) -> dict:
    path = _validation_state_path(job_dir)
    if not path.is_file():
        return {"status": "not_run",
                "note": "El paquete DAISY todavía no se ha generado para este trabajo."}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "unknown", "note": "No se pudo leer el resultado de validación."}


def _record_validation(job_dir: Path, state: dict) -> dict:
    write_atomic_text(_validation_state_path(job_dir), json.dumps(state, ensure_ascii=False, indent=2))
    return state


# Units a person asked to have narrated again right now. The job worker leaves
# them alone, so a review click and a running job never narrate one paragraph twice.
units_in_progress: set[tuple[str, str]] = set()


@dataclass
class UnitResult:
    state: str
    error: str
    # False when no new audio came back (a failed generation), so a review click
    # can put the paragraph back as it was instead of losing its audio.
    produced: bool


def _unit_context(job: dict, position: int) -> tuple[str, str]:
    """The passages either side of the unit at ``position``, as narration context."""
    units = job["sentences"]
    previous = units[position - 1]["synthesis_text"] if position > 0 else ""
    following = units[position + 1]["synthesis_text"] if position + 1 < len(units) else ""
    return narration_context(previous, following)


async def _produce_unit(
    job: dict, item: dict, *, job_dir: Path, log: JobLogger, context: tuple[str, str] = NO_CONTEXT,
    interactive: bool = False, fresh_take: bool = False,
) -> UnitResult:
    """Narrate one unit, check it against its text and store the outcome.

    The job worker retakes a real error automatically. A unit a person asked for
    (``interactive``) gets exactly one new reading, so they hear it straight away
    and decide; ``fresh_take`` skips the cache, for "narrate it again".
    Differences the comparison judges minor are accepted and recorded instead of
    queued for a person: that queue is for what a listener would notice.
    """
    job_id = job["job_id"]
    sentence_id = item["sentence_id"]
    chapter_index = item["chapter_index"]
    job_store.update_sentence(database_path, job_id, sentence_id, state="generating")
    job_store.add_event(database_path, job_id, "INFO", "sentence started", {"sentence_id": sentence_id})
    chosen_audio: bytes | None = None
    recognized_text = ""
    issues: list[dict[str, str]] = []
    confidence: float | None = None
    acoustic: dict[str, object] = {"status": "not_performed"}
    quality_retakes = int(item["quality_retakes"])
    tech_attempts = int(item["technical_attempts"])
    final_state = "failed"
    last_error = ""
    reused_audio = False
    previous_transcript: str | None = None
    # A person's request is one reading; the worker may retake up to the limit.
    # At least one reading always happens, even for a unit already past the limit.
    first_generation = quality_retakes
    last_generation = first_generation if interactive else max(first_generation, MAX_QUALITY_RETAKES)
    for generation_index in range(first_generation, last_generation + 1):
        # The first pass may reuse cached audio. A quality retake must not:
        # handing back the reading that already failed review would spend a
        # transcription on a guaranteed repeat. (Compared with the counter the
        # loop updates, every "retake" used to get the cached failed reading back.)
        audio, attempts, error, reused = await _synthesize_with_retries(
            item["synthesis_text"], job["voice_speed"], job["voice_id"], item, job_id, job_dir,
            refresh=fresh_take or generation_index > first_generation, context=context,
        )
        tech_attempts += attempts
        reused_audio = reused
        if audio is None:
            last_error = error
            break
        if not providers.is_mp3(audio):
            last_error = "El proveedor de narración devolvió datos sin una cabecera MP3 reconocible."
            job_store.add_event(database_path, job_id, "ERROR", "invalid MP3 artifact", {"sentence_id": sentence_id})
            log.error("tts.validate", "provider returned a payload without an MP3 header",
                      sentence_id=sentence_id, chapter_index=chapter_index)
            break
        if not settings.quality_review_enabled:
            chosen_audio, final_state = audio, "needs_human_review"
            last_error = "La revisión automática de calidad está desactivada."
            log.warning("qa.skip", "automatic quality review is disabled; sentence needs a person",
                        sentence_id=sentence_id, chapter_index=chapter_index)
            break
        try:
            recognized_text, recognizer = await _transcribe_sentence(audio, sentence_id, item["text"])
        except Exception as exc:
            detail, _permanent = _provider_failure_detail(exc)
            if chosen_audio is None:
                chosen_audio = audio
            final_state = "needs_human_review"
            last_error = f"Revisión automática no disponible: {detail}"
            job_store.add_event(database_path, job_id, "WARNING", "sentence sent to human review because transcription failed", {
                "sentence_id": sentence_id, "error": last_error,
            })
            log.warning("qa.transcribe", "transcription unavailable; routed to human review",
                        sentence_id=sentence_id, chapter_index=chapter_index,
                        asr_provider=active_asr_provider(), error=detail)
            break
        quality, current_issues, current_confidence = _sentence_quality(item["text"], recognized_text)
        acoustic = await _analyze_audio(audio, item["text"])
        if acoustic["status"] not in {"passed", "skipped"}:
            current_issues.append(qa_review.audio_issue(
                item["text"], recognized_text, json.dumps(acoustic, ensure_ascii=False),
            ))
            # An acoustic defect is a real generation problem, so it is
            # worth a retake rather than a reviewer's time.
            quality = "likely_error"
        job_store.add_event(database_path, job_id, "INFO", "quality transcription completed", {
            "sentence_id": sentence_id, "recognizer": recognizer, "state": quality,
            "confidence": current_confidence, "issue_count": len(current_issues),
        })
        log.info("qa.compare", "quality comparison completed", sentence_id=sentence_id,
                 chapter_index=chapter_index, recognizer=recognizer, verdict=quality,
                 confidence=current_confidence, issues=len(current_issues),
                 acoustic=acoustic.get("status"))
        chosen_audio, issues, confidence = audio, current_issues, current_confidence
        if quality == "verified":
            final_state, last_error = "verified", ""
            break
        accepted = qa_review.auto_accept_reason(quality, current_issues, current_confidence, item["text"])
        if accepted:
            # Kept with its differences, so the report shows what was let through.
            final_state = "verified"
            last_error = f"{qa_review.AUTO_ACCEPT_PREFIX}: {accepted}."
            log.info("qa.auto_accept", "minor difference accepted without a person", sentence_id=sentence_id,
                     chapter_index=chapter_index, confidence=current_confidence, issues=len(current_issues))
            break
        if interactive:
            final_state = "needs_human_review"
            last_error = ("La nueva locución todavía no coincide del todo con el texto. "
                          "Escúchala y decide si es correcta.")
            break
        if recognized_text == previous_transcript:
            # A fresh reading produced exactly the same transcript, so
            # further retakes would cost money and change nothing.
            final_state = "needs_human_review"
            last_error = ("La nueva locución se transcribió igual que la anterior, "
                          "así que hace falta una decisión humana.")
            log.warning("qa.repeat", "retake produced an identical transcript; stopping retakes",
                        sentence_id=sentence_id, chapter_index=chapter_index,
                        quality_retake=quality_retakes)
            break
        previous_transcript = recognized_text
        if generation_index < last_generation:
            quality_retakes = generation_index + 1
            job_store.add_event(database_path, job_id, "WARNING", "quality retake scheduled", {
                "sentence_id": sentence_id, "quality_retake": quality_retakes,
            })
            log.warning("qa.retake", "quality retake scheduled", sentence_id=sentence_id,
                        chapter_index=chapter_index, quality_retake=quality_retakes,
                        confidence=current_confidence)
        else:
            final_state = "needs_human_review"
            last_error = (f"El párrafo no superó la comparación tras {MAX_QUALITY_RETAKES} "
                          "retomas de calidad.")
    audio_path = None
    if chosen_audio is not None:
        relative = Path("audio") / f"chapter-{chapter_index + 1:03d}" / f"segment-{item['sequence'] + 1:04d}.mp3"
        destination = job_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if final_state == "verified":
            if destination.exists():
                originals = destination.parent / "originals"
                originals.mkdir(parents=True, exist_ok=True)
                prior = originals / f"{sentence_id}-{job_store.utc_now().replace(':', '-')}.mp3"
                shutil.copy2(destination, prior)
            # Atomic replace: an interrupted job never leaves a half
            # written segment where good audio used to be.
            write_atomic(destination, chosen_audio)
            audio_path = relative.as_posix()
        else:
            version_dir = destination.parent / "retakes"
            version_dir.mkdir(parents=True, exist_ok=True)
            candidate = version_dir / f"{sentence_id}-candidate-{tech_attempts}-{quality_retakes}.mp3"
            write_atomic(candidate, chosen_audio)
            audio_path = candidate.relative_to(job_dir).as_posix()
    job_store.update_sentence(
        database_path, job_id, sentence_id, state=final_state, technical_attempts=tech_attempts,
        quality_retakes=quality_retakes, audio_path=audio_path, recognized_text=recognized_text,
        issues_json=json.dumps(issues, ensure_ascii=False),
        acoustic_json=json.dumps(acoustic, ensure_ascii=False),
        confidence=confidence, last_error=last_error, reused_audio=int(reused_audio),
    )
    job_store.add_event(database_path, job_id, "INFO", "sentence finished", {
        "sentence_id": sentence_id, "state": final_state, "technical_attempts": tech_attempts,
        "quality_retakes": quality_retakes,
    })
    log.info("sentence.finish", "sentence finished", sentence_id=sentence_id,
             chapter_index=chapter_index, state=final_state, technical_attempts=tech_attempts,
             quality_retakes=quality_retakes, reused_audio=reused_audio, audio_path=audio_path,
             error=last_error or None)
    return UnitResult(final_state, last_error, chosen_audio is not None)


async def run_production_job(job_id: str) -> None:
    job = job_store.get_job(database_path, job_id)
    if not job:
        active_jobs.pop(job_id, None)
        return
    job_dir = Path(job["source_path"]).parent
    log = job_logger(job_dir, job_id, book_id=job["source_hash"])
    log.info("job.start", "production started", source_name=job["source_name"],
             total_sentences=job["total_sentences"], provider=active_tts_provider(),
             asr_provider=active_asr_provider(), voice=job["voice_id"],
             quality_review=settings.quality_review_enabled)
    job_store.update_job(database_path, job_id, state="running", started_at=job.get("started_at") or job_store.utc_now())
    try:
        total = len(job["sentences"])
        cancelled = False

        async def produce(position: int, original: dict) -> None:
            nonlocal cancelled
            if cancelled:
                return
            current = job_store.get_job(database_path, job_id)
            if not current or current["cancel_requested"]:
                cancelled = True
                job_store.update_job(database_path, job_id, state="cancelled", completed_at=job_store.utc_now())
                job_store.add_event(database_path, job_id, "WARNING", "job cancelled")
                log.warning("job.cancel", "job cancelled by request")
                return
            item = next(row for row in current["sentences"] if row["sentence_id"] == original["sentence_id"])
            if item["state"] == "verified" and item["audio_path"] and (job_dir / item["audio_path"]).is_file():
                return
            if item["state"] in {"needs_human_review", "unresolved"}:
                return
            if (job_id, item["sentence_id"]) in units_in_progress:
                # A person is having this one narrated right now.
                return
            result = await _produce_unit(job, item, job_dir=job_dir, log=log, context=_unit_context(job, position))
            # Refresh the reports periodically for progress visibility. Doing it
            # after every sentence re-renders the whole book each time, which is
            # quadratic on a long one; the final state is always written below.
            if result.state != "verified" or position % 20 == 19 or position == total - 1:
                _write_job_reports(job_id)

        # Several units are narrated and checked at once, within the provider's
        # concurrency limit. Each result is stored on its own row, so the order
        # in which they finish does not matter.
        await _run_limited(
            [functools.partial(produce, position, original) for position, original in enumerate(job["sentences"])],
            narration_concurrency,
        )

        final = job_store.get_job(database_path, job_id)
        if not final or final["state"] == "cancelled":
            return
        states = [item["state"] for item in final["sentences"]]
        if any(state in {"pending", "generating", "failed"} for state in states):
            job_store.update_job(database_path, job_id, state="partially_completed", completed_at=job_store.utc_now())
            log.warning("job.partial", "job partially completed with failed or pending sentences",
                        failed=sum(state == "failed" for state in states),
                        pending=sum(state in {"pending", "generating"} for state in states))
            _write_job_reports(job_id, final_state="partially_completed")
            return
        if any(state != "verified" for state in states):
            job_store.update_job(database_path, job_id, state="partially_completed", completed_at=job_store.utc_now())
            log.warning("job.review", "job awaits human review before delivery",
                        awaiting=sum(state != "verified" for state in states))
            _write_job_reports(job_id, final_state="partially_completed")
            return
        await _assemble_job_delivery(job_id)
    except Exception as exc:
        logger.exception("Production job %s failed", job_id)
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        job_store.update_job(database_path, job_id, state="failed", error=detail, completed_at=job_store.utc_now())
        job_store.add_event(database_path, job_id, "ERROR", "job failed", {"error": detail})
        log.error("job.fail", "job failed", error=detail)
        _write_job_reports(job_id, final_state="failed")
    finally:
        active_jobs.pop(job_id, None)


def _chapter_fingerprint(chapter_title: str, items: list[dict]) -> str:
    """Identity of a chapter's final audio: its title and its ordered segments.

    Correcting one sentence changes only that chapter's fingerprint, so every
    other chapter's joined MP3 can be reused instead of re-encoded.
    """
    parts = [chapter_title]
    for item in items:
        parts.append(f"{item['sentence_id']}:{item['audio_path'] or ''}:{item['updated_at']}:{item['text']}")
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()


def _read_chapter_state(delivery_dir: Path) -> dict[str, str]:
    path = delivery_dir / "chapter-state.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _tts_log_text(job: dict) -> str:
    """Readable narration history: one line per sentence, no credentials."""
    lines = [
        f"Narración del trabajo {job['job_id']}",
        f"Servicio de voz: {active_tts_provider()} · Revisión: {active_asr_provider()} · Voz: {job['voice_id']}",
        "",
    ]
    states = {"verified": "correcta", "needs_human_review": "revisión humana", "unresolved": "sin resolver",
              "failed": "fallo técnico", "pending": "pendiente", "generating": "en curso"}
    for item in job["sentences"]:
        lines.append(
            f"capítulo {item['chapter_index'] + 1:>3} · frase {item['sequence'] + 1:>4} · "
            f"{states.get(item['state'], item['state']):<16} "
            f"intentos={item['technical_attempts']} retomas={item['quality_retakes']} "
            f"{'reutilizado de la caché' if item.get('reused_audio') else 'generado'} · {item['sentence_id']}"
        )
    return "\n".join(lines) + "\n"


def _review_log_text(events: list[dict]) -> str:
    """Readable record of every human review decision."""
    if not events:
        return "No se registraron decisiones de revisión humana en este trabajo.\n"
    labels = {"accept": "aceptada", "retry": "volver a narrar", "correct": "texto corregido"}
    lines = []
    for event in events:
        details = event.get("details") or {}
        line = (f"{event.get('timestamp', '')} · {details.get('sentence_id', '')} · "
                f"{labels.get(details.get('decision'), details.get('decision', ''))}")
        if details.get("corrected_text"):
            line += f" · «{details['corrected_text']}»"
        lines.append(line)
    return "\n".join(lines) + "\n"


async def _assemble_job_delivery(job_id: str) -> None:
    job = job_store.get_job(database_path, job_id)
    if not job:
        return
    job_dir = Path(job["source_path"]).parent
    log = job_logger(job_dir, job_id, book_id=job["source_hash"])
    chapter_sentences: dict[int, list[dict]] = {}
    for sentence in job["sentences"]:
        chapter_sentences.setdefault(sentence["chapter_index"], []).append(sentence)

    delivery_dir = job_dir / "delivery"
    chapters_dir = job_dir / "chapter-audio"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    previous_state = _read_chapter_state(delivery_dir)

    chapters: list[tuple[str, list[str]]] = []
    segments: list[bytes] = []
    chapter_audio: list[bytes] = []
    chapter_state: dict[str, str] = {}
    sentence_segment_index: dict[str, int] = {}
    reused_chapters = 0
    rebuilt_chapters = 0

    for chapter_index, chapter in enumerate(job["chapters"]):
        chapter_items = chapter_sentences.get(chapter_index, [])
        if not chapter_items:
            continue
        missing = [item["sentence_id"] for item in chapter_items if not item["audio_path"]]
        if missing:
            raise RuntimeError(f"Chapter {chapter_index + 1} has sentences without audio: {missing[:3]}")
        chapters.append((chapter["title"], [item["text"] for item in chapter_items]))
        chapter_segments: list[bytes] = []
        for item in chapter_items:
            sentence_segment_index[item["sentence_id"]] = len(segments) + len(chapter_segments) + 1
            chapter_segments.append((job_dir / item["audio_path"]).read_bytes())
        segments.extend(chapter_segments)

        fingerprint = _chapter_fingerprint(chapter["title"], chapter_items)
        chapter_state[str(chapter_index)] = fingerprint
        joined_path = chapters_dir / f"chapter-{chapter_index + 1:03d}.mp3"
        if previous_state.get(str(chapter_index)) == fingerprint and joined_path.is_file() and joined_path.stat().st_size:
            # Nothing in this chapter changed, so keep the audio already built.
            chapter_audio.append(joined_path.read_bytes())
            reused_chapters += 1
            log.info("assemble.chapter", "reused unchanged chapter audio", chapter_index=chapter_index,
                     fingerprint=fingerprint[:16], sentences=len(chapter_items))
        else:
            # ffmpeg runs in a worker thread so the server keeps answering
            # status requests while a long chapter is re-encoded.
            joined = await asyncio.to_thread(daisy_join_mp3, chapter_segments)
            write_atomic(joined_path, joined)
            chapter_audio.append(joined)
            rebuilt_chapters += 1
            log.info("assemble.chapter", "rebuilt chapter audio", chapter_index=chapter_index,
                     fingerprint=fingerprint[:16], sentences=len(chapter_items), bytes=len(joined))

    if not segments:
        raise RuntimeError("No verified sentence audio is available to assemble.")

    metadata = job["metadata"]
    values = {**metadata, "once_identification_statement": settings.once_identification_statement,
              "legal_statement": settings.once_legal_statement}
    front_matter: list[tuple[str, str, bytes, str]] = []
    if settings.once_intro_template:
        text = settings.once_intro_template.format(**values)
        audio, _reused = await synthesize_cached(text, job["voice_speed"], job["voice_id"])
        front_matter.append(("Introducción", text, audio, settings.once_intro_filename))
    if settings.once_bibliographic_template:
        text = settings.once_bibliographic_template.format(**values)
        audio, _reused = await synthesize_cached(text, job["voice_speed"], job["voice_id"])
        front_matter.append(("Datos bibliográficos", text, audio, settings.once_bibliographic_filename))

    if delivery_dir.exists():
        shutil.rmtree(delivery_dir)
    delivery_dir.mkdir(parents=True, exist_ok=True)
    # ffmpeg joins the audio into clean continuous MP3 streams, but a container
    # without it must still deliver rather than crash. Which path ran is recorded.
    use_ffmpeg = bool(filesystem.which("ffmpeg"))
    if not use_ffmpeg:
        log.warning("assemble.ffmpeg", "ffmpeg is unavailable; chapter and book audio are byte-concatenated "
                                       "instead of joined into one clean stream")
    log.info("assemble.start", "building DAISY package", chapters=len(chapters), segments=len(segments),
             reused_chapters=reused_chapters, rebuilt_chapters=rebuilt_chapters,
             front_matter=len(front_matter), joined_with_ffmpeg=use_ffmpeg)
    clips: list[tuple[str, float, float]] = []
    package = await asyncio.to_thread(functools.partial(
        build_daisy_package,
        metadata["title"] or Path(job["source_name"]).stem, [text for _, body in chapters for text in body],
        segments, delivery_dir / "daisy", chapters=chapters, author=metadata["author"],
        edition=metadata["edition"], publication_year=metadata["publication_year"],
        publisher=metadata["publisher"], front_matter=front_matter,
        chapter_audio=chapter_audio, use_ffmpeg=use_ffmpeg, clips_out=clips,
    ))
    try:
        validate_daisy_package(package)
    except ValueError as exc:
        validation = _record_validation(job_dir, {
            "status": "failed", "method": "soni2_structural_validation", "detail": str(exc)[:400],
            "official_daisy_pipeline": "not_run",
        })
        log.error("daisy.validate", "structural validation failed", detail=str(exc)[:300])
        _write_job_reports(job_id, final_state="failed", validation=validation)
        raise
    validation = _record_validation(job_dir, {
        "status": "passed",
        "method": "soni2_structural_validation",
        "checks": "archivos requeridos, referencias smilref, destinos NCX, manifiesto y spine OPF, "
                  "audio final por capítulo y audio no vacío",
        "audio_joined_with_ffmpeg": use_ffmpeg,
        "official_daisy_pipeline": "not_run",
        "note": "Validación estructural propia. No sustituye al validador oficial DAISY Pipeline "
                "ni a una comprobación en un reproductor DAISY real.",
    })
    log.info("daisy.validate", "structural validation passed", method="soni2_structural_validation",
             official_daisy_pipeline="not_run", joined_with_ffmpeg=use_ffmpeg)

    write_atomic_text(delivery_dir / "tts.log", _tts_log_text(job))
    review_events = [event for event in job_store.get_events(database_path, job_id)
                     if event.get("message") == "human review decision"]
    write_atomic_text(delivery_dir / "review-decisions.log", _review_log_text(review_events))
    write_atomic_text(delivery_dir / "chapter-state.json",
                      json.dumps(chapter_state, ensure_ascii=False, indent=2))

    archive_path = job_dir / f"{job_id}-once-delivery.zip"
    job_store.add_event(database_path, job_id, "INFO", "package validated and delivery assembled", {
        "validation": "soni2_structural_validation_passed", "reused_chapters": reused_chapters,
        "rebuilt_chapters": rebuilt_chapters,
    })
    log.info("job.complete", "delivery assembled", output=archive_path.name,
             validation="soni2_structural_validation_passed", reused_chapters=reused_chapters,
             rebuilt_chapters=rebuilt_chapters, cache=audio_cache.stats.as_dict())
    # The report inside the ZIP points at the package's own audio so it plays
    # offline straight from the archive.
    # Each sentence plays its own stretch of the chapter MP3 (a media fragment).
    audio_hrefs = {}
    for sentence_id, index in sentence_segment_index.items():
        chapter_path, begin, end = clips[index - 1]
        audio_hrefs[sentence_id] = f"DAISY 3/audio/{chapter_path}#t={begin:.3f},{end:.3f}"
    _write_job_reports(job_id, final_state="completed", validation=validation, audio_hrefs=audio_hrefs)
    for name in ("job.log", "quality-report.json", "quality-report.html",
                 "reviewed-source.json", "daisy-validation.json"):
        source = job_dir / name
        if source.is_file():
            shutil.copy2(source, delivery_dir / name)
    staging = job_dir / f".{archive_path.name}.building"
    staging.unlink(missing_ok=True)
    # Audio is stored rather than deflated: MP3 does not compress, and
    # deflating a book-sized delivery cost minutes for about 2% of size.
    with zipfile.ZipFile(staging, "w") as archive:
        for path in package.rglob("*"):
            if path.is_file():
                add_to_archive(archive, path, "DAISY 3/" + path.relative_to(package).as_posix())
        # job.log is the readable history; its JSON Lines twin stays in the
        # job folder for tooling and is not delivered.
        for name in ("quality-report.html", "quality-report.json", "job.log", "tts.log",
                     "review-decisions.log", "reviewed-source.json", "daisy-validation.json"):
            path = delivery_dir / name
            if path.is_file():
                add_to_archive(archive, path, name)
        # The DAISY package already carries one copy of every sentence segment
        # and chapter MP3, so the job's working audio is not added again.
        add_to_archive(archive, Path(job["source_path"]), "source/" + Path(job["source_path"]).name)
    os.replace(staging, archive_path)
    # The staged DAISY tree existed only to be archived. Removing it leaves one
    # copy of the package (inside the ZIP) plus the working set that incremental
    # rebuilds need: per-sentence audio in audio/ and chapter audio in
    # chapter-audio/. The reports and logs beside it stay for direct serving.
    shutil.rmtree(package, ignore_errors=True)
    log.info("assemble.cleanup", "removed the staged package tree now that the archive holds it",
             archive=archive_path.name, archive_bytes=archive_path.stat().st_size)
    job_store.update_job(database_path, job_id, state="completed", completed_at=job_store.utc_now(), output_path=str(archive_path))


async def _start_or_resume_job(job_id: str) -> None:
    task = active_jobs.get(job_id)
    if task and not task.done():
        return
    active_jobs[job_id] = asyncio.create_task(run_production_job(job_id))


@app.get("/api/pronunciation-lexicon")
async def read_pronunciation_lexicon() -> dict[str, dict[str, str]]:
    job_store.initialize_store(database_path)
    return {"entries": job_store.get_lexicon(database_path)}


@app.put("/api/pronunciation-lexicon")
async def write_pronunciation_lexicon(request: LexiconRequest) -> dict[str, dict[str, str]]:
    job_store.initialize_store(database_path)
    try:
        job_store.replace_lexicon(database_path, request.entries)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"entries": job_store.get_lexicon(database_path)}


@app.post("/api/books/jobs", status_code=202)
async def create_production_job(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form(""),
    corrections_json: str = Form("{}"), metadata_json: str = Form("{}"), narration_plan_json: str = Form(""),
    create_navigation_sections_flag: bool = Form(False, alias="create_navigation_sections"),
    pages: str = Form(""),
) -> dict[str, object]:
    job_store.initialize_store(database_path)
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    filename = Path(file.filename or "document").name
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    if not provider_api_key(active_tts_provider()):
        raise HTTPException(status_code=503, detail="Configura una clave y voz TTS antes de iniciar el trabajo.")
    # Validate the voice and its accent before spending anything on generation.
    try:
        voice = resolve_narration_voice(voice_id)
    except ProviderError as exc:
        raise HTTPException(status_code=422 if exc.kind == "unsupported_voice" else 503, detail=exc.message) from exc
    try:
        structure = parse_book(filename, content, pages)
        metadata = book_metadata(structure, metadata_json)
        plan = build_narration_plan([(chapter.title, chapter.paragraphs) for chapter in structure.chapters])
        selected_ids = _selected_narration_ids(narration_plan_json, plan)
        chapters = apply_narration_selection(plan, selected_ids)
        chapters = apply_paragraph_corrections(chapters, corrections_json)
        if create_navigation_sections_flag:
            chapters = create_navigation_sections(chapters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    file_hash = hashlib.sha256(content).hexdigest()
    # Another page range of the same file is another book, not a duplicate job.
    source_hash = hashlib.sha256(f"{file_hash}\u0000pages={pages.strip()}".encode()).hexdigest() if pages.strip() else file_hash
    # A second click on "generate" must not start a second paid job over the
    # same book, text selection and voice.
    existing = job_store.find_active_job(database_path, source_hash, voice.voice_id, voice_speed)
    if existing:
        return {"job_id": existing["job_id"], "state": existing["state"],
                "total_sentences": existing["total_sentences"],
                "status_url": f"/api/jobs/{existing['job_id']}",
                "reused_existing_job": True,
                "detail": "Ya existe un trabajo en curso para este libro y esta voz."}
    job_id = str(uuid.uuid4())
    job_dir = storage_root / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    source_path = job_dir / filename
    source_path.write_bytes(content)
    chapter_records = [{"title": title, "paragraphs": paragraphs} for title, paragraphs in chapters]
    (job_dir / "reviewed-source.json").write_text(json.dumps({
        "source_sha256": file_hash, "source_filename": filename, "pages": pages.strip(), "chapters": chapter_records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    lexicon = job_store.get_lexicon(database_path)
    sentences: list[dict] = []
    for chapter_index, (chapter_title, paragraphs) in enumerate(chapters):
        # Put the real chapter label into the narration sequence before its prose.
        # It is marked so the DAISY exporter can synchronize it with the heading.
        heading_sentences = segment_paragraphs([chapter_title], f"{source_hash}:heading", chapter_index)
        for sentence in heading_sentences:
            sentence["chapter_title"] = chapter_title
            sentence["is_heading"] = True
            sentence["synthesis_text"] = build_synthesis_text(sentence["text"], lexicon)
            sentences.append(sentence)
        # One unit per paragraph, split exactly as the direct download and the
        # preview split it, so all three share the same cached audio.
        units = [part for paragraph in paragraphs for part in make_paragraphs(paragraph)]
        for sentence in segment_paragraphs(units, source_hash, chapter_index):
            sentence["sequence"] += len(heading_sentences)
            sentence["chapter_title"] = chapter_title
            sentence["is_heading"] = False
            sentence["synthesis_text"] = build_synthesis_text(sentence["text"], lexicon)
            sentences.append(sentence)
    if not sentences:
        raise HTTPException(status_code=422, detail="No hemos encontrado frases que se puedan narrar.")
    job_store.create_job(database_path, {
        "job_id": job_id, "source_name": filename, "source_path": str(source_path),
        "source_hash": source_hash, "chapters": chapter_records, "metadata": metadata,
        "voice_id": voice.voice_id, "voice_speed": voice_speed,
    }, sentences)
    job_store.add_event(database_path, job_id, "INFO", "job queued", {
        "sentences": len(sentences), "chapters": len(chapter_records),
    })
    # Report up front how much of this book is already paid for.
    cached = 0
    for sentence in sentences:
        try:
            if audio_cache.contains(narration_cache_key(sentence["synthesis_text"], voice_speed, voice.voice_id)):
                cached += 1
        except ProviderError:
            break
    job_logger(job_dir, job_id, book_id=source_hash).info(
        "job.queue", "job queued", source_name=filename, source_sha256=source_hash[:16],
        chapters=len(chapter_records), sentences=len(sentences), provider=active_tts_provider(),
        voice=voice.voice_id, voice_language=voice.language, voice_speed=voice_speed,
        already_cached_sentences=cached,
    )
    await _start_or_resume_job(job_id)
    return {"job_id": job_id, "state": "queued", "total_sentences": len(sentences), "status_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}")
async def production_job_status(job_id: str) -> dict[str, object]:
    job_store.initialize_store(database_path)
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    counts: dict[str, int] = {}
    for sentence in job["sentences"]:
        counts[sentence["state"]] = counts.get(sentence["state"], 0) + 1
    keys = ("job_id", "state", "source_name", "total_sentences", "created_at", "updated_at",
            "started_at", "completed_at", "output_path", "error", "voice_id")
    # Report the voice this job was produced with, so the interface can never
    # show a job next to a different voice the user has since selected.
    voice = find_tts_voice(job["voice_id"])
    return {key: job[key] for key in keys} | {
        "voice_name": voice.name if voice else "",
        "voice_language": voice.language if voice else "",
        "progress": counts, "sentences": job["sentences"], "events": job_store.get_events(database_path, job_id),
    }


@app.get("/api/jobs/{job_id}/review-queue")
async def production_review_queue(job_id: str) -> dict[str, object]:
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    items = [item for item in job["sentences"] if item["state"] in {"needs_human_review", "unresolved", "failed"}]
    return {"job_id": job_id, "total": len(items), "items": items}


@app.post("/api/jobs/{job_id}/review")
async def resolve_production_sentence(job_id: str, decision: SentenceDecision) -> dict[str, object]:
    """Apply a reviewer's decision now and answer with the paragraph as it stands.

    "Correct" and "retry" narrate the paragraph inside this request, so the
    page can play the new reading the moment it answers; they used to only
    queue it, and the reviewer could not hear what had changed.
    """
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    position, sentence = next(((index, item) for index, item in enumerate(job["sentences"])
                               if item["sentence_id"] == decision.sentence_id), (-1, None))
    if not sentence:
        raise HTTPException(status_code=404, detail="Frase no encontrada.")
    if decision.decision not in {"accept", "retry", "correct"}:
        raise HTTPException(status_code=422, detail="La decisión debe ser accept, retry o correct.")
    key = (job_id, decision.sentence_id)
    if key in units_in_progress or sentence["state"] == "generating":
        raise HTTPException(status_code=409, detail="Este párrafo ya se está narrando. Espera unos segundos.")
    job_dir = Path(job["source_path"]).parent
    if decision.decision == "accept":
        if not sentence["audio_path"]:
            raise HTTPException(status_code=409, detail="No existe audio para aceptar esta frase.")
        job_store.update_sentence(database_path, job_id, decision.sentence_id, state="verified", last_error="Accepted by human reviewer.")
    else:
        corrected = decision.corrected_text.strip()
        if decision.decision == "correct" and (not corrected or len(corrected) > 2000):
            raise HTTPException(status_code=422, detail="El texto corregido debe contener entre 1 y 2 000 caracteres.")
        units_in_progress.add(key)
        try:
            if decision.decision == "correct":
                with sqlite3.connect(database_path) as connection:
                    connection.execute("UPDATE production_sentences SET text=?,synthesis_text=? WHERE job_id=? AND sentence_id=?",
                                       (corrected, build_synthesis_text(corrected, job_store.get_lexicon(database_path)), job_id, decision.sentence_id))
            item = next(row for row in job_store.get_job(database_path, job_id)["sentences"]
                        if row["sentence_id"] == decision.sentence_id)
            result = await _produce_unit(
                job, item, job_dir=job_dir, log=job_logger(job_dir, job_id, book_id=job["source_hash"]),
                context=_unit_context(job, position), interactive=True, fresh_take=decision.decision == "retry",
            )
            if not result.produced:
                # Nothing new was narrated (no credit, a rejected key...): put the
                # paragraph back exactly as it was, so its audio is not lost.
                _restore_unit(job_id, sentence)
                kind, _, reason = result.error.partition(": ")
                status = 503 if kind.startswith(("credit", "credentials", "not_configured")) else 502
                raise HTTPException(status_code=status, detail=f"No se pudo narrar el párrafo: {reason or kind} "
                                                               "No se ha cambiado nada.")
        finally:
            units_in_progress.discard(key)
    job_store.add_event(database_path, job_id, "INFO", "human review decision", {
        "sentence_id": decision.sentence_id, "decision": decision.decision,
        "corrected_text": decision.corrected_text if decision.decision == "correct" else None,
    })
    _job_log(job_dir, "human review decision", sentence_id=decision.sentence_id, decision=decision.decision)
    state = await _finish_after_review(job_id)
    updated = next(row for row in job_store.get_job(database_path, job_id)["sentences"]
                   if row["sentence_id"] == decision.sentence_id)
    return {"job_id": job_id, "sentence_id": decision.sentence_id, "state": state, "sentence": updated}


def _restore_unit(job_id: str, before: dict) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE production_sentences SET text=?,synthesis_text=? WHERE job_id=? AND sentence_id=?",
                           (before["text"], before["synthesis_text"], job_id, before["sentence_id"]))
    job_store.update_sentence(
        database_path, job_id, before["sentence_id"], state=before["state"], audio_path=before["audio_path"],
        recognized_text=before["recognized_text"], issues_json=json.dumps(before["issues"], ensure_ascii=False),
        acoustic_json=json.dumps(before["acoustic"], ensure_ascii=False), confidence=before["confidence"],
        last_error=before["last_error"], technical_attempts=before["technical_attempts"],
        quality_retakes=before["quality_retakes"], reused_audio=before["reused_audio"],
    )


async def _finish_after_review(job_id: str) -> str:
    """Let the job catch up with a decision: assemble the book once nothing is left.

    The worker skips everything already decided, so this is quick, and it
    rebuilds only the chapters whose audio changed.
    """
    task = active_jobs.get(job_id)
    if task and not task.done():
        # Still running: it reaches the end and assembles the book by itself.
        return "running"
    job_store.update_job(database_path, job_id, state="queued", cancel_requested=0, completed_at=None)
    await _start_or_resume_job(job_id)
    return "queued"


@app.post("/api/jobs/{job_id}/resume")
async def resume_production_job(job_id: str) -> dict[str, str]:
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE production_sentences SET state='pending',last_error='' WHERE job_id=? AND state='failed'", (job_id,))
    job_store.update_job(database_path, job_id, state="queued", cancel_requested=0, completed_at=None, error="")
    await _start_or_resume_job(job_id)
    return {"job_id": job_id, "state": "queued"}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_production_job(job_id: str) -> dict[str, str]:
    if not job_store.get_job(database_path, job_id):
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    job_store.update_job(database_path, job_id, cancel_requested=1)
    job_store.add_event(database_path, job_id, "WARNING", "cancellation requested")
    return {"job_id": job_id, "state": "cancellation_requested"}


@app.get("/api/jobs/{job_id}/artifacts")
async def download_job_delivery(job_id: str) -> FileResponse:
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    if job["state"] != "completed" or not job["output_path"] or not Path(job["output_path"]).is_file():
        raise HTTPException(status_code=409, detail="El paquete completo todavía no está disponible.")
    return FileResponse(job["output_path"], media_type="application/zip", filename=f"{job_id}-once-delivery.zip")


@app.get("/api/jobs/{job_id}/sentences/{sentence_id}/audio")
async def production_sentence_audio(job_id: str, sentence_id: str) -> FileResponse:
    job = job_store.get_job(database_path, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    sentence = next((item for item in job["sentences"] if item["sentence_id"] == sentence_id), None)
    if not sentence or not sentence["audio_path"]:
        raise HTTPException(status_code=404, detail="No hay audio disponible para esta frase.")
    job_dir = Path(job["source_path"]).parent.resolve()
    audio_path = (job_dir / sentence["audio_path"]).resolve()
    if not audio_path.is_relative_to(job_dir) or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="El audio de esta frase no está disponible.")
    return FileResponse(audio_path, media_type="audio/mpeg", filename=f"{sentence_id}.mp3",
                        headers={"Cache-Control": "no-store"})


def book_metadata(structure, metadata_json: str = "{}") -> dict[str, str]:
    metadata = {
        "title": structure.title,
        "author": structure.author,
        "edition": structure.edition,
        "publication_year": structure.publication_year,
        "publisher": structure.publisher,
    }
    try:
        overrides = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Los datos bibliográficos no tienen un formato válido.") from exc
    if not isinstance(overrides, dict):
        raise ValueError("Los datos bibliográficos no son válidos.")
    for key in metadata:
        if key not in overrides:
            continue
        value = overrides[key]
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("Cada dato bibliográfico debe contener como máximo 500 caracteres.")
        metadata[key] = value.strip()
    if not metadata["title"]:
        raise ValueError("El título del libro no puede quedar vacío.")
    return metadata


def create_navigation_sections(chapters: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """Create honest, length-based navigation sections when a source has no chapter markers."""
    if len(chapters) != 1 or len(chapters[0][1]) < 2:
        return chapters
    _title, paragraphs = chapters[0]
    word_count = sum(len(paragraph.split()) for paragraph in paragraphs)
    target_words = max(120, min(1200, (word_count + 4) // 5))
    sections: list[tuple[str, list[str]]] = []
    active: list[str] = []
    active_words = 0
    for index, paragraph in enumerate(paragraphs):
        active.append(paragraph)
        active_words += len(paragraph.split())
        if active_words >= target_words and len(sections) < 4 and index < len(paragraphs) - 1:
            sections.append((f"Sección {len(sections) + 1}", active))
            active = []
            active_words = 0
    if active:
        sections.append((f"Sección {len(sections) + 1}", active))
    return sections if len(sections) >= 2 else chapters


async def synthesize(
    text: str, voice_speed: float = 1.0, voice_id: str = "", *, context: tuple[str, str] = NO_CONTEXT,
) -> bytes:
    """Generate narration audio for one passage with the active TTS provider.

    Long passages are split at natural boundaries to stay under provider limits
    and reassembled; MP3 is a frame stream, so concatenating encoded segments
    keeps playback continuous without holding a whole book in memory.
    ``context`` is the text before and after the passage, which providers that
    support it use to keep the intonation continuous across joins.
    """
    provider = active_tts_provider()
    voice = resolve_narration_voice(voice_id, enforce_accent=False)
    model = tts_model_for(provider)
    api_key = provider_api_key(provider)
    chunks = [chunk for chunk in _tts_chunks(text) if chunk] or [text]
    previous, following = context
    parts = [
        await providers.synthesize(
            chunk, voice, speed=voice_speed, api_key=api_key, model=model,
            previous_text=chunks[index - 1] if index else previous,
            next_text=chunks[index + 1] if index + 1 < len(chunks) else following,
        )
        for index, chunk in enumerate(chunks)
    ]
    return parts[0] if len(parts) == 1 else b"".join(parts)


async def synthesize_cached(
    text: str, voice_speed: float = 1.0, voice_id: str = "", *, refresh: bool = False,
    context: tuple[str, str] = NO_CONTEXT,
) -> tuple[bytes, bool]:
    """Return ``(audio, reused)``, paying the provider only for new audio.

    The cache key covers the provider, voice, model, speed and exact synthesis
    text, so an unchanged sentence is never generated twice, and correcting one
    sentence invalidates only that sentence. The context is deliberately not
    part of the key: correcting a paragraph must not re-bill its neighbours.
    """
    provider = active_tts_provider()
    voice = resolve_narration_voice(voice_id, enforce_accent=False)
    model = tts_model_for(provider)
    key = providers.cache_fingerprint(provider, voice, model, voice_speed, text)
    metadata = {
        "provider": provider, "voice_id": voice.voice_id, "voice_language": voice.language,
        "model": model, "speed": voice_speed if providers.supports_speed(provider) else None,
        "characters": len(text), "created_at": job_store.utc_now(),
    }
    return await audio_cache.get_or_create(
        key, lambda: synthesize(text, voice_speed, voice_id, context=context), metadata, refresh=refresh,
    )


async def narrate_with_backoff(
    text: str, voice_speed: float = 1.0, voice_id: str = "", *, context: tuple[str, str] = NO_CONTEXT,
) -> tuple[bytes, bool]:
    """``synthesize_cached``, retrying the failures that clear with time.

    Narrating several paragraphs at once makes a brief rate limit (HTTP 429)
    more likely; waiting and retrying beats failing the whole book. An empty
    balance or a rejected key is permanent and fails at once.
    """
    for delay in (*retry_delays, None):
        try:
            return await synthesize_cached(text, voice_speed, voice_id, context=context)
        except ProviderError as exc:
            if exc.permanent or delay is None:
                raise
            logger.warning("Narration failed (%s); retrying in %ss", exc.kind, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def narration_cache_key(text: str, voice_speed: float, voice_id: str) -> str:
    """Expose the cache identity so callers can report reuse before generating."""
    provider = active_tts_provider()
    voice = resolve_narration_voice(voice_id, enforce_accent=False)
    return providers.cache_fingerprint(provider, voice, tts_model_for(provider), voice_speed, text)


def _tts_chunks(text: str, max_chars: int = 1800) -> list[str]:
    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        boundary = max(remaining.rfind(mark, 0, max_chars + 1) for mark in (". ", "? ", "! ", "\n"))
        if boundary < max_chars // 2:
            boundary = remaining.rfind(" ", 0, max_chars + 1)
        if boundary < 1:
            boundary = max_chars
        else:
            boundary += 1
        chunks.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    return chunks


async def synthesize_with_alignment(text: str, voice_speed: float = 1.0, voice_id: str = "") -> tuple[bytes, list[dict[str, float | str]] | None]:
    """Generate narration and request word timestamps for transcript highlighting.

    Reuses cached preview audio so repeating a preview costs nothing, and keeps
    working without timings when the transcriber is unavailable.
    """
    audio, _reused = await synthesize_cached(text, voice_speed, voice_id)
    return audio, await _preview_alignment(audio, text)


async def synthesize_preview(
    paragraphs: list[str], voice_speed: float = 1.0, voice_id: str = "", *, following: str = "",
) -> tuple[bytes, list[dict[str, float | str]] | None]:
    """Narrate the preview paragraph by paragraph through the shared cache.

    The cache keys match the ones the download and the reviewed run use, so
    the preview is never paid for twice. ``following`` is the paragraph after
    the preview, the context its last paragraph is narrated with.
    """
    job_store.initialize_store(database_path)
    lexicon = job_store.get_lexicon(database_path)
    texts = [build_synthesis_text(paragraph, lexicon) for paragraph in paragraphs]

    async def narrate(index: int) -> bytes:
        context = narration_context(texts[index - 1] if index else "",
                                    texts[index + 1] if index + 1 < len(texts) else following)
        return (await narrate_with_backoff(texts[index], voice_speed, voice_id, context=context))[0]

    pieces = await _run_limited([functools.partial(narrate, index) for index in range(len(texts))],
                                narration_concurrency)
    # Joined as one clean MP3 stream: gluing the files byte by byte left each
    # file's header in the middle of the audio, which some players stumble on.
    audio = pieces[0] if len(pieces) == 1 else await asyncio.to_thread(daisy_join_mp3, pieces)
    return audio, await _preview_alignment(audio, "\n\n".join(paragraphs))


async def _preview_alignment(audio: bytes, text: str) -> list[dict[str, float | str]] | None:
    """Word timings for highlighting; the preview still plays without them."""
    try:
        result = await _transcription_result(audio, "preview.mp3", "audio/mpeg", with_timestamps=True,
                                             language=recognition_language(text))
        return _alignment_from_fish_segments(text, result.segments) or None
    except Exception as exc:
        logger.warning("Timing transcription unavailable: %s", type(exc).__name__)
        return None


def _phrases_from_alignment(
    characters: list[str], starts: list[float], ends: list[float], time_offset: float, text_offset: int,
    max_phrase_chars: int = 180,
) -> list[dict[str, float | str]]:
    phrases: list[dict[str, float | str]] = []
    begin = 0
    for index, character in enumerate(characters):
        at_boundary = character in {".", "?", "!", "…", "\n"}
        at_limit = index - begin + 1 >= max_phrase_chars
        final = index == len(characters) - 1
        if at_boundary or at_limit or final:
            phrase = "".join(characters[begin:index + 1])
            if phrase.strip():
                phrases.append({
                    "text": phrase,
                    "start": time_offset + float(starts[begin]),
                    "end": time_offset + float(ends[index]),
                    "text_start": text_offset + begin,
                    "text_end": text_offset + index + 1,
                })
            begin = index + 1
    return phrases


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def narration_is_configured(voice_id: str = "") -> bool:
    """True when the active provider has credentials and a usable voice."""
    provider = active_tts_provider()
    if not provider_api_key(provider):
        return False
    try:
        resolve_narration_voice(voice_id)
    except ProviderError:
        return False
    return True


@app.get("/api/capabilities")
async def capabilities() -> dict[str, object]:
    provider = active_tts_provider()
    voice_id = configured_voice_id()
    voice = find_tts_voice(voice_id, provider)
    catalogue = tts_voices(provider)
    return {
        "daisy_profile": "DAISY 3 (provisional, validación estructural propia)",
        "tts_provider": provider,
        "asr_provider": active_asr_provider(),
        "tts_configured": narration_is_configured(),
        "voice": voice_id,
        "voice_name": voice.name if voice else "",
        "voice_language": voice.language if voice else "",
        "voice_is_peninsular": bool(voice and voice.is_peninsular),
        "voice_accent_verified": bool(voice and voice.accent_known),
        "requires_peninsular_voice": settings.require_peninsular_voice,
        "voice_speed_control": providers.supports_speed(provider) and bool(provider_api_key(provider)),
        "voice_id_configurable": bool(catalogue) and bool(provider_api_key(provider)),
        "audio_review_configured": audio_review_is_configured(),
        "offline_mock_mode": provider == "mock",
        "chapter_audio": True,
        "chapter_smil": True,
        "full_text_in_package": True,
        "ncx_navigation": True,
        "ncc_navigation": False,
        "once_intro": bool(settings.once_intro_template and settings.once_identification_statement and settings.once_intro_filename),
        "once_bibliographic_audio": bool(settings.once_bibliographic_template and settings.once_legal_statement and settings.once_bibliographic_filename),
        # The production pipeline really does transcribe, compare and retake;
        # reporting these as false misrepresented what a job performs.
        "automatic_audio_review": audio_review_is_configured() and settings.quality_review_enabled,
        "retakes": settings.quality_review_enabled,
        "max_quality_retakes": MAX_QUALITY_RETAKES,
        "max_technical_attempts": len(retry_delays) + 1,
        "sentence_audio_cache": True,
        "official_daisy_pipeline_validation": False,
    }


@app.get("/api/voices")
async def list_configured_voices() -> dict[str, object]:
    provider = active_tts_provider()
    catalogue = tts_voices(provider)
    if not catalogue:
        raise HTTPException(status_code=503,
                            detail=f"El proveedor de voz «{provider}» no expone ninguna voz configurada.")
    if not provider_api_key(provider):
        raise HTTPException(status_code=503, detail="La voz no está configurada en el servidor.")
    if provider == "fishaudio":
        # Honour the configured Fish allow-list so an account that only owns
        # some reference voices does not advertise the others.
        allowed = [settings.fish_audio_voice_id.strip()]
        allowed.extend(value.strip() for value in settings.fish_audio_extra_voice_ids.split(",") if value.strip())
        allowed_ids = [value for value in dict.fromkeys(allowed) if value]
        if allowed_ids:
            catalogue = tuple(voice for voice in catalogue if voice.voice_id in allowed_ids)
    # Spain Spanish first: that is the accent this product narrates in.
    ordered = sorted(catalogue, key=lambda voice: (not voice.is_peninsular, voice.name))
    return {"provider": provider, "default_voice_id": configured_voice_id(),
            "voices": [voice.as_dict() for voice in ordered]}


def compare_transcripts(source_text: str, spoken_text: str) -> list[dict[str, str]]:
    return compare_spanish_transcripts(source_text, spoken_text)


def _provider_http_status(error: ProviderError) -> int:
    """Map a provider failure onto the status the browser should see."""
    return {
        "not_configured": 503,
        "credentials": 503,
        "credit": 503,
        "unsupported_voice": 422,
        "rate_limit": 429,
        "timeout": 504,
    }.get(error.kind, 502)


@app.post("/api/audio/review")
async def review_audio(file: UploadFile = File(...), source_text: str = Form(...)) -> dict[str, object]:
    if not audio_review_is_configured():
        raise HTTPException(status_code=503,
                            detail="Configura un servicio de transcripción (ASR_PROVIDER) para revisar el audio.")
    if not source_text.strip() or len(source_text) > 20000:
        raise HTTPException(status_code=422, detail="El texto de referencia debe tener entre 1 y 20 000 caracteres.")
    audio = await file.read(50 * 1024 * 1024 + 1)
    if len(audio) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El audio de revisión supera el límite de 50 MB.")
    try:
        spoken_text, recognizer = await transcribe_audio(
            audio, file.filename or "preview.mp3", file.content_type or "audio/mpeg"
        )
    except ProviderError as exc:
        # The provider layer already produced an actionable Spanish message.
        logger.warning("Audio review failed: provider=%s kind=%s", active_asr_provider(), exc.kind)
        raise HTTPException(status_code=_provider_http_status(exc), detail=exc.message) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="El servicio de transcripción tardó demasiado. Inténtalo de nuevo.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Soni2 no pudo conectar con el servicio de transcripción.") from exc
    verdict, issues, confidence = qa_review.evaluate(source_text, spoken_text)
    return {
        "recognized_text": spoken_text,
        "recognizer": recognizer,
        "verdict": verdict,
        "confidence": confidence,
        "issues": issues,
        "review_note": "Las diferencias se calculan tras normalizar el español, así que «147» y «ciento cuarenta "
                       "y siete» coinciden. Las marcadas como ambiguas necesitan criterio humano y no confirman "
                       "por sí solas que haya un error.",
    }


@app.post("/api/audio/retake")
async def regenerate_retake(
    text: str = Form(...), voice_speed: float = Form(1.0), voice_id: str = Form("")
) -> PreviewResult:
    if not text.strip() or len(text) > 5000:
        raise HTTPException(status_code=422, detail="El fragmento debe tener entre 1 y 5 000 caracteres.")
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    try:
        audio, alignment = await synthesize_with_alignment(text, voice_speed, voice_id)
    except ProviderError as exc:
        logger.warning("Segment retake failed: provider=%s kind=%s", active_tts_provider(), exc.kind)
        raise HTTPException(status_code=_provider_http_status(exc), detail=exc.message) from exc
    except Exception as exc:
        logger.warning("Segment retake generation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="No se ha podido generar la nueva versión del fragmento.") from exc
    return PreviewResult(audio_base64=base64.b64encode(audio).decode("ascii"), transcript=text, alignment=alignment)


@app.post("/api/books/structure")
async def analyze_book_structure(file: UploadFile = File(...), pages: str = Form("")) -> dict[str, object]:
    filename = Path(file.filename or "document").name
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    try:
        structure = parse_book(filename, content, pages)
        plan = build_narration_plan([(chapter.title, chapter.paragraphs) for chapter in structure.chapters])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"title": structure.title, "sections": [section.as_dict() for section in plan],
            "selected_ids": [section.id for section in plan if section.included]}


@app.post("/api/books/preview")
async def create_preview(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form(""), narration_plan_json: str = Form(""),
    pages: str = Form(""),
) -> PreviewResult:
    diagnostic_id = uuid.uuid4().hex[:12]
    filename = Path(file.filename or "document").name
    logger.info("Preview %s started: file=%r voice_selected=%s provider=%s", diagnostic_id, filename,
                bool(voice_id), settings.tts_provider.lower())
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    logger.info("Preview %s upload received: bytes=%d", diagnostic_id, len(content))
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    try:
        structure = parse_book(filename, content, pages)
        plan = build_narration_plan([(chapter.title, chapter.paragraphs) for chapter in structure.chapters])
        selected_ids = _selected_narration_ids(narration_plan_json, plan)
        selected_chapters = apply_narration_selection(plan, selected_ids)
        text = clean_text("\n\n".join("\n\n".join(([title] if title else []) + paragraphs) for title, paragraphs in selected_chapters))
        if len(text) < 20:
            raise ValueError("No hemos encontrado suficiente texto legible en este documento.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # The preview is the book's first whole paragraphs, split exactly as the
    # download splits them. Each is narrated and cached on its own, so the
    # download later reuses this audio instead of paying for it a second time.
    units = [part for _title, body in selected_chapters for paragraph in body for part in make_paragraphs(paragraph)]
    preview_units: list[str] = []
    for unit in units:
        if preview_units and sum(len(item) + 2 for item in preview_units) + len(unit) > settings.preview_characters:
            break
        preview_units.append(unit)
    preview_text = "\n\n".join(preview_units)
    following_unit = units[len(preview_units)] if len(units) > len(preview_units) else ""
    logger.info("Preview %s text extracted: chapters=%d selected_sections=%d characters=%d preview_characters=%d",
                diagnostic_id, len(structure.chapters), len(selected_chapters), len(text), len(preview_text))
    audio = b""
    alignment = None
    audio_error = None
    if not narration_is_configured(voice_id):
        audio_error = ("Texto extraído. Configura un servicio de voz y una voz válida para escuchar el audio.")
        logger.error("Preview %s stopped before synthesis: TTS credentials or voice are not configured", diagnostic_id)
    else:
        try:
            audio, alignment = await synthesize_preview(preview_units, voice_speed, voice_id,
                                                        following=following_unit)
            logger.info("Preview %s synthesis succeeded: audio_bytes=%d aligned=%s", diagnostic_id, len(audio), bool(alignment))
        except ProviderError as exc:
            # Say exactly what is wrong — exhausted credit, a rejected key, an
            # unsupported voice — instead of a generic failure.
            logger.warning("Preview %s narration failed: provider=%s kind=%s", diagnostic_id,
                           active_tts_provider(), exc.kind)
            audio_error = exc.message
        except httpx.HTTPStatusError as exc:
            logger.exception("Preview %s narration provider returned HTTP %s", diagnostic_id, exc.response.status_code)
            status = exc.response.status_code
            if status == 401:
                audio_error = "El servicio de narración rechazó la clave de API. Comprueba la configuración del servidor."
            elif status == 402:
                audio_error = "La cuenta del servicio de narración necesita saldo o un plan con acceso a generación de audio."
            elif status == 429:
                audio_error = "El servicio de voz alcanzó su límite de solicitudes (HTTP 429). Inténtalo de nuevo más tarde."
            else:
                audio_error = f"El proveedor de voz respondió HTTP {status}. Revisa su configuración y cuota."
        except httpx.TimeoutException:
            logger.exception("Preview %s narration timed out", diagnostic_id)
            audio_error = "El servicio de voz tardó demasiado en responder. Inténtalo de nuevo."
        except httpx.RequestError:
            logger.exception("Preview %s could not connect to the narration provider", diagnostic_id)
            audio_error = "No se pudo conectar con el servicio de voz. Comprueba la conexión del servidor e inténtalo de nuevo."
        except Exception as exc:
            logger.exception("Preview %s narration failed (%s)", diagnostic_id, type(exc).__name__)
            audio_error = f"No se pudo generar el audio ({type(exc).__name__}). Facilita el ID {diagnostic_id} al administrador."

    if audio_error:
        logger.error("Preview %s finished without audio: %s", diagnostic_id, audio_error)

    return PreviewResult(
        audio_base64=base64.b64encode(audio).decode("ascii"),
        transcript=preview_text,
        alignment=alignment,
        audio_error=audio_error,
        diagnostic_id=diagnostic_id,
        chapters=[{"title": title, "paragraphs": paragraphs} for title, paragraphs in selected_chapters],
        narration_plan=[section.as_dict() for section in plan],
        title=structure.title,
        author=structure.author,
        edition=structure.edition,
        publication_year=structure.publication_year,
        publisher=structure.publisher,
    )


async def _read_package_upload(file: UploadFile, voice_speed: float, voice_id: str) -> tuple[str, bytes]:
    """Validate a download request before any narration is paid for."""
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    filename = file.filename or "document"
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    # Check credentials and the voice before parsing a whole book, so a
    # misconfiguration fails immediately instead of part way through narration.
    if not provider_api_key(active_tts_provider()):
        raise HTTPException(status_code=503, detail="Configura una clave y voz TTS antes de generar el audiolibro.")
    try:
        resolve_narration_voice(voice_id)
    except ProviderError as exc:
        raise HTTPException(status_code=_provider_http_status(exc), detail=exc.message) from exc
    return filename, content


@dataclass
class PackageProgress:
    """How far a direct download has got, for the page's progress bar."""

    total: int = 0
    done: int = 0
    reused: int = 0


async def _build_direct_package(
    filename: str, content: bytes, work_dir: Path, *, voice_speed: float, voice_id: str,
    corrections_json: str, metadata_json: str, narration_plan_json: str, create_navigation_sections_flag: bool,
    progress: PackageProgress | None = None, pages: str = "",
) -> tuple[Path, str]:
    """Narrate the selected book and write its DAISY ZIP; returns the ZIP and its download name."""
    progress = progress or PackageProgress()
    structure = parse_book(filename, content, pages)
    metadata = book_metadata(structure, metadata_json)
    text = clean_text(structure.as_text())
    if len(text) < 20:
        raise ValueError("No hemos encontrado suficiente texto legible en este documento.")
    plan = build_narration_plan([(chapter.title, chapter.paragraphs) for chapter in structure.chapters])
    selected_ids = _selected_narration_ids(narration_plan_json, plan)
    source_chapters = apply_narration_selection(plan, selected_ids)
    source_chapters = apply_paragraph_corrections(source_chapters, corrections_json)
    if create_navigation_sections_flag:
        source_chapters = create_navigation_sections(source_chapters)
    chapter_paragraphs = [
        (title, [part for paragraph in body for part in make_paragraphs(paragraph)])
        for title, body in source_chapters
    ]
    chapter_paragraphs = [(heading, body) for heading, body in chapter_paragraphs if body]
    paragraphs = [paragraph for _, body in chapter_paragraphs for paragraph in body]
    if not paragraphs:
        raise ValueError("No hemos encontrado párrafos que se puedan narrar en este documento.")

    front_matter: list[tuple[str, str, bytes, str]] = []
    values = {
        **metadata,
        "once_identification_statement": settings.once_identification_statement,
        "legal_statement": settings.once_legal_statement,
    }
    if settings.once_intro_template:
        if not values["title"] or not values["author"] or not values["once_identification_statement"]:
            raise ValueError("Configure approved ONCE identification wording with title and author.")
        if any(token not in settings.once_intro_template for token in ("{title}", "{author}", "{once_identification_statement}")):
            raise ValueError("The intro template must include title, author, and the approved ONCE identification statement.")
        if not settings.once_intro_filename:
            raise ValueError("Configure the introductory filename from the approved ONCE example.")
        if Path(settings.once_intro_filename).name != settings.once_intro_filename or Path(settings.once_intro_filename).suffix.lower() != ".mp3":
            raise ValueError("ONCE_INTRO_FILENAME must be a single MP3 filename.")
        try:
            intro_text = settings.once_intro_template.format(**values)
        except (KeyError, ValueError) as exc:
            raise ValueError("La plantilla introductoria contiene una variable no válida.") from exc
        intro_audio, _reused = await synthesize_cached(intro_text, voice_speed, voice_id)
        front_matter.append(("Introducción", intro_text, intro_audio, settings.once_intro_filename))
    if settings.once_bibliographic_template:
        required = ("title", "author", "edition", "publication_year", "publisher", "legal_statement")
        if any(not values[key] for key in required):
            raise ValueError("Completa título, autor, edición, año y editorial para crear el audio bibliográfico.")
        if not settings.once_bibliographic_filename:
            raise ValueError("Configure the bibliographic filename from the approved ONCE example.")
        if Path(settings.once_bibliographic_filename).name != settings.once_bibliographic_filename or Path(settings.once_bibliographic_filename).suffix.lower() != ".mp3":
            raise ValueError("ONCE_BIBLIOGRAPHIC_FILENAME must be a single MP3 filename.")
        if settings.once_bibliographic_filename.casefold() == settings.once_intro_filename.casefold() and settings.once_intro_template:
            raise ValueError("ONCE introductory and bibliographic filenames must be different.")
        if any(f"{{{key}}}" not in settings.once_bibliographic_template for key in required):
            raise ValueError("The bibliographic template must include every metadata field and the approved legal statement.")
        try:
            bibliographic_text = settings.once_bibliographic_template.format(**values)
        except (KeyError, ValueError) as exc:
            raise ValueError("La plantilla bibliográfica contiene una variable no válida.") from exc
        biblio_audio, _reused = await synthesize_cached(bibliographic_text, voice_speed, voice_id)
        front_matter.append(("Datos bibliográficos", bibliographic_text, biblio_audio, settings.once_bibliographic_filename))

    # One request per paragraph, so the voice reads it as connected speech, and
    # several paragraphs at once. The reviewed run and the preview narrate the
    # same paragraph units with the same synthesis text, so audio any of them
    # paid for is reused here.
    job_store.initialize_store(database_path)
    lexicon = job_store.get_lexicon(database_path)
    progress.total = len(paragraphs)
    texts = [build_synthesis_text(paragraph, lexicon) for paragraph in paragraphs]

    async def narrate(index: int) -> bytes:
        context = narration_context(texts[index - 1] if index else "",
                                    texts[index + 1] if index + 1 < len(texts) else "")
        audio, reused = await narrate_with_backoff(texts[index], voice_speed, voice_id, context=context)
        progress.done += 1
        progress.reused += int(reused)
        return audio

    audio_segments = await _run_limited(
        [functools.partial(narrate, index) for index in range(len(texts))], narration_concurrency,
    )
    logger.info("Package synthesis finished: paragraphs=%d reused_from_cache=%d concurrency=%d",
                len(paragraphs), progress.reused, narration_concurrency())

    # Building and zipping the package is file work; a worker thread keeps the
    # server answering progress requests meanwhile.
    package_dir = await asyncio.to_thread(functools.partial(
        build_daisy_package,
        metadata["title"] or Path(filename).stem or "Libro",
        paragraphs,
        audio_segments,
        work_dir / "book",
        chapters=chapter_paragraphs,
        author=metadata["author"],
        edition=metadata["edition"],
        publication_year=metadata["publication_year"],
        publisher=metadata["publisher"],
        front_matter=front_matter,
        # Joined by ffmpeg into clean streams when it is available (it is in
        # Docker); byte-joined MP3 files confuse players about their length.
        use_ffmpeg=bool(filesystem.which("ffmpeg")),
    ))
    await asyncio.to_thread(validate_daisy_package, package_dir)
    archive_path = work_dir / "soni2-daisy.zip"
    await asyncio.to_thread(_zip_package, package_dir, archive_path)
    safe_name = re.sub(r"[^\w.-]+", "_", Path(filename).stem, flags=re.UNICODE).strip("._") or "libro"
    return archive_path, f"{safe_name}-daisy.zip"


def _zip_package(package_dir: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in package_dir.rglob("*"):
            if path.is_file():
                # Same layout as the reviewed delivery: the package lives in "DAISY 3/".
                add_to_archive(archive, path, "DAISY 3/" + path.relative_to(package_dir).as_posix())


def _package_failure(exc: Exception) -> tuple[int, str]:
    """HTTP status and a Spanish explanation for a failed direct download."""
    if isinstance(exc, ProviderError):
        # Say what is actually wrong — exhausted credit, a rejected key, an
        # unsupported voice — instead of a generic failure.
        logger.warning("Direct package generation failed: provider=%s kind=%s", active_tts_provider(), exc.kind)
        return _provider_http_status(exc), exc.message
    if isinstance(exc, ValueError):
        return 422, str(exc)
    logger.error("Full DAISY package generation failed", exc_info=exc)
    return 503, "No hemos podido crear el libro completo. Comprueba la conexión de narración e inténtalo de nuevo."


@app.post("/api/books/package")
async def create_book_package(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form(""),
    corrections_json: str = Form("{}"), metadata_json: str = Form("{}"), narration_plan_json: str = Form(""),
    create_navigation_sections_flag: bool = Form(False, alias="create_navigation_sections"),
    pages: str = Form(""),
) -> FileResponse:
    """Narrate and download in a single request. The page uses package tasks instead, for progress."""
    filename, content = await _read_package_upload(file, voice_speed, voice_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="soni2-package-"))
    try:
        archive_path, download_name = await _build_direct_package(
            filename, content, temp_dir, voice_speed=voice_speed, voice_id=voice_id,
            corrections_json=corrections_json, metadata_json=metadata_json,
            narration_plan_json=narration_plan_json, create_navigation_sections_flag=create_navigation_sections_flag,
            pages=pages,
        )
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        status, detail = _package_failure(exc)
        raise HTTPException(status_code=status, detail=detail) from exc
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


# --------------------------------------------------------------------------- #
# The direct download as a background task. The page starts it, polls its
# progress and fetches the ZIP when it is ready, instead of waiting minutes on
# one silent request.
# --------------------------------------------------------------------------- #

PACKAGE_TASK_TTL_SECONDS = 3600


@dataclass
class PackageTask:
    task_id: str
    work_dir: Path
    progress: PackageProgress
    state: str = "running"  # running | completed | failed | cancelled
    detail: str = ""
    archive_path: Path | None = None
    download_name: str = ""
    finished_at: float | None = None
    runner: asyncio.Task | None = None


package_tasks: dict[str, PackageTask] = {}


def _package_tasks_root() -> Path:
    return storage_root / "package-tasks"


def _forget_old_package_tasks() -> None:
    """Drop finished downloads after an hour, with their ZIPs."""
    now = time.monotonic()
    for task_id, task in list(package_tasks.items()):
        if task.finished_at is not None and now - task.finished_at > PACKAGE_TASK_TTL_SECONDS:
            shutil.rmtree(task.work_dir, ignore_errors=True)
            package_tasks.pop(task_id, None)


async def _run_package_task(task: PackageTask, filename: str, content: bytes, options: dict) -> None:
    try:
        task.archive_path, task.download_name = await _build_direct_package(
            filename, content, task.work_dir, progress=task.progress, **options,
        )
        task.state = "completed"
    except asyncio.CancelledError:
        task.state = "cancelled"
        shutil.rmtree(task.work_dir, ignore_errors=True)
        raise
    except Exception as exc:
        _status, task.detail = _package_failure(exc)
        task.state = "failed"
        shutil.rmtree(task.work_dir, ignore_errors=True)
    finally:
        task.finished_at = time.monotonic()


def _package_task(task_id: str) -> PackageTask:
    task = package_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404,
                            detail="Esta descarga ya no existe. Vuelve a pulsar «Descargar audiolibro ahora».")
    return task


def _package_task_status(task: PackageTask) -> dict[str, object]:
    return {"task_id": task.task_id, "state": task.state, "total": task.progress.total,
            "done": task.progress.done, "reused": task.progress.reused, "detail": task.detail}


@app.post("/api/books/package-tasks", status_code=202)
async def start_book_package(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form(""),
    corrections_json: str = Form("{}"), metadata_json: str = Form("{}"), narration_plan_json: str = Form(""),
    create_navigation_sections_flag: bool = Form(False, alias="create_navigation_sections"),
    pages: str = Form(""),
) -> dict[str, object]:
    filename, content = await _read_package_upload(file, voice_speed, voice_id)
    _forget_old_package_tasks()
    task_id = uuid.uuid4().hex
    work_dir = _package_tasks_root() / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    task = PackageTask(task_id=task_id, work_dir=work_dir, progress=PackageProgress())
    package_tasks[task_id] = task
    options = {
        "voice_speed": voice_speed, "voice_id": voice_id, "corrections_json": corrections_json,
        "metadata_json": metadata_json, "narration_plan_json": narration_plan_json,
        "create_navigation_sections_flag": create_navigation_sections_flag, "pages": pages,
    }
    task.runner = asyncio.create_task(_run_package_task(task, filename, content, options))
    return _package_task_status(task)


@app.get("/api/books/package-tasks/{task_id}")
async def book_package_status(task_id: str) -> dict[str, object]:
    return _package_task_status(_package_task(task_id))


@app.post("/api/books/package-tasks/{task_id}/cancel")
async def cancel_book_package(task_id: str) -> dict[str, object]:
    """Stop a download nobody is waiting for any more, so it stops spending."""
    task = _package_task(task_id)
    if task.state == "running" and task.runner:
        task.runner.cancel()
        task.state = "cancelled"
    return _package_task_status(task)


@app.get("/api/books/package-tasks/{task_id}/download")
async def download_book_package(task_id: str) -> FileResponse:
    task = _package_task(task_id)
    if task.state != "completed" or not task.archive_path or not task.archive_path.is_file():
        raise HTTPException(status_code=409, detail="El audiolibro todavía no está listo.")
    return FileResponse(task.archive_path, media_type="application/zip", filename=task.download_name)
