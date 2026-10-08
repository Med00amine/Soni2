import wave
from pathlib import Path

import pytest

from app.tts.config import ProductionTTSConfig
from app.tts.engine import MeloTTSEngine
from app.tts.exceptions import TTSConfigurationError, TTSSynthesisError


class _SpeakerData:
    spk2id = {"ES": 0}


class _Hps:
    data = _SpeakerData()


class _FakeModel:
    hps = _Hps()

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def tts_to_file(self, text, speaker_id, output_path, speed=1.0):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary synthesis failure")
        with wave.open(output_path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8_000)
            audio.writeframes(b"\x00\x00" * 800)


def test_production_engine_retries_and_emits_wav(tmp_path: Path) -> None:
    model = _FakeModel(failures=1)
    engine = MeloTTSEngine(
        ProductionTTSConfig(max_retries=1, retry_delay=0, device="cpu"),
        model_loader=lambda _: (model, 0),
    )

    output = engine.synthesize("Hola", tmp_path / "sent-1.wav")

    assert output.exists()
    assert model.calls == 2
    assert engine.last_metrics is not None
    assert engine.last_metrics.retries == 1


def test_production_engine_reuses_valid_cache(tmp_path: Path) -> None:
    model = _FakeModel()
    path = tmp_path / "sent-1.wav"
    engine = MeloTTSEngine(
        ProductionTTSConfig(device="cpu"),
        model_loader=lambda _: (model, 0),
    )
    engine.synthesize("Hola", path)
    engine.synthesize("Hola", path)

    assert model.calls == 1


def test_production_engine_reports_exhausted_retries(tmp_path: Path) -> None:
    model = _FakeModel(failures=5)
    engine = MeloTTSEngine(
        ProductionTTSConfig(max_retries=1, retry_delay=0),
        model_loader=lambda _: (model, 0),
    )

    with pytest.raises(TTSSynthesisError, match="sent-1"):
        engine.synthesize("Hola", tmp_path / "sent-1.wav")

    assert model.calls == 2


def test_production_engine_loads_model_once(tmp_path: Path) -> None:
    model = _FakeModel()
    loads = 0

    def loader(_):
        nonlocal loads
        loads += 1
        return model, 0

    engine = MeloTTSEngine(ProductionTTSConfig(), model_loader=loader)
    engine.synthesize("Uno", tmp_path / "one.wav")
    engine.synthesize("Dos", tmp_path / "two.wav")

    assert loads == 1


def test_production_engine_rejects_unknown_speaker(tmp_path: Path) -> None:
    engine = MeloTTSEngine(
        ProductionTTSConfig(voice="UNKNOWN"),
        model_loader=lambda _: (_FakeModel(), 0),
    )

    with pytest.raises(TTSConfigurationError, match="Unknown MeloTTS speaker"):
        engine.synthesize("Hola", tmp_path / "sent-1.wav")
