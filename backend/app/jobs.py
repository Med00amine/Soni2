"""Durable SQLite state for resumable audiobook production jobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_store(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS production_jobs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                chapters_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                voice_id TEXT NOT NULL,
                voice_speed REAL NOT NULL,
                total_sentences INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                output_path TEXT,
                error TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS production_sentences (
                job_id TEXT NOT NULL REFERENCES production_jobs(job_id) ON DELETE CASCADE,
                sentence_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                chapter_title TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                is_heading INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                synthesis_text TEXT NOT NULL,
                text_start INTEGER NOT NULL,
                text_end INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                technical_attempts INTEGER NOT NULL DEFAULT 0,
                quality_retakes INTEGER NOT NULL DEFAULT 0,
                audio_path TEXT,
                recognized_text TEXT NOT NULL DEFAULT '',
                issues_json TEXT NOT NULL DEFAULT '[]',
                acoustic_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL,
                last_error TEXT NOT NULL DEFAULT '',
                reused_audio INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, sentence_id)
            );
            CREATE TABLE IF NOT EXISTS production_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES production_jobs(job_id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS pronunciation_lexicon (
                term TEXT PRIMARY KEY COLLATE NOCASE,
                pronunciation TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sentence_job_state_idx
                ON production_sentences(job_id, state, chapter_index, sequence);
            """
        )
        # Keep existing Docker volumes usable as columns are added.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(production_sentences)")}
        for name, definition in (
            ("is_heading", "INTEGER NOT NULL DEFAULT 0"),
            ("reused_audio", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE production_sentences ADD COLUMN {name} {definition}")


def create_job(database: Path, job: dict[str, Any], sentences: list[dict[str, Any]]) -> None:
    now = utc_now()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO production_jobs
            (job_id,state,source_name,source_path,source_hash,chapters_json,metadata_json,
             voice_id,voice_speed,total_sentences,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["job_id"], "queued", job["source_name"], job["source_path"], job["source_hash"],
             json.dumps(job["chapters"], ensure_ascii=False), json.dumps(job["metadata"], ensure_ascii=False),
             job["voice_id"], job["voice_speed"], len(sentences), now, now),
        )
        connection.executemany(
            """INSERT INTO production_sentences
            (job_id,sentence_id,chapter_index,chapter_title,sequence,is_heading,text,synthesis_text,
             text_start,text_end,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(job["job_id"], item["sentence_id"], item["chapter_index"], item["chapter_title"],
              item["sequence"], int(item.get("is_heading", False)), item["text"], item["synthesis_text"], item["text_start"],
              item["text_end"], now) for item in sentences],
        )


def get_job(database: Path, job_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM production_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["chapters"] = json.loads(result.pop("chapters_json"))
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["sentences"] = [dict(item) for item in connection.execute(
            "SELECT * FROM production_sentences WHERE job_id=? ORDER BY chapter_index,sequence", (job_id,)
        )]
        for item in result["sentences"]:
            item["issues"] = json.loads(item.pop("issues_json"))
            item["acoustic"] = json.loads(item.pop("acoustic_json"))
        return result


def update_job(database: Path, job_id: str, **values: Any) -> None:
    allowed = {"state", "updated_at", "started_at", "completed_at", "output_path", "error", "cancel_requested"}
    if not values or set(values) - allowed:
        raise ValueError("Invalid production job update fields.")
    values.setdefault("updated_at", utc_now())
    assignments = ",".join(f"{name}=?" for name in values)
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE production_jobs SET {assignments} WHERE job_id=?", (*values.values(), job_id))


def update_sentence(database: Path, job_id: str, sentence_id: str, **values: Any) -> None:
    allowed = {
        "state", "technical_attempts", "quality_retakes", "audio_path", "recognized_text",
        "issues_json", "acoustic_json", "confidence", "last_error", "reused_audio", "updated_at",
    }
    if not values or set(values) - allowed:
        raise ValueError("Invalid sentence update fields.")
    values.setdefault("updated_at", utc_now())
    assignments = ",".join(f"{name}=?" for name in values)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE production_sentences SET {assignments} WHERE job_id=? AND sentence_id=?",
            (*values.values(), job_id, sentence_id),
        )


def find_active_job(database: Path, source_hash: str, voice_id: str, voice_speed: float) -> dict[str, Any] | None:
    """Find a job still running over the same source, voice and speed.

    Guards against a duplicate submission starting a second paid run of a book
    that is already being produced.
    """
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT job_id,state,total_sentences FROM production_jobs
               WHERE source_hash=? AND voice_id=? AND ABS(voice_speed-?)<0.001
                 AND state IN ('queued','running') AND cancel_requested=0
               ORDER BY created_at DESC LIMIT 1""",
            (source_hash, voice_id, voice_speed),
        ).fetchone()
        return dict(row) if row else None


def reset_interrupted_jobs(database: Path) -> list[str]:
    now = utc_now()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE production_sentences SET state='pending',updated_at=? WHERE state='generating'", (now,))
        rows = connection.execute(
            "SELECT job_id FROM production_jobs WHERE state IN ('queued','running','partially_completed') AND cancel_requested=0"
        ).fetchall()
        connection.execute(
            "UPDATE production_jobs SET state='queued',updated_at=? WHERE state IN ('running','partially_completed') AND cancel_requested=0",
            (now,),
        )
        return [row[0] for row in rows]


def stop_interrupted_jobs(database: Path) -> list[str]:
    """Stop the jobs a restart interrupted instead of resuming them unattended.

    Verified audio is kept, and a sentence caught mid-generation goes back to
    pending, so an explicit resume later continues where the job stopped.
    """
    now = utc_now()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE production_sentences SET state='pending',updated_at=? WHERE state='generating'", (now,))
        rows = connection.execute("SELECT job_id FROM production_jobs WHERE state IN ('queued','running')").fetchall()
        connection.execute(
            "UPDATE production_jobs SET state='cancelled',cancel_requested=1,error=?,updated_at=?,completed_at=? "
            "WHERE state IN ('queued','running')",
            ("Detenido al reiniciar el servidor.", now, now),
        )
        return [row[0] for row in rows]


def get_lexicon(database: Path) -> dict[str, str]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT term,pronunciation FROM pronunciation_lexicon ORDER BY length(term) DESC"))


def add_event(database: Path, job_id: str, level: str, message: str, details: dict[str, Any] | None = None) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO production_events(job_id,timestamp,level,message,details_json) VALUES (?,?,?,?,?)",
            (job_id, utc_now(), level, message, json.dumps(details or {}, ensure_ascii=False)),
        )


def get_events(database: Path, job_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        result = []
        for row in connection.execute(
            "SELECT timestamp,level,message,details_json FROM production_events WHERE job_id=? ORDER BY event_id", (job_id,)
        ):
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            result.append(event)
        return result


def replace_lexicon(database: Path, entries: dict[str, str]) -> None:
    normalized = [(term.strip(), pronunciation.strip(), utc_now()) for term, pronunciation in entries.items()]
    if any(not term or not pronunciation for term, pronunciation, _ in normalized):
        raise ValueError("Pronunciation terms and pronunciations must be non-empty.")
    if len(normalized) > 2000 or any(len(term) > 200 or len(pronunciation) > 500 for term, pronunciation, _ in normalized):
        raise ValueError("The pronunciation lexicon exceeds its allowed size.")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM pronunciation_lexicon")
        connection.executemany(
            "INSERT INTO pronunciation_lexicon(term,pronunciation,updated_at) VALUES (?,?,?)", normalized
        )
