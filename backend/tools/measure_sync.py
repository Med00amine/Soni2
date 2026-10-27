"""Measure how closely a delivered DAISY package's SMIL clips follow the speech.

    cd backend
    python -m tools.measure_sync "path/to/unpacked/DAISY 3"

Each chapter file is transcribed with word timestamps (the configured ASR
provider, which costs a little), and for every SMIL clip the first spoken word
of its paragraph is located near clipBegin. The offset between where the clip
starts and where that word is actually heard is the synchronization error a
DAISY player's highlighting would show. A clip whose first word cannot be found
nearby is reported, not hidden.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, providers  # noqa: E402
from app.qa_review import phonetic_key  # noqa: E402
from app.spanish_text_normalizer import normalize_for_speech  # noqa: E402

SMIL = "{http://www.w3.org/2001/SMIL20/}"
DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"


def _seconds(clock: str) -> float:
    hours, minutes, seconds = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _first_word(text: str) -> str:
    for word in normalize_for_speech(text).spoken.split():
        cleaned = "".join(character for character in word if character.isalpha())
        if cleaned:
            return cleaned
    return ""


async def main_async(package: Path) -> dict:
    dtbook_path = next(package.glob("*.xml"))
    texts = {node.get("id"): "".join(node.itertext()) for node in ET.parse(dtbook_path).getroot().iter()
             if node.get("id")}
    provider = main.active_asr_provider()
    results = []
    transcripts: dict[Path, list[dict]] = {}
    for smil in sorted((package / "smil").glob("chapter-*.smil")):
        for par in ET.parse(smil).getroot().iter(f"{SMIL}par"):
            text_ref = par.find(f"{SMIL}text").get("src").partition("#")[2]
            audio = par.find(f"{SMIL}audio")
            if audio is None:
                # A silent page marker: a navigation point, nothing is read.
                continue
            audio_path = (smil.parent / audio.get("src")).resolve()
            if audio_path not in transcripts:
                transcript = await providers.transcribe(audio_path.read_bytes(), provider=provider,
                                                        api_key=main.provider_api_key(provider), with_timestamps=True)
                transcripts[audio_path] = transcript.segments
            begin, end = _seconds(audio.get("clipBegin")), _seconds(audio.get("clipEnd"))
            expected = phonetic_key(_first_word(texts.get(text_ref, "")))
            candidates = [word for word in transcripts[audio_path]
                          if abs(float(word["start"]) - begin) <= 1.5
                          and phonetic_key("".join(c for c in str(word["text"]) if c.isalpha())) == expected]
            if candidates:
                word = min(candidates, key=lambda item: abs(float(item["start"]) - begin))
                results.append({"clip": text_ref, "clip_begin": begin, "word": word["text"],
                                "word_start": float(word["start"]), "offset": round(float(word["start"]) - begin, 3),
                                "clip_end": end})
            else:
                results.append({"clip": text_ref, "clip_begin": begin, "word": None, "offset": None})
    offsets = [abs(item["offset"]) for item in results if item["offset"] is not None]
    summary = {
        "clips": len(results), "located": len(offsets), "not_located": [item["clip"] for item in results
                                                                       if item["offset"] is None],
        "median_abs_offset_s": round(statistics.median(offsets), 3) if offsets else None,
        "max_abs_offset_s": round(max(offsets), 3) if offsets else None,
        "within_250ms": sum(offset <= 0.25 for offset in offsets),
    }
    return {"summary": summary, "clips": results}


if __name__ == "__main__":
    report = asyncio.run(main_async(Path(sys.argv[1])))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
