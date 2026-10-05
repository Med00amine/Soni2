"""Small filesystem storage adapter; all paths are confined to the data root."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.ingestion.models import Book


class StorageError(ValueError):
    """Raised for missing or unsafe storage resources."""


class BookStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.books = self.root / "books"
        self.books.mkdir(parents=True, exist_ok=True)

    def create(self, book: Book, source: bytes, filename: str) -> Book:
        book_id = _safe_id(book.id)
        book.id = book_id
        directory = self._directory(book_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "source.xml").write_bytes(source)
        (directory / "filename").write_text(filename, encoding="utf-8")
        self.save_book(book)
        return book

    def save_book(self, book: Book) -> None:
        self._directory(book.id).mkdir(parents=True, exist_ok=True)
        (self._directory(book.id) / "book.json").write_text(
            book.model_dump_json(indent=2), encoding="utf-8"
        )

    def get(self, book_id: str) -> Book:
        path = self._directory(book_id) / "book.json"
        try:
            return Book.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError(f"Book '{book_id}' was not found.") from exc

    def directory(self, book_id: str) -> Path:
        directory = self._directory(book_id)
        if not directory.is_dir():
            raise StorageError(f"Book '{book_id}' was not found.")
        return directory

    def save_json(self, book_id: str, name: str, value: Any) -> None:
        self._safe_child(book_id, name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def read_json(self, book_id: str, name: str) -> Any:
        try:
            return json.loads(self._safe_child(book_id, name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError(f"Resource '{name}' was not found.") from exc

    def _directory(self, book_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", book_id):
            raise StorageError("Invalid book identifier.")
        return self.books / book_id

    def _safe_child(self, book_id: str, name: str) -> Path:
        directory = self.directory(book_id).resolve()
        candidate = (directory / name).resolve()
        if candidate.parent != directory or candidate == directory:
            raise StorageError("Invalid storage resource.")
        return candidate


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "book"
    return normalized[:100]
