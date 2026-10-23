"""Content-addressed narration cache so unchanged text is never paid for twice.

A cache entry is keyed by everything that can change the audio: provider,
voice, model, speed and the exact synthesis text. Correcting one sentence
therefore changes exactly one key, and re-uploading the same book reuses every
entry. Entries are shared across jobs, previews and package builds because the
key contains no job identifier.

Concurrent requests for the same key wait on one in-flight synthesis instead of
paying for the same audio twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    coalesced: int = 0
    bytes_reused: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "coalesced": self.coalesced,
                "bytes_reused": self.bytes_reused}


@dataclass
class AudioCache:
    """Durable MP3 cache rooted at a directory on persistent storage."""

    root: Path
    stats: CacheStats = field(default_factory=CacheStats)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _users: dict[str, int] = field(default_factory=dict, repr=False)

    def path_for(self, key: str) -> Path:
        # Shard by the first two hex characters to keep directories small.
        return self.root / key[:2] / f"{key}.mp3"

    def get(self, key: str) -> bytes | None:
        path = self.path_for(key)
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return path.read_bytes()

    def contains(self, key: str) -> bool:
        path = self.path_for(key)
        return path.is_file() and path.stat().st_size > 0

    def put(self, key: str, audio: bytes, metadata: dict[str, object] | None = None) -> Path:
        if not audio:
            raise ValueError("Refusing to cache empty narration audio.")
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, audio)
        if metadata is not None:
            write_atomic(path.with_suffix(".json"),
                         json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))
        return path

    async def get_or_create(
        self, key: str, factory: Callable[[], Awaitable[bytes]], metadata: dict[str, object] | None = None,
        *, refresh: bool = False,
    ) -> tuple[bytes, bool]:
        """Return ``(audio, reused)``, synthesizing at most once per key.

        ``refresh`` forces a new generation and replaces the entry. A quality
        retake needs this: returning the cached audio would hand back the exact
        reading that already failed review.
        """
        cached = None if refresh else self.get(key)
        if cached is not None:
            self.stats.hits += 1
            self.stats.bytes_reused += len(cached)
            return cached, True
        lock = self._locks.setdefault(key, asyncio.Lock())
        waiting = lock.locked()
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                # Another caller may have finished while this one waited.
                cached = None if refresh else self.get(key)
                if cached is not None:
                    self.stats.hits += 1
                    self.stats.bytes_reused += len(cached)
                    if waiting:
                        self.stats.coalesced += 1
                    return cached, True
                audio = await factory()
                self.put(key, audio, metadata)
                self.stats.misses += 1
                return audio, False
        finally:
            # Drop the lock once the last caller leaves, so a long-running server
            # does not accumulate one lock per sentence it ever produced.
            remaining = self._users.get(key, 1) - 1
            if remaining <= 0:
                self._users.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._users[key] = remaining

    def usage(self) -> dict[str, int]:
        entries = 0
        total = 0
        if self.root.is_dir():
            for path in self.root.rglob("*.mp3"):
                entries += 1
                total += path.stat().st_size
        return {"entries": entries, "bytes": total}


def write_atomic(path: Path, payload: bytes) -> None:
    """Write via a temporary file in the same directory, then rename.

    ``os.replace`` is atomic on the same filesystem, so an interrupted job
    leaves either the previous file or the complete new one, never a partial
    artifact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_atomic_text(path: Path, text: str) -> None:
    write_atomic(path, text.encode("utf-8"))
