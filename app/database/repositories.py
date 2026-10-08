"""Transactional catalog repositories and deterministic generation identity."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.ingestion.models import Book

from .models import AudiobookCatalog, BookCatalog, Favorite, ListeningProgress, User, UserLibrary

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

    def list_audiobooks(self, search: str | None = None, language: str | None = None,
                        source_format: str | None = None, voice: str | None = None,
                        page: int = 1, page_size: int = 20) -> tuple[list[AudiobookCatalog], int]:
        with self._session_factory() as session:
            query = select(AudiobookCatalog).join(BookCatalog).where(AudiobookCatalog.status == "ready")
            if search:
                term = f"%{search.strip()}%"
                query = query.where(or_(BookCatalog.title.ilike(term), BookCatalog.author.ilike(term)))
            if language:
                query = query.where(AudiobookCatalog.language == language)
            if source_format:
                query = query.where(BookCatalog.source_format == source_format)
            if voice:
                query = query.where(AudiobookCatalog.voice_id == voice)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            records = list(session.scalars(query.order_by(AudiobookCatalog.created_at).offset((page - 1) * page_size).limit(page_size)))
            return records, total

    def public_recommendations(self, audiobook_id: str | None = None, limit: int = 5) -> list[AudiobookCatalog]:
        with self._session_factory() as session:
            base = select(AudiobookCatalog).join(BookCatalog).where(AudiobookCatalog.status == "ready")
            current = session.get(AudiobookCatalog, audiobook_id) if audiobook_id else None
            if current:
                base = base.where(AudiobookCatalog.id != audiobook_id)
                current_book = session.get(BookCatalog, current.book_id)
                if current_book and current_book.author:
                    base = base.order_by((BookCatalog.author == current_book.author).desc(), AudiobookCatalog.created_at.desc())
                else:
                    base = base.order_by((AudiobookCatalog.language == current.language).desc(), AudiobookCatalog.created_at.desc())
            else:
                base = base.order_by(AudiobookCatalog.created_at.desc())
            return list(session.scalars(base.limit(limit)))

    def get_book(self, book_id: str) -> BookCatalog | None:
        with self._session_factory() as session:
            return session.get(BookCatalog, book_id)

    def get_user(self, user_id: str) -> User | None:
        with self._session_factory() as session:
            return session.get(User, user_id)

    def create_user(self, email: str, password_hash: str, display_name: str) -> User:
        with self._session_factory() as session:
            record = User(id=uuid.uuid4().hex, email=email, password_hash=password_hash, display_name=display_name)
            session.add(record)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("An account with that email already exists.") from exc
            return record

    def authenticate_user(self, email: str, password: str):
        with self._session_factory() as session:
            user = session.scalar(select(User).where(User.email == email))
            return user if user else None

    def library(self, user_id: str, favorites: bool = False):
        with self._session_factory() as session:
            model = Favorite if favorites else UserLibrary
            return list(session.scalars(select(AudiobookCatalog).join(model, model.audiobook_id == AudiobookCatalog.id).where(model.user_id == user_id, AudiobookCatalog.status == "ready").order_by(AudiobookCatalog.created_at.desc())))

    def add_membership(self, user_id: str, audiobook_id: str, favorite: bool = False) -> None:
        with self._session_factory() as session:
            model = Favorite if favorite else UserLibrary
            if not session.get(AudiobookCatalog, audiobook_id):
                raise KeyError("Audiobook was not found.")
            if not session.scalar(select(model).where(model.user_id == user_id, model.audiobook_id == audiobook_id)):
                session.add(model(user_id=user_id, audiobook_id=audiobook_id))
                session.commit()

    def remove_membership(self, user_id: str, audiobook_id: str, favorite: bool = False) -> None:
        with self._session_factory() as session:
            model = Favorite if favorite else UserLibrary
            record = session.scalar(select(model).where(model.user_id == user_id, model.audiobook_id == audiobook_id))
            if record:
                session.delete(record)
                session.commit()

    def get_progress(self, user_id: str, audiobook_id: str) -> ListeningProgress | None:
        with self._session_factory() as session:
            return session.scalar(select(ListeningProgress).where(ListeningProgress.user_id == user_id, ListeningProgress.audiobook_id == audiobook_id))

    def save_progress(self, user_id: str, audiobook_id: str, sentence_id: str | None, position: float, completed: bool) -> ListeningProgress:
        if position < 0:
            raise ValueError("Position must be non-negative.")
        with self._session_factory() as session:
            audiobook = session.get(AudiobookCatalog, audiobook_id)
            if not audiobook or audiobook.status != "ready":
                raise KeyError("Audiobook was not found.")
            record = session.scalar(select(ListeningProgress).where(ListeningProgress.user_id == user_id, ListeningProgress.audiobook_id == audiobook_id))
            if not record:
                record = ListeningProgress(user_id=user_id, audiobook_id=audiobook_id)
                session.add(record)
            record.current_sentence_id, record.position_seconds, record.completed = sentence_id, position, completed
            session.commit()
            return record

    def _update(self, audiobook_id: str, **changes: Any) -> None:
        with self._session_factory() as session:
            record = session.get(AudiobookCatalog, audiobook_id)
            if record is None:
                raise KeyError("Audiobook was not found.")
            for name, value in changes.items():
                setattr(record, name, value)
            session.commit()
