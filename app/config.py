"""Application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by CLI and pipeline components."""

    default_language: str = "es"
    data_root: Path = Path("data")
    output_root: Path = Path("data/output")
    model_config = SettingsConfigDict(env_prefix="DAISY_", env_file=".env", extra="ignore")


settings = Settings()

