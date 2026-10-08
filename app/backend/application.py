"""Application services composing the existing parser, TTS, synchronizer and builder."""

from __future__ import annotations

import threading
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.audio.processor import AudioProcessor
from app.daisy.builder import DaisyBuilder
from app.daisy.validator import DaisyValidator
from app.ingestion.detection import parser_for
from app.ingestion.models import AudioMetadata, Book
from app.synchronization.synchronizer import SynchronizationEngine
from app.text.pronunciation import SpanishPronunciationProcessor
from app.text.segmenter import SentenceSegmenter
from app.tts.config import ProductionTTSConfig
from app.tts.factory import create_tts_engine
from app.auth import create_access_token, hash_password, normalize_email, verify_password
from app.database.repositories import CatalogService
from app.database.session import create_session_factory

from .schemas import AudiobookResponse, JobResponse, ProgressResponse, UserResponse
from .jobs import ExecutorJobQueue, InProcessJobQueue, JobQueue, JobRecord, JobRepository, JobResult
from .storage import BookStorage

logger = logging.getLogger(__name__)


class BackendApplication:
    def __init__(self, storage: BookStorage, *, executor: ThreadPoolExecutor | None = None,
                 queue: JobQueue | None = None, max_concurrency: int = 1,
                 database_url: str | None = None) -> None:
        self.storage = storage
        self.catalog = CatalogService(create_session_factory(database_url or "sqlite:///data/vocality.db", storage.root))
        self.job_repository = JobRepository(storage.root)
        self._queue = queue
        self._lock = threading.Lock()
        if self._queue is None:
            self._queue = (ExecutorJobQueue(executor, self._run) if executor else
                           InProcessJobQueue(self._run, max_concurrency))

    def upload(self, source: bytes, filename: str) -> Book:
        safe_filename = Path(filename).name
        source_path = self.storage.root / f".upload-{uuid.uuid4().hex}-{safe_filename}"
        try:
            source_path.write_bytes(source)
            book = parser_for(source_path).parse(source_path)
        finally:
            source_path.unlink(missing_ok=True)
        book.id = book.content_fingerprint or uuid.uuid4().hex
        return self.storage.create(book, source, safe_filename)

    def start_generation(self, book_id: str, engine: str, voice_id: str | None) -> JobResponse:
        book = self.storage.get(book_id)
        self.catalog.register_book(book)
        job_id = uuid.uuid4().hex
        reservation = self.catalog.reserve_audiobook(book, engine, voice_id, job_id)
        if not reservation.created:
            existing = reservation.audiobook
            if existing.job_id:
                try:
                    return self.job(existing.job_id)
                except KeyError:
                    pass
            raise ValueError("Audiobook generation is already in progress.")
        job = self.job_repository.create(
            book_id, engine, voice_id, job_id=job_id, audiobook_id=reservation.audiobook.id
        )
        self._queue.submit(job.id)
        return self._response(job)

    def job(self, job_id: str) -> JobResponse:
        try:
            return self._response(self.job_repository.get(job_id))
        except KeyError:
            raise

    @property
    def jobs(self) -> dict[str, JobResponse]:
        """Compatibility view for the Phase 5 API and existing integrations."""
        return {job.id: self._response(job) for job in self.job_repository.list()}

    def _run(self, job_id: str) -> None:
        claimed = self.job_repository.claim(job_id)
        if claimed is None:
            return
        job = claimed
        try:
            self._update(job, stage="Parsing book", progress=0.05)
            book = self.storage.get(job.book_id)
            sentences = list(_book_sentences(book))
            self._update(job, stage="Normalizing text", progress=0.1, total=len(sentences), completed=0)
            processor, segmenter = SpanishPronunciationProcessor(), SentenceSegmenter()
            for sentence in sentences:
                source = sentence.source_text or sentence.original_text or sentence.text
                sentence.source_text = sentence.original_text = source
                sentence.tts_text = " ".join(segmenter.segment(processor.process(source)))
            tts = create_tts_engine(job.engine, production_config=ProductionTTSConfig(voice=job.voice_id or "ES"))
            staging = self.storage.directory(book.id) / ".audio"
            audio: dict[str, AudioMetadata] = {}
            self._update(job, stage="Generating audio", progress=0.15)
            for index, sentence in enumerate(sentences, 1):
                path = staging / f"{sentence.id}.wav"
                tts.synthesize(sentence.tts_text or sentence.text, path, job.voice_id)
                audio[sentence.id] = AudioProcessor().inspect(path)
                if not job.audiobook_id:
                    raise ValueError("Generation job is missing its audiobook catalog record.")
                self.catalog.mark_ready(job.audiobook_id, f"books/{book.id}/package")
                self._update(
                    job,
                    progress=0.15 + 0.6 * index / max(1, len(sentences)),
                    completed=index,
                )
            self._update(job, stage="Synchronizing", progress=0.8)
            points = SynchronizationEngine().synchronize(sentences, audio)
            package = self.storage.directory(book.id) / "package"
            self._update(job, stage="Building DAISY", progress=0.85)
            DaisyBuilder().build(book, points, audio, package)
            self.storage.save_json(book.id, "synchronization.json", [p.model_dump(mode="json") for p in points])
            self._update(job, stage="Validating", progress=0.95)
            report = DaisyValidator().validate(package)
            if not report.valid:
                raise ValueError("Generated audiobook failed validation.")
            self._update(
                job,
                status="completed",
                stage="Completed",
                progress=1,
                completed=len(sentences),
                result=JobResult(
                    package=f"books/{book.id}/package",
                    synchronization=f"books/{book.id}/synchronization.json",
                ),
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.exception("Audiobook job %s failed", job_id)
            self._update(
                job,
                status="failed",
                stage="Failed",
                error="Unable to generate the audiobook.",
                completed_at=datetime.now(timezone.utc),
            )
            if job.audiobook_id:
                self.catalog.mark_failed(job.audiobook_id)

    def _update(self, job: JobResponse, **changes: object) -> None:
        record = self.job_repository.update(job.id, **changes)
        if isinstance(job, JobResponse):
            job.__dict__.update(self._response(record).__dict__)

    def shutdown(self) -> None:
        self._queue.shutdown()

    def audiobooks(self) -> list[AudiobookResponse]:
        records, _ = self.catalog.list_audiobooks()
        return [self._audiobook_response(item) for item in records]

    def catalog_page(self, **filters):
        records, total = self.catalog.list_audiobooks(**filters)
        return [self._audiobook_response(item) for item in records], total

    def recommendations(self, audiobook_id: str | None = None):
        reason = "Metadata match" if audiobook_id else "Recently added"
        return [(self._audiobook_response(item), reason) for item in self.catalog.public_recommendations(audiobook_id)]

    def register_user(self, email: str, password: str, display_name: str):
        record = self.catalog.create_user(normalize_email(email), hash_password(password), display_name.strip() or "Reader")
        return self._user_response(record)

    def login_user(self, email: str, password: str):
        record = self.catalog.authenticate_user(normalize_email(email), password)
        if not record or not record.is_active or not verify_password(password, record.password_hash):
            raise ValueError("Invalid email or password.")
        return self._user_response(record)

    def user(self, user_id: str):
        record = self.catalog.get_user(user_id)
        if not record or not record.is_active:
            raise ValueError("User was not found.")
        return self._user_response(record)

    def membership(self, user_id: str, audiobook_id: str, favorite: bool, add: bool):
        (self.catalog.add_membership if add else self.catalog.remove_membership)(user_id, audiobook_id, favorite)

    def user_library(self, user_id: str, favorite: bool = False):
        return [self._audiobook_response(item) for item in self.catalog.library(user_id, favorite)]

    def progress(self, user_id: str, audiobook_id: str):
        record = self.catalog.get_progress(user_id, audiobook_id)
        if not record:
            return None
        return ProgressResponse(
            audiobook_id=record.audiobook_id, current_sentence_id=record.current_sentence_id,
            position_seconds=record.position_seconds, completed=record.completed, updated_at=record.updated_at,
        )

    def save_progress(self, user_id: str, audiobook_id: str, sentence_id: str | None, position: float, completed: bool):
        audiobook = self.catalog.get(audiobook_id)
        if not audiobook:
            raise KeyError("Audiobook was not found.")
        if sentence_id and sentence_id not in {sentence.id for sentence in _book_sentences(self.storage.get(audiobook.book_id))}:
            raise ValueError("Sentence does not belong to this audiobook.")
        record = self.catalog.save_progress(user_id, audiobook_id, sentence_id, position, completed)
        return ProgressResponse(
            audiobook_id=record.audiobook_id, current_sentence_id=record.current_sentence_id,
            position_seconds=record.position_seconds, completed=record.completed, updated_at=record.updated_at,
        )

    @staticmethod
    def _response(job: JobRecord) -> JobResponse:
        return JobResponse(**job.model_dump(mode="json"))

    @staticmethod
    def _audiobook_response(item) -> AudiobookResponse:
        return AudiobookResponse(
            id=item.id, book_id=item.book_id, engine=item.tts_engine, voice_id=item.voice_id,
            language=item.language, model_version=item.model_version,
            normalization_version=item.normalization_version, generation_key=item.generation_key,
            status=item.status, created_at=item.created_at,
        )

    @staticmethod
    def _user_response(item) -> UserResponse:
        return UserResponse(id=item.id, email=item.email, display_name=item.display_name, created_at=item.created_at)


def _book_sentences(book: Book):
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
