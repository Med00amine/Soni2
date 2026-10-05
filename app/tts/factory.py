"""TTS engine construction."""

from .base import TTSEngine
from .config import ProductionTTSConfig
from .engine import MeloTTSEngine
from .exceptions import TTSConfigurationError
from .mock import MockTTS


def create_tts_engine(
    name: str,
    *,
    production_config: ProductionTTSConfig | None = None,
) -> TTSEngine:
    normalized = name.strip().lower()
    if normalized == "mock":
        return MockTTS()
    if normalized in {"production", "melo", "melotts"}:
        return MeloTTSEngine(production_config)
    raise TTSConfigurationError(f"Unsupported TTS engine '{name}'.")
