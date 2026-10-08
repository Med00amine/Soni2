"""Lossless WAV inspection without modifying source audio."""

import math
import wave
from pathlib import Path

from app.ingestion.models import AudioMetadata


class AudioProcessor:
    """Read deterministic metadata and basic signal measurements from WAV files."""

    def inspect(self, path: Path) -> AudioMetadata:
        path = Path(path)
        try:
            with wave.open(str(path), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frame_count = audio.getnframes()
                frames = audio.readframes(frame_count)
        except (OSError, EOFError, wave.Error) as exc:
            raise ValueError(f"Unable to read WAV file '{path}': {exc}") from exc

        if sample_width not in {1, 2, 3, 4}:
            raise ValueError(f"Unsupported WAV sample width {sample_width} for '{path}'.")
        values = _decode_samples(frames, sample_width)
        scale = float(2 ** (sample_width * 8 - 1))
        normalized = [value / scale for value in values]
        peak = max((abs(value) for value in normalized), default=0.0)
        rms = math.sqrt(sum(value * value for value in normalized) / len(normalized)) if normalized else 0.0
        return AudioMetadata(
            path=path,
            duration_seconds=frame_count / sample_rate if sample_rate else 0.0,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            frame_count=frame_count,
            rms=rms,
            peak=peak,
            clipping_detected=peak >= 0.999,
        )


def _decode_samples(frames: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [sample - 128 for sample in frames]
    values: list[int] = []
    for offset in range(0, len(frames) - sample_width + 1, sample_width):
        raw = frames[offset : offset + sample_width]
        if sample_width == 3:
            sign = b"\xff" if raw[2] & 0x80 else b"\x00"
            values.append(int.from_bytes(raw + sign, "little", signed=True))
        else:
            values.append(int.from_bytes(raw, "little", signed=True))
    return values
