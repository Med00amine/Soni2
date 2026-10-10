from __future__ import annotations

import base64
import difflib
import json
import logging
import re
import tempfile
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote
import unicodedata

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.background import BackgroundTask

from app.daisy_export import build_daisy_package, make_paragraphs
from app.book_structure import extract_book_structure
from app.daisy_validation import validate_daisy_package


class Settings(BaseSettings):
    app_env: str = "development"
    tts_provider: str = "deepgram"
    tts_api_key: str = ""
    tts_region: str = ""
    deepgram_api_key: str = ""
    deepgram_base_url: str = "https://api.eu.deepgram.com"
    deepgram_model: str = "aura-2-nestor-es"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    tts_voice: str = "es-ES-ElviraNeural"
    tts_language: str = "es-ES"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_size_mb: int = 200
    preview_characters: int = 5000
    once_intro_template: str = ""
    once_bibliographic_template: str = ""
    once_identification_statement: str = ""
    once_legal_statement: str = ""
    once_intro_filename: str = ""
    once_bibliographic_filename: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soni2")
app = FastAPI(title="Soni2", version="0.1.0", description="Servicio accesible para convertir documentos en audio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ALLOWED = {".pdf", ".txt", ".xml", ".docx", ".epub"}


class PreviewResult(BaseModel):
    audio_base64: str = ""
    audio_mime_type: str = "audio/mpeg"
    transcript: str
    alignment: list[dict[str, float | str]] | None = None
    audio_error: str | None = None
    chapters: list[dict[str, object]] = []
    title: str = ""
    author: str = ""
    edition: str = ""
    publication_year: str = ""
    publisher: str = ""


def extract_text(filename: str, content: bytes) -> str:
    """Compatibility wrapper over the canonical book parser."""
    return parse_book(filename, content).as_text()


def parse_book(filename: str, content: bytes):
    try:
        return extract_book_structure(filename, content)
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Book parsing failed for %s: %s", filename, type(exc).__name__)
        raise ValueError("No se ha podido leer este archivo. Comprueba que no este dañado y que el formato sea compatible.") from exc


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


async def synthesize(text: str, voice_speed: float = 1.0, voice_id: str = "") -> bytes:
    provider = settings.tts_provider.lower()
    # Deepgram rejects very large speak requests (HTTP 413). Keep each request
    # below a conservative text size and split at sentence/word boundaries.
    max_chars = 1800
    if len(text) > max_chars:
        remaining = text.strip()
        chunks: list[str] = []
        while remaining:
            if len(remaining) <= max_chars:
                chunks.append(remaining)
                break
            boundary = max(
                remaining.rfind(mark, 0, max_chars + 1)
                for mark in (". ", "? ", "! ", "\n")
            )
            if boundary < max_chars // 2:
                boundary = remaining.rfind(" ", 0, max_chars + 1)
            if boundary < 1:
                boundary = max_chars
            else:
                # Keep sentence punctuation with the preceding audio segment.
                boundary += 1
            chunks.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()

        audio_parts: list[bytes] = []
        for chunk in chunks:
            if chunk:
                audio_parts.append(await synthesize(chunk, voice_speed, voice_id))
        # MP3 is a frame based stream, so concatenating these encoded segments
        # keeps playback continuous without loading the whole book in memory.
        return b"".join(audio_parts)
    if provider == "deepgram":
        if not settings.deepgram_api_key:
            raise RuntimeError("Deepgram is selected but its API key is missing.")
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.deepgram_base_url.rstrip('/')}/v1/speak",
                params={"model": settings.deepgram_model, "encoding": "mp3"},
                json={"text": text},
                headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            )
        response.raise_for_status()
        if not response.content or not response.headers.get("content-type", "").startswith("audio/"):
            raise RuntimeError("Narration service returned an invalid audio file.")
        return response.content
    if provider == "elevenlabs":
        selected_voice_id = voice_id or settings.elevenlabs_voice_id
        if not settings.elevenlabs_api_key or not selected_voice_id:
            raise RuntimeError("ElevenLabs requires an API key and a voice ID in the server configuration.")
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{quote(selected_voice_id, safe='')}",
                params={"output_format": "mp3_44100_128"},
                json={"text": text, "model_id": settings.elevenlabs_model_id, "voice_settings": {"speed": voice_speed}},
                headers={"xi-api-key": settings.elevenlabs_api_key, "Accept": "audio/mpeg"},
            )
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("ElevenLabs returned an empty audio file.")
        return response.content
    if provider != "azure":
        raise RuntimeError("The configured narration provider is not supported.")
    if not settings.tts_api_key or not settings.tts_region:
        raise RuntimeError("Narration is not configured. Add Azure Speech credentials to .env.")
    endpoint = f"https://{settings.tts_region}.tts.speech.microsoft.com/cognitiveservices/v1"
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    ssml = (
        f"<speak version='1.0' xml:lang='{settings.tts_language}'><voice "
        f"xml:lang='{settings.tts_language}' name='{settings.tts_voice}'>{escaped}</voice></speak>"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            endpoint,
            content=ssml.encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": settings.tts_api_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
                "User-Agent": "Soni2",
            },
        )
    response.raise_for_status()
    if not response.content.startswith(b"ID3") and not response.content.startswith(b"\xff\xfb"):
        raise RuntimeError("Narration service returned an invalid audio file.")
    return response.content


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
    """Return audio plus phrase timings when the selected provider supplies them."""
    if settings.tts_provider.lower() != "elevenlabs":
        return await synthesize(text, voice_speed), None
    selected_voice_id = voice_id or settings.elevenlabs_voice_id
    if not settings.elevenlabs_api_key or not selected_voice_id:
        raise RuntimeError("ElevenLabs requires an API key and a voice ID in the server configuration.")

    audio_parts: list[bytes] = []
    phrases: list[dict[str, float | str]] = []
    time_offset = 0.0
    text_offset = 0
    for chunk in _tts_chunks(text):
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{quote(selected_voice_id, safe='')}/with-timestamps",
                params={"output_format": "mp3_44100_128"},
                json={"text": chunk, "model_id": settings.elevenlabs_model_id, "voice_settings": {"speed": voice_speed}},
                headers={"xi-api-key": settings.elevenlabs_api_key, "Accept": "application/json"},
            )
        response.raise_for_status()
        result = response.json()
        audio_parts.append(base64.b64decode(result["audio_base64"], validate=True))
        alignment = result.get("alignment") or result.get("normalized_alignment")
        if not alignment:
            raise RuntimeError("ElevenLabs did not return text/audio timing data.")
        characters = alignment.get("characters", [])
        starts = alignment.get("character_start_times_seconds", [])
        ends = alignment.get("character_end_times_seconds", [])
        if not characters or len(characters) != len(starts) or len(characters) != len(ends):
            raise RuntimeError("ElevenLabs returned incomplete text/audio timing data.")
        phrases.extend(_phrases_from_alignment(characters, starts, ends, time_offset, text_offset))
        time_offset += max(float(value) for value in ends)
        text_offset += len(chunk)
    return b"".join(audio_parts), phrases


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
    provider = settings.tts_provider.lower()
    if provider == "deepgram":
        return bool(settings.deepgram_api_key)
    if provider == "azure":
        return bool(settings.tts_api_key and settings.tts_region)
    if provider == "elevenlabs":
        return bool(settings.elevenlabs_api_key and (voice_id or settings.elevenlabs_voice_id))
    return False


@app.get("/api/capabilities")
async def capabilities() -> dict[str, object]:
    provider = settings.tts_provider.lower()
    tts_configured = narration_is_configured()
    return {
        "daisy_profile": "DAISY 3 (provisional)",
        "tts_provider": provider,
        "tts_configured": tts_configured,
        "voice": settings.deepgram_model if provider == "deepgram" else settings.tts_voice,
        "elevenlabs_available": provider == "elevenlabs" and bool(settings.elevenlabs_api_key),
        "voice_speed_control": provider == "elevenlabs" and bool(settings.elevenlabs_api_key),
        "voice_id_configurable": provider == "elevenlabs" and bool(settings.elevenlabs_api_key),
        "audio_review_configured": bool(settings.elevenlabs_api_key),
        "chapter_audio": True,
        "chapter_smil": True,
        "full_text_in_package": True,
        "ncx_navigation": True,
        "ncc_navigation": False,
        "once_intro": bool(settings.once_intro_template and settings.once_identification_statement and settings.once_intro_filename),
        "once_bibliographic_audio": bool(settings.once_bibliographic_template and settings.once_legal_statement and settings.once_bibliographic_filename),
        "automatic_audio_review": False,
        "retakes": False,
    }


@app.get("/api/voices")
async def list_elevenlabs_voices() -> dict[str, object]:
    if not settings.elevenlabs_api_key:
        raise HTTPException(status_code=503, detail="Configura ElevenLabs en el servidor para consultar las voces disponibles.")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": settings.elevenlabs_api_key},
            )
        response.raise_for_status()
        data = response.json()
        return {"voices": [
            {"voice_id": voice.get("voice_id", ""), "name": voice.get("name", "Voz"), "labels": voice.get("labels", {})}
            for voice in data.get("voices", []) if voice.get("voice_id")
        ]}
    except Exception as exc:
        logger.warning("ElevenLabs voice listing failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="No se han podido cargar las voces de ElevenLabs.") from exc


def compare_transcripts(source_text: str, spoken_text: str) -> list[dict[str, str]]:
    def words(text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKD", text.casefold())
        normalized = "".join(character for character in normalized if not unicodedata.combining(character))
        return re.findall(r"[\w]+(?:['’][\w]+)?", normalized, flags=re.UNICODE)

    source_words = words(source_text)
    spoken_words = words(spoken_text)
    issues: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(a=source_words, b=spoken_words, autojunk=False)
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag != "equal":
            issues.append({
                "kind": tag,
                "expected": " ".join(source_words[start_a:end_a]),
                "recognized": " ".join(spoken_words[start_b:end_b]),
            })
    return issues


@app.post("/api/audio/review")
async def review_audio(file: UploadFile = File(...), source_text: str = Form(...)) -> dict[str, object]:
    if not settings.elevenlabs_api_key:
        raise HTTPException(status_code=503, detail="Configura ElevenLabs para utilizar la revisión de audio.")
    if not source_text.strip() or len(source_text) > 20000:
        raise HTTPException(status_code=422, detail="El texto de referencia debe tener entre 1 y 20 000 caracteres.")
    audio = await file.read(50 * 1024 * 1024 + 1)
    if len(audio) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El audio de revisión supera el límite de 50 MB.")
    try:
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": settings.elevenlabs_api_key},
                data={"model_id": "scribe_v2", "language_code": "es"},
                files={"file": (file.filename or "preview.mp3", audio, file.content_type or "audio/mpeg")},
            )
        response.raise_for_status()
        spoken_text = response.json().get("text", "")
        if not spoken_text.strip():
            raise HTTPException(status_code=502, detail="Scribe no devolvió una transcripción. Prueba con otra vista previa.")
    except httpx.HTTPStatusError as exc:
        upstream_status = exc.response.status_code
        logger.warning("ElevenLabs Scribe rejected review request with HTTP %s", upstream_status)
        if upstream_status == 401:
            detail = "ElevenLabs Scribe rechazó la clave (401). Revisa ELEVENLABS_API_KEY en .env y reinicia Soni2."
        elif upstream_status == 403:
            detail = "La cuenta o clave de ElevenLabs no tiene acceso a Scribe (403). Comprueba los permisos de Speech to Text."
        elif upstream_status == 429:
            detail = "ElevenLabs Scribe alcanzó un límite o se quedó sin cuota (429). Comprueba el uso de la cuenta."
        elif upstream_status == 400:
            detail = "Scribe rechazó el audio o sus parámetros (400). Genera otra vista previa y vuelve a probar."
        else:
            detail = f"ElevenLabs Scribe devolvió un error (HTTP {upstream_status}). Inténtalo de nuevo más tarde."
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.TimeoutException as exc:
        logger.warning("ElevenLabs Scribe review request timed out")
        raise HTTPException(status_code=504, detail="ElevenLabs Scribe tardó demasiado en revisar el audio. Inténtalo de nuevo.") from exc
    except httpx.RequestError as exc:
        logger.warning("ElevenLabs Scribe review request could not connect: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Soni2 no pudo conectar con ElevenLabs Scribe. Comprueba la conexión e inténtalo de nuevo.") from exc
    issues = compare_transcripts(source_text, spoken_text)
    return {"recognized_text": spoken_text, "issues": issues, "review_note": "Las diferencias son avisos para revisión humana; no confirman por sí solas que haya un error."}


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
    except Exception as exc:
        logger.warning("Segment retake generation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="No se ha podido generar la nueva versión del fragmento.") from exc
    return PreviewResult(audio_base64=base64.b64encode(audio).decode("ascii"), transcript=text, alignment=alignment)


@app.post("/api/books/preview")
async def create_preview(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form("")
) -> PreviewResult:
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    filename = file.filename or "document"
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    try:
        structure = parse_book(filename, content)
        text = clean_text(structure.as_text())
        if len(text) < 20:
            raise ValueError("No hemos encontrado suficiente texto legible en este documento.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    preview_text = text[: settings.preview_characters]
    audio = b""
    alignment = None
    audio_error = None
    if not narration_is_configured(voice_id):
        audio_error = "Texto extraido. Configura ElevenLabs con una clave y una voz para escuchar el audio."
    else:
        try:
            audio, alignment = await synthesize_with_alignment(preview_text, voice_speed, voice_id)
        except Exception:
            logger.exception("Preview narration failed")
            audio_error = "El texto se ha extraido, pero no se pudo generar el audio. Revisa la configuracion de voz."

    return PreviewResult(
        audio_base64=base64.b64encode(audio).decode("ascii"),
        transcript=preview_text,
        alignment=alignment,
        audio_error=audio_error,
        chapters=[
            {"title": chapter.title, "paragraphs": chapter.paragraphs}
            for chapter in structure.chapters
        ],
        title=structure.title,
        author=structure.author,
        edition=structure.edition,
        publication_year=structure.publication_year,
        publisher=structure.publisher,
    )


@app.post("/api/books/package")
async def create_book_package(
    file: UploadFile = File(...), voice_speed: float = Form(1.0), voice_id: str = Form(""),
    corrections_json: str = Form("{}"), metadata_json: str = Form("{}"),
    create_navigation_sections_flag: bool = Form(False, alias="create_navigation_sections"),
) -> FileResponse:
    if not 0.7 <= voice_speed <= 1.2:
        raise HTTPException(status_code=422, detail="La velocidad debe estar entre 0,7 y 1,2.")
    filename = file.filename or "document"
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")

    temp_dir = Path(tempfile.mkdtemp(prefix="soni2-package-"))
    try:
        structure = parse_book(filename, content)
        metadata = book_metadata(structure, metadata_json)
        text = clean_text(structure.as_text())
        if len(text) < 20:
            raise ValueError("No hemos encontrado suficiente texto legible en este documento.")
        source_chapters = apply_paragraph_corrections(
            [(chapter.title, chapter.paragraphs) for chapter in structure.chapters],
            corrections_json,
        )
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
            front_matter.append(("Introducción", intro_text, await synthesize(intro_text, voice_speed, voice_id), settings.once_intro_filename))
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
            front_matter.append(("Datos bibliográficos", bibliographic_text, await synthesize(bibliographic_text, voice_speed, voice_id), settings.once_bibliographic_filename))

        audio_segments: list[bytes] = []
        for paragraph in paragraphs:
            audio_segments.append(await synthesize(paragraph, voice_speed, voice_id))

        package_dir = build_daisy_package(
            metadata["title"] or Path(filename).stem or "Libro",
            paragraphs,
            audio_segments,
            temp_dir / "book",
            chapters=chapter_paragraphs,
            author=metadata["author"],
            edition=metadata["edition"],
            publication_year=metadata["publication_year"],
            publisher=metadata["publisher"],
            front_matter=front_matter,
        )
        validate_daisy_package(package_dir)
        archive_path = temp_dir / "soni2-daisy.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in package_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(package_dir).as_posix())
        safe_name = re.sub(r"[^\w.-]+", "_", Path(filename).stem, flags=re.UNICODE).strip("._") or "libro"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{safe_name}-daisy.zip",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("Full DAISY package generation failed")
        raise HTTPException(
            status_code=503,
            detail="No hemos podido crear el libro completo. Comprueba la conexión de narración e inténtalo de nuevo.",
        ) from exc
