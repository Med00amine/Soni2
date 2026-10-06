"""Format-agnostic document parser contracts and shared model helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

from .models import Book, Chapter, Metadata, Paragraph, Sentence


class BookParseError(ValueError):
    """Raised when an input document cannot become a readable Book."""


class BookParser(Protocol):
    def parse(self, source: str | Path) -> Book: ...


def content_fingerprint(source: Path) -> str:
    """Return a stable identity for the logical source bytes."""
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_paragraph(text: str, paragraph_id: str, chapter_id: str, order: int) -> Paragraph:
    normalized = " ".join(text.split())
    if not normalized:
        raise BookParseError(f"Readable paragraph '{paragraph_id}' contains no text.")
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]
    sentences = [
        Sentence(
            id=f"{paragraph_id}-sentence-{index}",
            text=part,
            source_text=part,
            original_text=part,
            paragraph_id=paragraph_id,
            chapter_id=chapter_id,
            order=index,
        )
        for index, part in enumerate(parts or [normalized], 1)
    ]
    return Paragraph(id=paragraph_id, text=normalized, sentences=sentences, order=order)


def make_book(
    source: Path,
    *,
    title: str | None,
    author: str | None,
    language: str | None,
    chapters: list[Chapter],
    source_format: str,
    metadata: dict[str, str] | None = None,
) -> Book:
    if not chapters or not any(chapter.paragraphs or chapter.sections for chapter in chapters):
        raise BookParseError("The document contains no extractable readable text.")
    values = metadata or {}
    values.setdefault("source_format", source_format)
    return Book(
        id=source.stem,
        title=title or source.stem,
        author=author,
        language=language,
        source_format=source_format,
        source_filename=source.name,
        content_fingerprint=content_fingerprint(source),
        metadata=Metadata(title=title, author=author, language=language, values=values),
        chapters=chapters,
    )
