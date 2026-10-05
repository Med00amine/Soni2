"""Application services composing the existing parser, TTS, synchronizer and builder."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.audio.processor import AudioProcessor
from app.daisy.builder import DaisyBuilder
from app.daisy.validator import DaisyValidator
from app.ingestion.dtbook import DTBookParser
from app.ingestion.models import AudioMetadata, Book
from app.synchronization.synchronizer import SynchronizationEngine
from app.text.pronunciation import SpanishPronunciationProcessor
from app.text.segmenter import SentenceSegmenter
from app.tts.config import ProductionTTSConfig
from app.tts.factory import create_tts_engine

from .schemas import JobResponse
from .storage import BookStorage


class BackendApplication:
    def __init__(self, storage: BookStorage, *, executor: ThreadPoolExecutor | None = None) -> None:
        self.storage = storage
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="audiobook")
        self.jobs: dict[str, JobResponse] = {}
        self._lock = threading.Lock()

    def upload(self, source: bytes, filename: str) -> Book:
        source_path = self.storage.root / f".upload-{uuid.uuid4().hex}.xml"
        try:
            source_path.write_bytes(source)
            book = DTBookParser().parse(source_path)
        finally:
            source_path.unlink(missing_ok=True)
        book.id = uuid.uuid4().hex
        return self.storage.create(book, source, filename)

    def start_generation(self, book_id: str, engine: str, voice_id: str | None) -> JobResponse:
        self.storage.get(book_id)
        job = JobResponse(
            id=uuid.uuid4().hex, book_id=book_id, status="queued", progress=0,
            stage="Queued",
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self.jobs[job.id] = job
        self.executor.submit(self._run, job.id, engine, voice_id)
        return job

    def job(self, job_id: str) -> JobResponse:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise KeyError("Job was not found.") from exc

    def _run(self, job_id: str, engine: str, voice_id: str | None) -> None:
        job = self.jobs[job_id]
        try:
            self._update(job, status="running", stage="Parsing book", progress=0.05)
            book = self.storage.get(job.book_id)
            sentences = list(_book_sentences(book))
            self._update(job, stage="Normalizing text", progress=0.1, total=len(sentences), completed=0)
            processor, segmenter = SpanishPronunciationProcessor(), SentenceSegmenter()
            for sentence in sentences:
                source = sentence.source_text or sentence.original_text or sentence.text
                sentence.source_text = sentence.original_text = source
                sentence.tts_text = " ".join(segmenter.segment(processor.process(source)))
            tts = create_tts_engine(engine, production_config=ProductionTTSConfig(voice=voice_id or "ES"))
            staging = self.storage.directory(book.id) / ".audio"
            audio: dict[str, AudioMetadata] = {}
            self._update(job, stage="Generating audio", progress=0.15)
            for index, sentence in enumerate(sentences, 1):
                path = staging / f"{sentence.id}.wav"
                tts.synthesize(sentence.tts_text or sentence.text, path, voice_id)
                audio[sentence.id] = AudioProcessor().inspect(path)
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
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            self._update(
                job,
                status="failed",
                stage="Failed",
                error="Unable to generate the audiobook.",
                completed_at=datetime.now(timezone.utc),
            )

    def _update(self, job: JobResponse, **changes: object) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)
            self.jobs[job.id] = job


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
