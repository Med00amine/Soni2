"""Durable job records and the small replaceable worker queue."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from concurrent.futures import Executor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class JobResult(BaseModel):
    """Safe, relative references to generated resources."""

    package: str
    synchronization: str


class JobRecord(BaseModel):
    id: str
    book_id: str
    engine: str
    voice_id: str | None = None
    status: str = "queued"
    progress: float = Field(0, ge=0, le=1)
    stage: str = "Queued"
    completed: int | None = None
    total: int | None = None
    error: str | None = None
    result: JobResult | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobRepository:
    """Atomic JSON persistence for jobs. A record is the source of truth."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create(self, book_id: str, engine: str, voice_id: str | None) -> JobRecord:
        job = JobRecord(
            id=uuid.uuid4().hex, book_id=book_id, engine=engine, voice_id=voice_id,
            created_at=datetime.now(timezone.utc),
        )
        self.save(job)
        return job

    def get(self, job_id: str) -> JobRecord:
        path = self._path(job_id)
        try:
            return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeyError("Job was not found.") from exc

    def list(self, book_id: str | None = None) -> list[JobRecord]:
        records = []
        for path in self.root.glob("*.json"):
            try:
                record = JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("Ignoring malformed job record %s", path)
                continue
            if book_id is None or record.book_id == book_id:
                records.append(record)
        return sorted(records, key=lambda item: item.created_at)

    def save(self, job: JobRecord) -> JobRecord:
        with self._lock:
            target = self._path(job.id)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, target)
        return job

    def update(self, job_id: str, **changes: object) -> JobRecord:
        with self._lock:
            job = self.get(job_id)
            values = job.model_dump()
            unknown = set(changes) - set(values)
            if unknown:
                raise ValueError(f"Unknown job fields: {', '.join(sorted(unknown))}")
            values.update(changes)
            return self.save(JobRecord.model_validate(values))

    def claim(self, job_id: str) -> JobRecord | None:
        """Atomically claim a queued job; repeated delivery is harmless."""
        with self._lock:
            job = self.get(job_id)
            if job.status != "queued":
                return None
            return self.update(
                job_id,
                status="running",
                stage="Starting",
                started_at=datetime.now(timezone.utc),
            )

    def _path(self, job_id: str) -> Path:
        if not job_id or Path(job_id).name != job_id or not job_id.isalnum():
            raise KeyError("Job was not found.")
        return self.root / f"{job_id}.json"


class JobQueue(Protocol):
    def submit(self, job_id: str) -> None: ...
    def shutdown(self, wait: bool = True) -> None: ...


class InProcessJobQueue:
    """Bounded worker pool, deliberately isolated behind a tiny interface."""

    def __init__(self, handler: Callable[[str], None], max_concurrency: int = 1) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._handler = handler
        self._items: queue.Queue[str | None] = queue.Queue()
        self._threads = [
            threading.Thread(target=self._worker, name=f"audiobook-worker-{i}", daemon=True)
            for i in range(max_concurrency)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, job_id: str) -> None:
        self._items.put(job_id)

    def shutdown(self, wait: bool = True) -> None:
        for _ in self._threads:
            self._items.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def _worker(self) -> None:
        while True:
            job_id = self._items.get()
            try:
                if job_id is None:
                    return
                self._handler(job_id)
            except Exception:
                logger.exception("Unhandled worker failure for job %s", job_id)
            finally:
                self._items.task_done()


class ExecutorJobQueue:
    """Compatibility adapter for callers that already provide an Executor."""

    def __init__(self, executor: Executor, handler: Callable[[str], None]) -> None:
        self._executor, self._handler = executor, handler

    def submit(self, job_id: str) -> None:
        self._executor.submit(self._handler, job_id)

    def shutdown(self, wait: bool = True) -> None:
        shutdown = getattr(self._executor, "shutdown", None)
        if shutdown:
            shutdown(wait=wait)
