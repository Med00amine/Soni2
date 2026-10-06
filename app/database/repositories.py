"""Transactional catalog repositories and deterministic generation identity."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ingestion.models import Book

from .models import AudiobookCatalog, BookCatalog

MODEL_VERSION = "melotts-0.1.2"
NORMALIZATION_VERSION = "spanish-normalization-v1"


def generation_key(book: Book, engine: str, voice_id: str | None, settings: dict[str, Any] | None = None) -> str:
    payload = {
        "source_fingerprint": book.content_fingerprint,
        "language": book.language or "es",
        "engine": engine,
        "voice": voice_id or "ES",
        "model_version": MODEL_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "settings": settings or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reservation:
    audiobook: AudiobookCatalog
    created: bool


class CatalogService:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def register_book(self, book: Book) -> BookCatalog:
        if not book.content_fingerprint:
            raise ValueError("Book is missing a content fingerprint.")
        with self._session_factory() as session:
            existing = session.scalar(select(BookCatalog).where(BookCatalog.content_fingerprint == book.content_fingerprint))
            if existing:
                return existing
            record = BookCatalog(
                id=book.id, title=book.title, author=book.author, language=book.language,
                source_format=book.source_format, source_filename=book.source_filename,
                content_fingerprint=book.content_fingerprint,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.scalar(select(BookCatalog).where(BookCatalog.content_fingerprint == book.content_fingerprint))
            return record

    def reserve_audiobook(self, book: Book, engine: str, voice_id: str | None, job_id: str) -> Reservation:
        key = generation_key(book, engine, voice_id)
        with self._session_factory() as session:
            existing = session.scalar(select(AudiobookCatalog).where(AudiobookCatalog.generation_key == key))
            if existing:
                if existing.status == "failed":
                    existing.status = "processing"
                    existing.job_id = job_id
                    session.commit()
                    return Reservation(existing, True)
                return Reservation(existing, False)
            record = AudiobookCatalog(
                id=uuid.uuid4().hex, book_id=book.id, tts_engine=engine,
                voice_id=voice_id or "ES", language=book.language or "es",
                model_version=MODEL_VERSION, normalization_version=NORMALIZATION_VERSION,
                generation_config_hash=key, generation_key=key, status="processing", job_id=job_id,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(AudiobookCatalog).where(AudiobookCatalog.generation_key == key))
                if existing is None:
                    raise
                return Reservation(existing, False)
            return Reservation(record, True)

    def mark_ready(self, audiobook_id: str, storage_location: str) -> None:
        self._update(audiobook_id, status="ready", storage_location=storage_location)

    def mark_failed(self, audiobook_id: str) -> None:
        self._update(audiobook_id, status="failed")

    def get(self, audiobook_id: str) -> AudiobookCatalog | None:
        with self._session_factory() as session:
            return session.get(AudiobookCatalog, audiobook_id)

    def list_audiobooks(self) -> list[AudiobookCatalog]:
        with self._session_factory() as session:
            return list(session.scalars(select(AudiobookCatalog).order_by(AudiobookCatalog.created_at)))

    def _update(self, audiobook_id: str, **changes: Any) -> None:
        with self._session_factory() as session:
            record = session.get(AudiobookCatalog, audiobook_id)
            if record is None:
                raise KeyError("Audiobook was not found.")
            for name, value in changes.items():
                setattr(record, name, value)
            session.commit()
