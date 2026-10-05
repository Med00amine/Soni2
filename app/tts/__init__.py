"""Text-to-speech interfaces and test implementations."""

from .base import TTSEngine
from .config import ProductionTTSConfig
from .engine import MeloTTSEngine
from .factory import create_tts_engine
from .mock import MockTTS

__all__ = ["MeloTTSEngine", "MockTTS", "ProductionTTSConfig", "TTSEngine", "create_tts_engine"]
