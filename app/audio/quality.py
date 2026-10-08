"""Configurable, deterministic audio quality checks."""

from pathlib import Path
from app.ingestion.models import AudioMetadata, AudioQualityReport
from .processor import AudioProcessor


class AudioQuality:
    """Validate measurable properties without applying arbitrary transformations."""

    def __init__(
        self,
        *,
        min_sample_rate: int = 8_000,
        expected_channels: int = 1,
        max_silence_rms: float = 0.0001,
    ) -> None:
        self.min_sample_rate = min_sample_rate
        self.expected_channels = expected_channels
        self.max_silence_rms = max_silence_rms

    def validate(self, metadata: AudioMetadata | None, path: Path | None = None) -> AudioQualityReport:
        errors: list[str] = []
        warnings: list[str] = []
        if metadata is None:
            return AudioQualityReport(valid=False, errors=[f"Missing audio: {path or '<unknown>'}"])
        if metadata.duration_seconds <= 0 or metadata.frame_count == 0:
            errors.append("Audio has zero duration.")
        if metadata.sample_rate < self.min_sample_rate:
            errors.append(f"Sample rate {metadata.sample_rate} is below {self.min_sample_rate} Hz.")
        if metadata.channels != self.expected_channels:
            errors.append(f"Expected {self.expected_channels} channel(s), got {metadata.channels}.")
        if metadata.clipping_detected:
            errors.append("Audio contains clipped samples.")
        if metadata.duration_seconds > 1.0 and metadata.rms <= self.max_silence_rms:
            warnings.append("Audio is suspiciously silent.")
        return AudioQualityReport(valid=not errors, errors=errors, warnings=warnings)

    def validate_file(self, path: Path) -> AudioQualityReport:
        try:
            metadata = AudioProcessor().inspect(path)
        except ValueError:
            return AudioQualityReport(valid=False, errors=[f"Unreadable audio: {path}"])
        return self.validate(metadata, path)
