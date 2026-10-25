"""Check that reported fault times point at the right moment of the finished audio.

    cd backend
    python -m tools.verify_locations path/to/unpacked-zip

For every entry of ``fault-locations.json``, a short window of the complete
audiobook MP3 is cut around ``book_time`` and transcribed (the configured ASR
provider; it costs a little). The location is confirmed when the word the
narration was compared on (the expected or the heard form) is recognized in
that window.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, providers  # noqa: E402
from app.qa_review import phonetic_key  # noqa: E402

BEFORE, AFTER = 1.0, 2.0


def _words(text: str) -> set[str]:
    return {phonetic_key("".join(c for c in word if c.isalpha())) for word in text.split() if any(c.isalpha() for c in word)}


async def check(unpacked: Path) -> dict:
    locations = json.loads((unpacked / "fault-locations.json").read_text(encoding="utf-8"))
    package = unpacked / "DAISY 3"
    ffmpeg = shutil.which("ffmpeg")
    provider = main.active_asr_provider()
    results = []
    with tempfile.TemporaryDirectory(prefix="soni2-locate-") as folder:
        for index, item in enumerate(locations):
            if item.get("book_time") is None:
                continue
            start = max(0.0, item["book_time"] - BEFORE)
            clip = Path(folder) / f"window-{index}.mp3"
            subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t",
                            f"{BEFORE + AFTER:.2f}", "-i", str(package / item["book_file"]), "-c:a", "libmp3lame",
                            "-q:a", "4", str(clip)], check=True)
            heard = await providers.transcribe(clip.read_bytes(), provider=provider,
                                               api_key=main.provider_api_key(provider))
            targets = _words(item["expected"]) | _words(item["heard"])
            found = bool(targets & _words(heard.text))
            results.append({"book_clock": item["book_clock"], "expected": item["expected"], "heard": item["heard"],
                            "window_transcript": heard.text, "confirmed": found})
    return {"locations": len(results), "confirmed": sum(item["confirmed"] for item in results), "results": results}


if __name__ == "__main__":
    report = asyncio.run(check(Path(sys.argv[1])))
    for item in report["results"]:
        mark = "OK " if item["confirmed"] else "?? "
        print(f"{mark}{item['book_clock']:>10}  «{item['expected']}»/«{item['heard']}»  ← {item['window_transcript']}")
    print(f"{report['confirmed']} of {report['locations']} locations confirmed in the complete audiobook")
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
