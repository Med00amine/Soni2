"""Validated production TTS configuration."""

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class ProductionTTSConfig(BaseModel):
    """Settings for a local MeloTTS worker process."""

    model_path: Path | None = None
    device: str = "auto"
    language: str = "ES"
    voice: str = "ES"
    sample_rate: int | None = None
    max_retries: int = Field(default=2, ge=0)
    retry_delay: float = Field(default=1.0, ge=0)
    timeout: float | None = Field(default=None, gt=0)
    cache_enabled: bool = True
    force_regenerate: bool = False

    @field_validator("language", "voice")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("language and voice must not be empty")
        return value.strip()
