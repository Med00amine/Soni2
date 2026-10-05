"""Engine-independent internal representation of a structured book."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Metadata(BaseModel):
    """Book metadata retained from the source document."""

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    language: str | None = None
    author: str | None = None
    values: dict[str, str] = Field(default_factory=dict)


class Sentence(BaseModel):
    id: str
    text: str
    original_text: str | None = None
    tts_text: str | None = None
    paragraph_id: str
    chapter_id: str
    order: int


class Paragraph(BaseModel):
    id: str
    text: str
    sentences: list[Sentence] = Field(default_factory=list)
    order: int


class Section(BaseModel):
    id: str
    title: str | None = None
    paragraphs: list[Paragraph] = Field(default_factory=list)
    order: int
    sections: list["Section"] = Field(default_factory=list)


class Chapter(BaseModel):
    id: str
    title: str | None = None
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[Paragraph] = Field(default_factory=list)
    order: int


class Book(BaseModel):
    id: str
    title: str | None = None
    language: str | None = None
    author: str | None = None
    metadata: Metadata = Field(default_factory=Metadata)
    chapters: list[Chapter] = Field(default_factory=list)


class AudioSegment(BaseModel):
    sentence_id: str
    file_path: Path
    duration: float
    sample_rate: int
    start_time: float = 0.0
    end_time: float


class AudioMetadata(BaseModel):
    """Measured properties of an audio file."""

    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    rms: float = 0.0
    peak: float = 0.0
    clipping_detected: bool = False


class AudioQualityReport(BaseModel):
    """Deterministic audio quality result."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DaisyValidationReport(BaseModel):
    """Structured DAISY package validation result."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SynchronizationPoint(BaseModel):
    text_id: str
    audio_file: Path
    clip_begin: float
    clip_end: float


Section.model_rebuild()
