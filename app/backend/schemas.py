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
    source_format: str = "dtbook"
    source_filename: str | None = None


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
    audiobook_id: str | None = None
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


class AudiobookResponse(BaseModel):
    id: str
    book_id: str
    engine: str
    voice_id: str
    language: str
    model_version: str
    normalization_version: str
    generation_key: str
    status: str
    created_at: datetime


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    created_at: datetime


class AuthRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class CatalogPage(BaseModel):
    items: list[AudiobookResponse]
    page: int
    page_size: int
    total: int


class ProgressRequest(BaseModel):
    current_sentence_id: str | None = None
    position_seconds: float = Field(ge=0)
    completed: bool = False


class ProgressResponse(ProgressRequest):
    audiobook_id: str
    updated_at: datetime


class RecommendationResponse(AudiobookResponse):
    reason: str


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
