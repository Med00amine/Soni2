import wave

from app.tts.mock import MockTTS


def test_mock_tts_is_deterministic(tmp_path) -> None:
    first = MockTTS().synthesize("Hola mundo.", tmp_path / "first.wav")
    second = MockTTS().synthesize("Hola mundo.", tmp_path / "second.wav")

    assert first.read_bytes() == second.read_bytes()
    with wave.open(str(first)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16_000
        assert wav.getnframes() > 0
