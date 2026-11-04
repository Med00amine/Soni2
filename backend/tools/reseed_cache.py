"""Put audio already paid for back in the narration cache, from the jobs that hold it.

A job keeps every paragraph it narrated. If the cache was emptied (to free disk,
say) that audio is still there, but nothing can reuse it, and producing the book
again pays for it a second time. This links each verified paragraph back to the
cache entry it belongs to, with its verdict, so it costs nothing to use again.

Hard links take almost no disk, and no audio is lost: the jobs keep their own
files. Only audio from the active provider and model is taken, and only where the
voice still resolves. Jobs are read newest first, and an entry the cache already
has is left alone unless nobody ever verified it and this audio was: a retake that
failed review must not stand in for the reading a person accepted.

Run it where the application's data is, for example in the backend container:

    docker exec -i -w /app soni2-backend-1 python - --dry-run < backend/tools/reseed_cache.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app import jobs as job_store
from app import main
from app.audio_cache import link_or_copy
from app.providers import ProviderError


def reseed(*, dry_run: bool = False) -> dict[str, int]:
    """Link verified audio into the cache. Returns how much was found and where it went."""
    provider = main.active_tts_provider()
    model = main.tts_model_for(provider)
    counts = {"units": 0, "linked": 0, "replaced": 0, "already_cached": 0, "verdicts": 0, "skipped": 0,
              "characters": 0}
    with sqlite3.connect(main.database_path) as connection:
        job_ids = [row[0] for row in connection.execute("SELECT job_id FROM production_jobs ORDER BY created_at DESC")]
    for job_id in job_ids:
        job = job_store.get_job(main.database_path, job_id)
        if not job:
            continue
        job_dir = Path(job["source_path"]).parent
        attempts: dict[str, dict] = {}
        for attempt in job_store.get_attempts(main.database_path, job_id):
            attempts[attempt["sentence_id"]] = attempt  # ordered, so the last take wins
        for unit in job["sentences"]:
            if unit["state"] != "verified" or not unit["audio_path"]:
                continue
            counts["units"] += 1
            segment = job_dir / unit["audio_path"]
            take = (attempts.get(unit["sentence_id"]) or {}).get("provider") or {}
            if not segment.is_file() or take.get("provider") != provider or take.get("model") != model:
                counts["skipped"] += 1
                continue
            try:
                key = main.narration_cache_key(unit["synthesis_text"], job["voice_speed"], job["voice_id"])
            except ProviderError:
                counts["skipped"] += 1
                continue
            target = main.audio_cache.path_for(key)
            verified_before = target.with_suffix(".qa.json").is_file()
            if target.is_file() and target.stat().st_size:
                if verified_before or not unit.get("verification") or target.read_bytes() == segment.read_bytes():
                    counts["already_cached"] += 1
                else:
                    counts["replaced"] += 1
                    if not dry_run:
                        link_or_copy(segment, target)
            else:
                counts["linked"] += 1
                counts["characters"] += len(unit["synthesis_text"])
                if not dry_run:
                    link_or_copy(segment, target)
            if dry_run or not target.is_file() or unit.get("verification") in {None, ""}:
                continue
            audio = segment.read_bytes()
            spoken = unit.get("spoken_text") or unit["text"]
            if target.read_bytes() == audio and main.audio_cache.get_verdict(key, audio, spoken) is None:
                main._remember_verdict(key, audio, spoken, verification=unit["verification"],
                                       recognized_text=unit.get("recognized_text") or "",
                                       secondary_text=unit.get("secondary_text") or "",
                                       issues=unit.get("issues") or [], confidence=unit.get("confidence"),
                                       metrics=unit.get("metrics") or {}, last_error=unit.get("last_error") or "",
                                       diagnosis=unit.get("diagnosis") or {})
                counts["verdicts"] += 1
    return counts


if __name__ == "__main__":
    print(reseed(dry_run="--dry-run" in sys.argv))
