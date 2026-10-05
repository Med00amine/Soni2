"""Deterministic, dependency-free TTS substitute for tests and local development."""

import hashlib
import math
import wave
from pathlib import Path

from .base import TTSEngine


class MockTTS(TTSEngine):
    """Generate a deterministic mono WAV whose duration depends on the input text."""

    def __init__(self, sample_rate: int = 16_000) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = sample_rate

    def synthesize(self, text: str, output_path: Path, voice_id: str | None = None) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{voice_id or ''}:{text}".encode("utf-8")).digest()
        duration = 0.25 + (len(text) % 40) * 0.01
        frames = int(self.sample_rate * duration)
        frequency = 220.0 + digest[0]
        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            frames_data = bytearray()
            for index in range(frames):
                sample = int(8000 * math.sin(2 * math.pi * frequency * index / self.sample_rate))
                frames_data.extend(sample.to_bytes(2, byteorder="little", signed=True))
            wav.writeframes(frames_data)
        return output_path

