"""Per-job logging: a readable ``job.log`` plus a machine-readable event stream.

The deliverable the reviewers asked for is a real ``.log`` file a person can
open and follow, so ``job.log`` is plain timestamped text. The JSON Lines
stream stays beside it for tooling, instead of replacing the readable log.

Nothing here ever receives credentials: callers pass provider names and
operation outcomes, never keys or tokens.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

LOG_FILENAME = "job.log"
EVENT_FILENAME = "job-events.jsonl"

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Defence in depth: if a key, token or bearer header ever reaches a log field,
# redact it rather than persist it. Sentence identifiers are 24 hex characters,
# so the hex rule starts at 32 and cannot swallow them.
_SECRET_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"(?:bearer|token)\s+\S+"
    r"|(?:sk|pk|api|key)[-_][A-Za-z0-9_-]{8,}"
    r"|[A-Fa-f0-9]{32,}"
    r")(?![A-Za-z0-9])"
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def redact(value: str) -> str:
    return _SECRET_PATTERN.sub("<redacted>", value)


def _render_value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = redact(" ".join(text.split()))
    if len(text) > 240:
        text = text[:237] + "..."
    return f'"{text}"' if " " in text or "=" in text else text


class JobLogger:
    """Append readable and structured records for one production job."""

    def __init__(self, job_dir: Path, job_id: str, *, book_id: str = "") -> None:
        self.job_dir = Path(job_dir)
        self.job_id = job_id
        self.book_id = book_id
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        operation: str,
        message: str,
        *,
        level: str = "INFO",
        chapter_index: int | None = None,
        sentence_id: str | None = None,
        **details: object,
    ) -> None:
        level = level.upper() if level.upper() in _LEVELS else "INFO"
        timestamp = _timestamp()
        scope = [f"job={self.job_id[:8]}"]
        if self.book_id:
            scope.append(f"book={self.book_id[:12]}")
        if chapter_index is not None:
            scope.append(f"ch={chapter_index + 1:03d}")
        if sentence_id:
            scope.append(f"sentence={sentence_id[:12]}")
        fields = " ".join(f"{name}={_render_value(value)}" for name, value in details.items() if value is not None)
        line = f"{timestamp} {level:<7} [{' '.join(scope)}] {operation}: {redact(message)}"
        if fields:
            line += f" | {fields}"
        with (self.job_dir / LOG_FILENAME).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

        record: dict[str, object] = {
            "timestamp": timestamp, "level": level, "job_id": self.job_id, "operation": operation,
            "message": message,
        }
        if self.book_id:
            record["book_id"] = self.book_id
        if chapter_index is not None:
            record["chapter_index"] = chapter_index
        if sentence_id:
            record["sentence_id"] = sentence_id
        record.update({name: value for name, value in details.items() if value is not None})
        with (self.job_dir / EVENT_FILENAME).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def info(self, operation: str, message: str, **details: object) -> None:
        self.log(operation, message, level="INFO", **details)

    def warning(self, operation: str, message: str, **details: object) -> None:
        self.log(operation, message, level="WARNING", **details)

    def error(self, operation: str, message: str, **details: object) -> None:
        self.log(operation, message, level="ERROR", **details)

    def read_log(self) -> str:
        path = self.job_dir / LOG_FILENAME
        return path.read_text(encoding="utf-8") if path.is_file() else ""
