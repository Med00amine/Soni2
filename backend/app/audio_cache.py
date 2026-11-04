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
import hashlib
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
                if path.parent.parent.name == "chapters":
                    continue
                entries += 1
                total += path.stat().st_size
        return {"entries": entries, "bytes": total}

    # ------------------------------------------------------------------ #
    # A job keeps its own copy of every audio file it uses. When the bytes
    # are the cache entry's, the copy is a hard link: the same audio is not
    # stored once per job on a disk that has little room to spare.
    # ------------------------------------------------------------------ #

    def store_copy(self, key: str, audio: bytes, destination: Path) -> None:
        """Write ``audio`` at ``destination``, as a link to the cache entry when they are the same bytes."""
        source = self.path_for(key) if key else None
        if source is not None and source.is_file() and source.stat().st_size == len(audio) \
                and source.read_bytes() == audio:
            link_or_copy(source, destination)
            return
        write_atomic(destination, audio)

    # ------------------------------------------------------------------ #
    # Quality verdicts. Listening to a passage costs a transcription (and,
    # on a difference, a second one, which ElevenLabs bills in credits). A
    # passage whose exact audio was already verified against the exact same
    # expected speech is not transcribed again when a book is produced again.
    # ------------------------------------------------------------------ #

    def get_verdict(self, key: str, audio: bytes, spoken: str) -> dict | None:
        path = self.path_for(key).with_suffix(".qa.json")
        if not key or not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or record.get("audio_sha256") != _sha256(audio) \
                or record.get("spoken_sha256") != _sha256(spoken.encode("utf-8")):
            return None
        return record

    def put_verdict(self, key: str, audio: bytes, spoken: str, record: dict) -> None:
        if not key:
            return
        path = self.path_for(key).with_suffix(".qa.json")
        payload = {**record, "audio_sha256": _sha256(audio), "spoken_sha256": _sha256(spoken.encode("utf-8"))}
        write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    # ------------------------------------------------------------------ #
    # Mastered chapters, by what went into them (see main._chapter_content_key).
    # ------------------------------------------------------------------ #

    def _chapter_path(self, key: str) -> Path:
        return self.root / "chapters" / key[:2] / f"{key}.mp3"

    def get_chapter(self, key: str) -> tuple[Path, list[tuple[float, float]], list[list[tuple[float, float]]]] | None:
        path = self._chapter_path(key)
        if not key or not path.is_file() or not path.stat().st_size:
            return None
        try:
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            clips = [(float(begin), float(end)) for begin, end in meta["clips"]]
            pauses = [[(float(at), float(added)) for at, added in unit] for unit in meta["pauses"]]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return path, clips, pauses

    def put_chapter(self, key: str, source: Path, clips: list[tuple[float, float]],
                    pauses: list[list[tuple[float, float]]]) -> None:
        path = self._chapter_path(key)
        link_or_copy(source, path)
        write_atomic(path.with_suffix(".json"), json.dumps(
            {"clips": [list(clip) for clip in clips], "pauses": [[list(item) for item in unit] for unit in pauses]},
        ).encode("utf-8"))

    @staticmethod
    def link_or_copy(source: Path, destination: Path) -> None:
        link_or_copy(source, destination)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def link_or_copy(source: Path, destination: Path) -> None:
    """Make ``destination`` the same file as ``source``: a hard link, or a copy.

    Every writer here replaces files (``os.replace``) rather than changing them
    in place, so two names for one file can never see each other's changes.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.link")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
    except OSError:
        write_atomic(destination, source.read_bytes())
        return
    os.replace(temporary, destination)


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
