from __future__ import annotations

import base64
import io
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import ebooklib
import fitz
import httpx
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import epub
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    tts_provider: str = "deepgram"
    tts_api_key: str = ""
    tts_region: str = ""
    deepgram_api_key: str = ""
    deepgram_base_url: str = "https://api.eu.deepgram.com"
    deepgram_model: str = "aura-2-nestor-es"
    tts_voice: str = "es-ES-ElviraNeural"
    tts_language: str = "es-ES"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_size_mb: int = 200
    preview_characters: int = 5000
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
    audio_base64: str
    audio_mime_type: str = "audio/mpeg"
    transcript: str


def extract_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".pdf":
            with fitz.open(stream=content, filetype="pdf") as pdf:
                text = "\n\n".join(page.get_text("text") for page in pdf)
            if len(re.sub(r"\s", "", text)) < 40:
                raise ValueError("Este PDF parece escaneado y no contiene texto legible para extraer.")
            return text
        if suffix == ".txt":
            return content.decode("utf-8-sig")
        if suffix == ".xml":
            root = ET.fromstring(content)
            return "\n".join(part.strip() for part in root.itertext() if part.strip())
        if suffix == ".docx":
            doc = Document(io.BytesIO(content))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if suffix == ".epub":
            try:
                # EbookLib checks the input with os.path.exists(), so it requires
                # a filesystem path rather than a BytesIO object. Keep its
                # temporary source short-lived and remove it immediately after parsing.
                with tempfile.TemporaryDirectory(prefix="soni2-epub-") as temp_dir:
                    epub_path = Path(temp_dir) / "book.epub"
                    epub_path.write_bytes(content)
                    book = epub.read_epub(str(epub_path))
            except Exception as exc:
                logger.warning("EPUB parsing failed for %s: %s", filename, exc)
                raise ValueError(
                    "No hemos podido abrir este EPUB. Puede estar dañado, protegido con DRM o no cumplir el formato EPUB."
                ) from exc
            chapters: list[str] = []
            for item_id, _linear in book.spine:
                item = book.get_item_with_id(item_id)
                if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                    continue
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for unwanted in soup(["script", "style", "nav", "header", "footer"]):
                    unwanted.decompose()
                chapter = soup.get_text("\n", strip=True)
                if chapter:
                    chapters.append(chapter)
            return "\n\n".join(chapters)
    except (ET.ParseError, UnicodeDecodeError, fitz.FileDataError) as exc:
        raise ValueError("No hemos podido leer este documento. Comprueba el archivo e inténtalo de nuevo.") from exc
    raise ValueError("Este tipo de archivo no es compatible.")


def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\ufeff", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"[\t\r\f\v ]+", " ", text)
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line and (not compact or line != compact[-1]):
            compact.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(compact)).strip()


async def synthesize(text: str) -> bytes:
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
                audio_parts.append(await synthesize(chunk))
        # MP3 is a frame based stream, so concatenating these encoded segments
        # keeps playback continuous without loading the whole book in memory.
        return b"".join(audio_parts)
    if settings.tts_provider.lower() == "deepgram":
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
    if settings.tts_provider.lower() != "azure":
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


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/books/preview")
async def create_preview(file: UploadFile = File(...)) -> PreviewResult:
    filename = file.filename or "document"
    if Path(filename).suffix.lower() not in ALLOWED:
        raise HTTPException(status_code=415, detail="Elige un archivo PDF, EPUB, DOCX, XML o TXT.")
    content = await file.read(settings.max_upload_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo supera el tamaño máximo permitido.")
    try:
        text = clean_text(extract_text(filename, content))
        if len(text) < 20:
            raise ValueError("No hemos encontrado suficiente texto legible en este documento.")
        preview_text = text[: settings.preview_characters]
        audio = await synthesize(preview_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Preview generation failed")
        raise HTTPException(status_code=503, detail="El servicio de narración no está disponible temporalmente. Inténtalo de nuevo.") from exc
    return PreviewResult(
        audio_base64=base64.b64encode(audio).decode("ascii"),
        transcript=preview_text,
    )
