"""SQLAlchemy catalog tables; binary audiobook content remains on disk."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BookCatalog(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str | None] = mapped_column(String(32))
    source_format: Mapped[str] = mapped_column(String(32))
    source_filename: Mapped[str | None] = mapped_column(String(500))
    content_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AudiobookCatalog(Base):
    __tablename__ = "audiobooks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), index=True)
    tts_engine: Mapped[str] = mapped_column(String(64))
    voice_id: Mapped[str] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(128))
    normalization_version: Mapped[str] = mapped_column(String(128))
    generation_config_hash: Mapped[str] = mapped_column(String(64))
    generation_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    storage_location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("book_id", "generation_config_hash", name="uq_book_generation_config"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UserLibrary(Base):
    __tablename__ = "user_library"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    audiobook_id: Mapped[str] = mapped_column(ForeignKey("audiobooks.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "audiobook_id", name="uq_user_library"),)


class Favorite(Base):
    __tablename__ = "favorites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    audiobook_id: Mapped[str] = mapped_column(ForeignKey("audiobooks.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "audiobook_id", name="uq_user_favorite"),)


class ListeningProgress(Base):
    __tablename__ = "listening_progress"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    audiobook_id: Mapped[str] = mapped_column(ForeignKey("audiobooks.id"), index=True)
    current_sentence_id: Mapped[str | None] = mapped_column(String(200))
    position_seconds: Mapped[float] = mapped_column(default=0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("user_id", "audiobook_id", name="uq_user_progress"),)
