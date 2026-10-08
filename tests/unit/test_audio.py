import wave

from app.audio.processor import AudioProcessor
from app.audio.quality import AudioQuality
from app.tts.mock import MockTTS


def test_audio_processor_reads_wav_metadata(tmp_path) -> None:
    path = MockTTS(sample_rate=8_000).synthesize("Hola", tmp_path / "audio.wav")

    metadata = AudioProcessor().inspect(path)

    assert metadata.duration_seconds > 0
    assert metadata.sample_rate == 8_000
    assert metadata.channels == 1
    assert metadata.frame_count > 0
    assert metadata.peak > 0


def test_audio_quality_reports_missing_and_valid_audio(tmp_path) -> None:
    quality = AudioQuality()

    assert not quality.validate_file(tmp_path / "missing.wav").valid
    path = MockTTS().synthesize("Hola", tmp_path / "valid.wav")
    assert quality.validate_file(path).valid


def test_audio_processor_rejects_invalid_wav(tmp_path) -> None:
    path = tmp_path / "invalid.wav"
    path.write_bytes(b"not wav")

    try:
        AudioProcessor().inspect(path)
    except ValueError as exc:
        assert "Unable to read WAV" in str(exc)
    else:
        raise AssertionError("Expected invalid WAV to fail")
