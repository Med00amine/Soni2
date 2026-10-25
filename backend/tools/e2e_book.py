"""Produce one complete book end to end with the providers configured in .env.

    cd backend
    python -m tools.e2e_book tests/corpus/el-faro-de-cabo-gris.xml --out ../e2e-results/faro

The book goes through the same HTTP endpoints and production worker as in
the application: upload, extraction, normalization, narration, integrity
checks, recognition by two recognizers, diagnosis and retakes, assembly,
DAISY validation, completeness and the release gate. Nothing is decided by a
person here: segments the system cannot resolve stay in the review queue and
the book stays NEEDS_REVIEW, which the report shows.

The downloaded ZIP is then opened and validated again from scratch (DTDs and
Soni2's structural check), so the result does not rest on the job's own
records. This calls paid APIs.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import daisy_dtd, main, qa_metrics  # noqa: E402
from app import jobs as job_store  # noqa: E402
from app.audio_cache import AudioCache  # noqa: E402
from app.daisy_export import mp3_duration  # noqa: E402
from app.daisy_validation import validate_daisy_package  # noqa: E402


async def account_characters() -> int | None:
    if not main.settings.elevenlabs_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                                        headers={"xi-api-key": main.settings.elevenlabs_api_key})
        return int(response.json()["character_count"]) if response.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("book")
    parser.add_argument("--out", required=True)
    parser.add_argument("--voice", default="")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    storage = out / "storage"
    main.storage_root = storage
    main.database_path = storage / "soni2-jobs.sqlite3"
    main.audio_cache = AudioCache(storage / "audio-cache")

    async def no_background(job_id: str) -> None:
        return None

    main._start_or_resume_job = no_background  # the worker is run below, in the open
    asyncio.run(main.refresh_voice_profiles())
    client = TestClient(main.app)
    before = asyncio.run(account_characters())
    book = Path(args.book)
    started = time.monotonic()
    response = client.post("/api/books/jobs", data={"voice_id": args.voice},
                           files={"file": (book.name, io.BytesIO(book.read_bytes()), "application/octet-stream")})
    if response.status_code != 202:
        raise SystemExit(f"Job refused: HTTP {response.status_code} {response.text}")
    job_id = response.json()["job_id"]
    print("job", job_id, "units", response.json()["total_sentences"], flush=True)
    asyncio.run(main.run_production_job(job_id))
    elapsed = time.monotonic() - started
    after = asyncio.run(account_characters())

    status = client.get(f"/api/jobs/{job_id}").json()
    release = client.get(f"/api/jobs/{job_id}/release").json()
    queue = client.get(f"/api/jobs/{job_id}/review-queue").json()
    attempts = client.get(f"/api/jobs/{job_id}/attempts").json()["attempts"]
    units = status["sentences"]
    download = client.get(f"/api/jobs/{job_id}/artifacts")
    diagnostic = False
    if download.status_code == 409:
        diagnostic = True
        download = client.get(f"/api/jobs/{job_id}/artifacts", params={"diagnostic": "true"})
    revalidation: dict = {"status": "not_run"}
    audio_seconds = None
    if download.status_code == 200:
        archive = out / ("delivery-DIAGNOSTIC.zip" if diagnostic else "delivery.zip")
        archive.write_bytes(download.content)
        unpacked = out / "unpacked"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpacked)
        package = unpacked / "DAISY 3"
        try:
            validate_daisy_package(package)
            structural = "passed"
        except ValueError as exc:
            structural = f"failed: {exc}"
        revalidation = {"structural": structural, "dtd": daisy_dtd.validate_package(package)}
        chapters = sorted((package / "audio").glob("chapter-*/chapter-*.mp3"))
        audio_seconds = round(sum(mp3_duration(path.read_bytes()) for path in chapters), 1)
    billed = sum(int((attempt.get("provider") or {}).get("characters_billed") or 0) for attempt in attempts)
    generated = sum(1 for attempt in attempts if (attempt.get("provider") or {}).get("cache") == "miss")
    metrics = [unit.get("metrics") for unit in units if unit.get("metrics")]
    secondary_used = sum(1 for attempt in attempts if attempt.get("secondary"))
    report = {
        "book": book.name, "job_id": job_id, "state": status["state"], "release_state": release["state"],
        "blockers": release["blockers"], "reviews": release["reviews"],
        "provider": main.active_tts_provider(), "model": main.tts_model_for(main.active_tts_provider()),
        "voice": status["voice_id"], "asr_primary": main.active_asr_provider(),
        "asr_secondary": main.secondary_asr_provider(),
        "units": len(units),
        "unit_states": status["progress"],
        "verification": release["dimensions"]["content"]["verified_by"],
        "attempts": len(attempts), "generations": generated,
        "quality_retakes": sum(unit["quality_retakes"] for unit in units),
        "technical_attempts": sum(unit["technical_attempts"] for unit in units),
        "secondary_recognitions": secondary_used,
        "characters_billed_by_provider": billed,
        "account_characters_before": before, "account_characters_after": after,
        "asr_primary_aggregate": qa_metrics.aggregate(metrics),
        "review_queue": [{key: item.get(key) for key in ("sentence_id", "chapter_index", "text", "recognized_text",
                                                         "error_category", "severity", "recommended_action",
                                                         "last_error")} for item in queue["items"]],
        "listening_recommended": queue["listening_recommended"],
        "processing_seconds": round(elapsed, 1), "audio_seconds": audio_seconds,
        "download": "diagnostic" if diagnostic else ("released" if download.status_code == 200 else download.status_code),
        "revalidation_from_zip": revalidation,
        "dimensions": release["dimensions"],
    }
    (out / "e2e-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: report[key] for key in ("state", "release_state", "units", "unit_states", "verification",
                                            "attempts", "quality_retakes", "secondary_recognitions",
                                            "characters_billed_by_provider", "processing_seconds", "audio_seconds",
                                            "download", "blockers", "reviews")}
    summary["wer"] = report["asr_primary_aggregate"].get("wer")
    summary["dtd_from_zip"] = revalidation.get("dtd", {}).get("status")
    summary["structural_from_zip"] = revalidation.get("structural")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for item in report["review_queue"]:
        print("REVIEW:", item["error_category"], item["severity"], "|", item["text"][:90], "|",
              (item["recognized_text"] or "")[:90], "|", item["last_error"][:120])
    for item in report["listening_recommended"]:
        print("LISTEN:", item["text"][:80], "|", item["reason"][:140])
    print("job dir:", Path(job_store.get_job(main.database_path, job_id)["source_path"]).parent)


if __name__ == "__main__":
    main_cli()
