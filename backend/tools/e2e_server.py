"""Run the application for browser testing, for free, with real audio.

    cd backend
    python -m tools.e2e_server --storage <folder> --port 8011

This is app.main itself, served by uvicorn, with only the narration provider
replaced: the benchmark's fake voice returns real MPEG audio (shaped noise
with pauses, as long as reading the text takes) under the ElevenLabs provider
name, so integrity analysis, mastering, packaging and validation run exactly
as in production, and the mock recognizer reads back the text the audio
carries. Every real network call fails, and no key from .env is used.

Pair it with the Vite dev server:

    cd frontend
    $env:VITE_BACKEND_PROXY_TARGET = "http://127.0.0.1:8011"; npx vite --port 5181 --strictPort

For tools.e2e_buttons: ``--extra-voice`` offers a second Spain-Spanish voice to
choose, and ``--mishear catalejo=sombrero`` makes the voice say «sombrero»
wherever the text says «catalejo», so the recognizer finds a difference and
the phrase waits for a person's decision, as a real misreading would.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from app import main, providers  # noqa: E402
from app.audio_cache import AudioCache  # noqa: E402
from tools.benchmark_regeneration import VOICE, FakeVoice, block_network  # noqa: E402


SECOND_VOICE = "benchvoice0000000001"


def mishearing(synthesize, words: dict[str, str]):
    """The voice says HEARD where the text says WORD, as a voice misreading a word would."""
    patterns = [(re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE), heard) for word, heard in words.items()]

    async def say(text: str, voice, **kwargs) -> bytes:
        for pattern, heard in patterns:
            text = pattern.sub(heard, text)
        return await synthesize(text, voice, **kwargs)

    return say


def configure(storage: Path, latency: float = 0.0, port: int = 8011, *, extra_voice: bool = False,
              mishear: dict[str, str] | None = None) -> FakeVoice:
    block_network()
    storage.mkdir(parents=True, exist_ok=True)
    voice = FakeVoice(storage / "speech-pool", latency)
    providers.synthesize = mishearing(voice.synthesize, mishear) if mishear else voice.synthesize  # type: ignore[assignment]
    providers.reported_concurrency["elevenlabs"] = 5
    main.settings = main.settings.model_copy(update={
        "tts_provider": "elevenlabs", "elevenlabs_api_key": "e2e-offline",
        "elevenlabs_voice_id": f"{VOICE}|es-ES|Voz de prueba",
        "elevenlabs_extra_voice_ids": f"{SECOND_VOICE}|es-ES|Segunda voz de prueba" if extra_voice else "",
        "elevenlabs_model_id": "eleven_v4", "elevenlabs_voice_name": "", "elevenlabs_voice_language": "",
        "elevenlabs_stability": None, "elevenlabs_similarity_boost": None, "elevenlabs_style": None,
        "asr_provider": "mock", "asr_secondary_provider": "none", "deepgram_api_key": "", "fish_audio_api_key": "",
        "require_peninsular_voice": True, "tts_concurrency": 0, "quality_review_enabled": True,
        "once_intro_template": "", "once_bibliographic_template": "",
        "cors_origins": f"http://localhost:{port - 8011 + 5181},http://127.0.0.1:{port - 8011 + 5181}",
    })
    main.storage_root = storage
    main.database_path = storage / "soni2-jobs.sqlite3"
    main.audio_cache = AudioCache(storage / "audio-cache")
    return voice


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", required=True)
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--latency", type=float, default=0.3, help="seconds each fake narration takes")
    parser.add_argument("--extra-voice", action="store_true", help="offer a second voice to choose")
    parser.add_argument("--mishear", action="append", default=[], metavar="WORD=HEARD",
                        help="the voice says HEARD wherever the text says WORD")
    args = parser.parse_args()
    mishear = dict(item.split("=", 1) for item in args.mishear)
    voice = configure(Path(args.storage).resolve(), args.latency, args.port, extra_voice=args.extra_voice,
                      mishear=mishear)
    voice.prepare(longest=40.0)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main_cli()
