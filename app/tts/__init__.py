"""Text-to-speech interfaces and test implementations."""

from .base import TTSEngine
from .mock import MockTTS

__all__ = ["MockTTS", "TTSEngine"]

