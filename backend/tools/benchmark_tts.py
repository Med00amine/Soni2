"""Benchmark narration models on identical Spanish passages with real providers.

    cd backend
    python -m tools.benchmark_tts --models eleven_multilingual_v2,eleven_v4,eleven_flash_v2_5 \
        --voice o0SveC0zgHFuCsEO3vHR --out ../benchmark-results

Every passage is narrated by every model with the same voice and the same
seed, then transcribed by two independent recognizers (Deepgram nova-3 and
ElevenLabs Scribe). For each take the tool records: integrity and speaking
rate, WER and S/D/I against the expected spoken text, latency, and characters
billed. The numbers passage is also narrated *without* Soni2's normalization,
to measure whether the normalizer helps or the model's own is better.

This calls paid APIs. Characters billed are read from the provider's reply,
and the account's character counter is read before and after when the key may
read it. Audio is saved so a person can listen: recognizer agreement is not a
pronunciation verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audio_integrity, providers, qa_metrics, qa_review, repair  # noqa: E402
from app.main import settings  # noqa: E402  (reads the repository .env)
from app.spanish_text_normalizer import normalize_for_speech  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "corpus" / "benchmark_es.json"


async def subscription_count(api_key: str) -> int | None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                                        headers={"xi-api-key": api_key})
        return int(response.json()["character_count"]) if response.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError):
        return None


async def transcribe(audio: bytes, provider: str, api_key: str) -> tuple[providers.Transcript, float]:
    started = time.monotonic()
    result = await providers.transcribe(audio, provider=provider, api_key=api_key, language="es")
    return result, time.monotonic() - started


async def run_take(passage: dict, model: str, voice: providers.Voice, variant: str, out: Path) -> dict:
    source = passage["text"]
    spoken = normalize_for_speech(source).spoken
    sent = spoken if variant == "normalized" else source
    meta: dict = {}
    started = time.monotonic()
    try:
        audio = await providers.synthesize(sent, voice, api_key=settings.elevenlabs_api_key, model=model,
                                           seed=12345, meta=meta)
    except providers.ProviderError as exc:
        return {"passage": passage["id"], "model": model, "variant": variant, "error": f"{exc.kind}: {exc.message}"}
    latency = time.monotonic() - started
    name = f"{model}__{passage['id']}__{variant}.mp3"
    (out / name).write_bytes(audio)
    integrity = await asyncio.to_thread(audio_integrity.assess, audio, spoken)
    row = {"passage": passage["id"], "category": passage["category"], "model": model, "variant": variant,
           "audio": name, "latency_seconds": round(latency, 2), "characters_sent": len(sent),
           "characters_billed": meta.get("characters_billed"), "request_id": meta.get("request_id"),
           "integrity": integrity.get("status"), "integrity_reasons": integrity.get("reasons"),
           "duration_seconds": integrity.get("duration_seconds"), "chars_per_second": integrity.get("chars_per_second"),
           "signal": integrity.get("signal"), "recognizers": {}}
    signal = integrity.get("signal") or {}
    speech = (signal.get("duration_seconds", 0) - signal.get("leading_silence_seconds", 0)
              - signal.get("trailing_silence_seconds", 0)) or integrity.get("duration_seconds")
    for provider, key in (("deepgram", settings.deepgram_api_key), ("elevenlabs", settings.elevenlabs_api_key)):
        if not key:
            continue
        try:
            transcript, asr_latency = await transcribe(audio, provider, key)
            text, engine = transcript.text, transcript.engine
        except providers.ProviderError as exc:
            row["recognizers"][provider] = {"error": f"{exc.kind}: {exc.message}"}
            continue
        verdict, issues, confidence, reference, metrics = score(spoken, source, text)
        row["recognizers"][provider] = {
            "engine": engine, "transcript": text, "verdict": verdict, "reference": reference,
            "confidence": confidence, "wer": metrics["wer"], "cer": metrics.get("cer"),
            "substitutions": metrics["substitutions"], "deletions": metrics["deletions"],
            "insertions": metrics["insertions"], "reference_words": metrics["reference_words"],
            "issues": [{key: issue.get(key) for key in ("kind", "expected", "recognized", "category", "reason")}
                       for issue in issues],
            "latency_seconds": round(asr_latency, 2),
            "timing_anomalies": repair.timing_anomalies(transcript.segments, spoken, speech),
        }
    return row


def score(spoken: str, source: str, text: str):
    verdict, issues, confidence, reference = qa_review.evaluate_against(spoken, source, text)
    options = {"reference_is_spoken": True, "context": source} if reference == "spoken" else {}
    metrics = qa_metrics.error_rates(spoken if reference == "spoken" else source, text, **options)
    return verdict, issues, confidence, reference, metrics


def rescore(path: Path) -> None:
    """Re-evaluate stored transcripts with the current comparison, at no cost."""
    data = json.loads(path.read_text(encoding="utf-8"))
    corpus = {passage["id"]: passage["text"] for passage in json.loads(CORPUS.read_text(encoding="utf-8"))["passages"]}
    for row in data["takes"]:
        source = corpus[row["passage"]]
        spoken = normalize_for_speech(source).spoken
        for result in row.get("recognizers", {}).values():
            if "transcript" not in result:
                continue
            verdict, issues, confidence, reference, metrics = score(spoken, source, result["transcript"])
            result.update({"verdict": verdict, "reference": reference, "confidence": confidence,
                           "wer": metrics["wer"], "substitutions": metrics["substitutions"],
                           "deletions": metrics["deletions"], "insertions": metrics["insertions"],
                           "reference_words": metrics["reference_words"],
                           "issues": [{key: issue.get(key) for key in ("kind", "expected", "recognized", "category", "reason")}
                                      for issue in issues]})
    data["summary"] = summarize(data["takes"])
    data["rescored"] = True
    path.with_name("benchmark-rescored.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in data["takes"]:
        print(f"{row['model']:24} {row['passage']:13} {row['variant']:10} "
              + " ".join(f"{name}={result.get('verdict')}({result.get('wer')})" for name, result in row["recognizers"].items()))
        for name, result in row["recognizers"].items():
            for issue in result.get("issues", []):
                print(f"      {name}: {issue['expected']!r} -> {issue['recognized']!r}")


def summarize(rows: list[dict]) -> dict:
    summary: dict = {}
    for row in rows:
        if "error" in row:
            continue
        key = f"{row['model']}/{row['variant']}"
        entry = summary.setdefault(key, {"takes": 0, "latency": [], "billed": 0, "integrity_flags": 0,
                                         "chars_per_second": [], "recognizers": {}})
        entry["takes"] += 1
        entry["latency"].append(row["latency_seconds"])
        entry["billed"] += int(row.get("characters_billed") or 0)
        entry["integrity_flags"] += int(row["integrity"] != "passed")
        if row.get("chars_per_second"):
            entry["chars_per_second"].append(row["chars_per_second"])
        for provider, result in row["recognizers"].items():
            if "error" in result:
                continue
            agg = entry["recognizers"].setdefault(provider, [])
            agg.append(result)
    for entry in summary.values():
        entry["mean_latency"] = round(sum(entry["latency"]) / len(entry["latency"]), 2)
        rates = entry.pop("chars_per_second")
        entry["chars_per_second_range"] = [min(rates), max(rates)] if rates else None
        del entry["latency"]
        for provider, results in list(entry["recognizers"].items()):
            totals = qa_metrics.aggregate(results)
            totals["verified"] = sum(1 for result in results if result["verdict"] == "verified")
            entry["recognizers"][provider] = totals
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="eleven_multilingual_v2,eleven_v4,eleven_flash_v2_5")
    parser.add_argument("--voice", default="o0SveC0zgHFuCsEO3vHR")
    parser.add_argument("--out", default="benchmark-results")
    parser.add_argument("--passages", default="", help="comma-separated passage ids (default: all)")
    parser.add_argument("--rescore", default="", help="re-evaluate a stored benchmark.json without new calls")
    args = parser.parse_args()
    if args.rescore:
        rescore(Path(args.rescore))
        return
    if not settings.elevenlabs_api_key:
        raise SystemExit("ELEVENLABS_API_KEY is not configured.")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))["passages"]
    if args.passages:
        wanted = set(args.passages.split(","))
        corpus = [passage for passage in corpus if passage["id"] in wanted]
    before = await subscription_count(settings.elevenlabs_api_key)
    rows: list[dict] = []
    for model in args.models.split(","):
        voice = providers.elevenlabs_voice(args.voice, "benchmark", "es-ES", model)
        for passage in corpus:
            variants = ["normalized", "raw"] if passage["category"] == "numbers_dates_abbreviations" else ["normalized"]
            for variant in variants:
                row = await run_take(passage, model, voice, variant, out)
                rows.append(row)
                recognizers = {name: result.get("verdict", result.get("error")) for name, result in
                               row.get("recognizers", {}).items()}
                print(f"{model:24} {passage['id']:13} {variant:10} {row.get('integrity', row.get('error'))} "
                      f"{row.get('chars_per_second')} c/s {recognizers}", flush=True)
    after = await subscription_count(settings.elevenlabs_api_key)
    result = {"voice": args.voice, "seed": 12345, "models": args.models.split(","),
              "account_characters_before": before, "account_characters_after": after,
              "account_characters_used": (after - before) if before is not None and after is not None else None,
              "summary": summarize(rows), "takes": rows}
    (out / "benchmark.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print("account characters used:", result["account_characters_used"])


if __name__ == "__main__":
    asyncio.run(main())
