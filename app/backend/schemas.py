"""Public API schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BookSummary(BaseModel):
    id: str
    title: str | None = None
    language: str | None = None
    author: str | None = None
    chapter_count: int
    sentence_count: int = 0
    processing_state: str | None = None


class ChapterResponse(BaseModel):
    id: str
    title: str | None = None
    order: int
    paragraph_count: int


class BookResponse(BookSummary):
    metadata: dict[str, str] = Field(default_factory=dict)
    chapters: list[ChapterResponse] = Field(default_factory=list)


class VoiceResponse(BaseModel):
    id: str
    name: str
    engine: str
    language: str


class GenerationRequest(BaseModel):
    voice_id: str | None = None
    engine: Literal["mock", "production"] = "mock"


class CreateJobRequest(GenerationRequest):
    book_id: str


class JobResponse(BaseModel):
    id: str
    book_id: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: float = Field(ge=0, le=1)
    stage: str = "Queued"
    completed: int | None = None
    total: int | None = None
    error: str | None = None
    result: dict[str, str] | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SynchronizedSentence(BaseModel):
    id: str
    chapter_id: str
    chapter_title: str | None = None
    text: str
    audio_file: str | None = None
    start: float | None = None
    end: float | None = None


class SynchronizedTextResponse(BaseModel):
    book_id: str
    sentences: list[SynchronizedSentence]
