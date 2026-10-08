"""FastAPI HTTP layer for the audiobook backend."""

from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from app.auth import create_access_token, decode_access_token
from app.config import settings
from app.ingestion.parsers import BookParseError
from .application import BackendApplication
from .schemas import (
    AuthRequest, AuthResponse, AudiobookResponse, BookResponse, BookSummary, CatalogPage, ChapterResponse,
    CreateJobRequest, GenerationRequest, JobResponse, ProgressRequest, ProgressResponse, RecommendationResponse,
    UserResponse,
    SynchronizedSentence, SynchronizedTextResponse, VoiceResponse,
)
from .storage import BookStorage, StorageError

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def create_app(application: BackendApplication | None = None) -> FastAPI:
    service = application or BackendApplication(
        BookStorage(settings.data_root), max_concurrency=settings.job_max_concurrency,
        database_url=settings.database_url,
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        service.shutdown()

    app = FastAPI(title="DAISY Audiobook API", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    bearer = HTTPBearer(auto_error=False)

    def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> UserResponse:
        if not credentials:
            raise HTTPException(401, "Authentication required.", headers={"WWW-Authenticate": "Bearer"})
        try:
            return service.user(decode_access_token(credentials.credentials, settings.auth_secret))
        except ValueError as exc:
            raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc

    @app.post("/api/auth/register", response_model=AuthResponse, status_code=201)
    def register(request: AuthRequest):
        try:
            user = service.register_user(request.email, request.password, request.display_name)
            return AuthResponse(access_token=create_access_token(user.id, settings.auth_secret, settings.auth_token_ttl_minutes), user=user)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/auth/login", response_model=AuthResponse)
    def login(request: AuthRequest):
        try:
            user = service.login_user(request.email, request.password)
            return AuthResponse(access_token=create_access_token(user.id, settings.auth_secret, settings.auth_token_ttl_minutes), user=user)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/api/auth/me", response_model=UserResponse)
    def me(user: UserResponse = Depends(current_user)):
        return user
    @app.post("/api/books", response_model=BookResponse, status_code=201)
    async def upload_book(file: UploadFile = File(...)):
        if not file.filename or not file.filename.lower().endswith((".xml", ".dtbook", ".epub", ".html", ".htm", ".pdf")):
            raise HTTPException(415, "Unsupported document format. Supported formats: DTBook XML, EPUB, HTML, PDF.")
        try:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "The DTBook file is too large.")
            return _book_response(service.upload(content, file.filename))
        except BookParseError as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/books", response_model=list[BookSummary])
    def list_books():
        result = []
        for directory in sorted(service.storage.books.iterdir()):
            if directory.is_dir():
                try:
                    result.append(_summary(service.storage.get(directory.name)))
                except StorageError:
                    continue
        return result

    @app.get("/api/books/{book_id}", response_model=BookResponse)
    def get_book(book_id: str):
        return _book_response(_get_book(service, book_id))

    @app.get("/api/audiobooks/{book_id}", response_model=BookResponse)
    def get_audiobook(book_id: str):
        return _book_response(_get_book(service, book_id))

    @app.get("/api/catalog/audiobooks", response_model=CatalogPage)
    def catalog_audiobooks(search: str | None = None, language: str | None = None,
                           source_format: str | None = None, voice: str | None = None,
                           page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
        items, total = service.catalog_page(search=search, language=language, source_format=source_format,
                                            voice=voice, page=page, page_size=page_size)
        return CatalogPage(items=items, page=page, page_size=page_size, total=total)

    @app.get("/api/recommendations", response_model=list[RecommendationResponse])
    def recommendations(audiobook_id: str | None = None):
        return [RecommendationResponse(**item.model_dump(), reason=reason) for item, reason in service.recommendations(audiobook_id)]

    @app.get("/api/library", response_model=list[AudiobookResponse])
    def library(user: UserResponse = Depends(current_user)):
        return service.user_library(user.id)

    @app.post("/api/library/{audiobook_id}", status_code=204)
    def add_library(audiobook_id: str, user: UserResponse = Depends(current_user)):
        try:
            service.membership(user.id, audiobook_id, False, True)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/library/{audiobook_id}", status_code=204)
    def remove_library(audiobook_id: str, user: UserResponse = Depends(current_user)):
        service.membership(user.id, audiobook_id, False, False)

    @app.get("/api/favorites", response_model=list[AudiobookResponse])
    def favorites(user: UserResponse = Depends(current_user)):
        return service.user_library(user.id, True)

    @app.post("/api/favorites/{audiobook_id}", status_code=204)
    def add_favorite(audiobook_id: str, user: UserResponse = Depends(current_user)):
        try:
            service.membership(user.id, audiobook_id, True, True)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/favorites/{audiobook_id}", status_code=204)
    def remove_favorite(audiobook_id: str, user: UserResponse = Depends(current_user)):
        service.membership(user.id, audiobook_id, True, False)

    @app.get("/api/audiobooks/{audiobook_id}/progress", response_model=ProgressResponse | None)
    def get_progress(audiobook_id: str, user: UserResponse = Depends(current_user)):
        return service.progress(user.id, audiobook_id)

    @app.put("/api/audiobooks/{audiobook_id}/progress", response_model=ProgressResponse)
    def put_progress(audiobook_id: str, request: ProgressRequest, user: UserResponse = Depends(current_user)):
        try:
            return service.save_progress(user.id, audiobook_id, request.current_sentence_id, request.position_seconds, request.completed)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/books/{book_id}/chapters", response_model=list[ChapterResponse])
    def chapters(book_id: str):
        return [_chapter_response(c) for c in _get_book(service, book_id).chapters]

    @app.get("/api/audiobooks/{book_id}/chapters", response_model=list[ChapterResponse])
    def audiobook_chapters(book_id: str):
        return [_chapter_response(c) for c in _get_book(service, book_id).chapters]

    @app.get("/api/voices", response_model=list[VoiceResponse])
    def voices():
        return [VoiceResponse(id="ES", name="MeloTTS Spanish", engine="production", language="es")]

    @app.post("/api/books/{book_id}/generate", response_model=JobResponse, status_code=202)
    def generate(book_id: str, request: GenerationRequest = GenerationRequest()):
        try:
            return service.start_generation(book_id, request.engine, request.voice_id)
        except (StorageError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/jobs", response_model=JobResponse, status_code=202)
    def create_job(request: CreateJobRequest):
        try:
            return service.start_generation(request.book_id, request.engine, request.voice_id)
        except (StorageError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/jobs/{job_id}", response_model=JobResponse)
    def job(job_id: str):
        try:
            return service.job(job_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/books/{book_id}/status", response_model=JobResponse)
    def status(book_id: str):
        jobs = [job for job in service.jobs.values() if job.book_id == book_id]
        if not jobs:
            raise HTTPException(404, "No generation job found.")
        return max(jobs, key=lambda item: item.created_at)

    @app.get("/api/books/{book_id}/text", response_model=SynchronizedTextResponse)
    @app.get("/api/audiobooks/{book_id}/text", response_model=SynchronizedTextResponse)
    def synchronized_text(book_id: str):
        book = _get_book(service, book_id)
        try:
            points = {item["text_id"]: item for item in service.storage.read_json(book_id, "synchronization.json")}
        except StorageError:
            points = {}
        chapter_titles = {chapter.id: chapter.title for chapter in book.chapters}
        sentences = [
            SynchronizedSentence(id=s.id, chapter_id=s.chapter_id,
                                 chapter_title=chapter_titles.get(s.chapter_id),
                                 text=s.source_text or s.text,
                                 audio_file=Path(points[s.id]["audio_file"]).name if s.id in points else None,
                                 start=points[s.id]["clip_begin"] if s.id in points else None,
                                 end=points[s.id]["clip_end"] if s.id in points else None)
            for s in _book_sentences(book)
        ]
        return SynchronizedTextResponse(book_id=book_id, sentences=sentences)

    @app.get("/api/books/{book_id}/audio/{filename}")
    @app.get("/api/audiobooks/{book_id}/audio/{filename}")
    def audio(book_id: str, filename: str):
        path = _package_file(service, book_id, "audio", filename)
        if not path.is_file():
            raise HTTPException(404, "Audio file not found.")
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    @app.get("/api/books/{book_id}/download")
    @app.get("/api/audiobooks/{book_id}/daisy")
    def download(book_id: str):
        package = service.storage.directory(book_id) / "package"
        if not package.is_dir():
            raise HTTPException(404, "Audiobook has not been generated.")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in package.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(package).as_posix())
        stream.seek(0)
        return StreamingResponse(stream, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{book_id}.zip"'})

    return app


def _get_book(service, book_id):
    try:
        return service.storage.get(book_id)
    except StorageError as exc:
        raise HTTPException(404, str(exc)) from exc


def _package_file(service, book_id: str, folder: str, filename: str) -> Path:
    try:
        package = service.storage.directory(book_id) / "package"
        candidate = (package / folder / filename).resolve()
        if candidate.parent != (package / folder).resolve():
            raise StorageError("Invalid audio path.")
        return candidate
    except StorageError as exc:
        raise HTTPException(404, str(exc)) from exc


def _summary(book):
    sentence_count = sum(1 for _ in _book_sentences(book))
    return BookSummary(id=book.id, title=book.title, language=book.language, author=book.author,
                       chapter_count=len(book.chapters), sentence_count=sentence_count,
                       source_format=book.source_format, source_filename=book.source_filename)


def _book_response(book):
    return BookResponse(**_summary(book).model_dump(), metadata=book.metadata.values,
                        chapters=[_chapter_response(c) for c in book.chapters])


def _chapter_response(chapter):
    return ChapterResponse(id=chapter.id, title=chapter.title, order=chapter.order,
                           paragraph_count=len(chapter.paragraphs))


def _book_sentences(book):
    for chapter in book.chapters:
        yield from _chapter_sentences(chapter)


def _chapter_sentences(chapter):
    for paragraph in chapter.paragraphs:
        yield from paragraph.sentences
    for section in chapter.sections:
        yield from _section_sentences(section)


def _section_sentences(section):
    for paragraph in section.paragraphs:
        yield from paragraph.sentences
    for nested in section.sections:
        yield from _section_sentences(nested)
