from pathlib import Path

import pytest

from app.backend.jobs import JobRepository


def test_job_repository_persists_and_updates_records(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path)
    created = repository.create("book-1", "mock", "ES")

    loaded = repository.get(created.id)
    assert loaded.status == "queued"
    assert loaded.book_id == "book-1"

    updated = repository.update(created.id, status="completed", progress=1, completed=3, total=3)
    assert updated.status == "completed"
    assert repository.get(created.id).completed == 3
    assert repository.list("book-1")[0].id == created.id


def test_job_repository_rejects_missing_and_unsafe_ids(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path)
    with pytest.raises(KeyError):
        repository.get("missing")
    with pytest.raises(KeyError):
        repository.get("../escape")


def test_job_repository_validates_updates(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path)
    created = repository.create("book-1", "mock", "ES")

    with pytest.raises(ValueError):
        repository.update(created.id, progress=2)

    with pytest.raises(ValueError):
        repository.update(created.id, unsupported="value")
