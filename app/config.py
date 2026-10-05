"""Application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by CLI and pipeline components."""

    default_language: str = "es"
    data_root: Path = Path("data")
    output_root: Path = Path("data/output")
    tts_engine: str = "mock"
    tts_model_path: Path | None = None
    tts_device: str = "auto"
    tts_language: str = "ES"
    tts_voice: str = "ES"
    tts_sample_rate: int | None = None
    tts_output_dir: Path = Path("data/output")
    tts_max_retries: int = 2
    tts_retry_delay: float = 1.0
    tts_timeout: float | None = None
    tts_cache_enabled: bool = True
    tts_force_regenerate: bool = False
    job_max_concurrency: int = 1
    model_config = SettingsConfigDict(env_prefix="DAISY_", env_file=".env", extra="ignore")


settings = Settings()
