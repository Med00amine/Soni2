from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import main


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"text": "El camino mide nueve metros."}


class _FakeClient:
    request: dict[str, object] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url, **kwargs):
        type(self).request = {"url": url, **kwargs}
        return _FakeResponse()


def test_fish_asr_is_used_for_production_transcription(monkeypatch) -> None:
    settings = main.settings.model_copy(update={
        "tts_provider": "fishaudio",
        "fish_audio_api_key": "test-fish-key",
    })
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)

    transcript, recognizer = asyncio.run(main._transcribe_sentence(b"audio-bytes", "sentence-001"))

    assert transcript == "El camino mide nueve metros."
    assert recognizer == "fish-audio-transcribe-1"
    assert _FakeClient.request["url"] == "https://api.fish.audio/v1/asr"
    assert _FakeClient.request["headers"]["Authorization"] == "Bearer test-fish-key"
    assert _FakeClient.request["headers"]["model"] == "transcribe-1"
    assert _FakeClient.request["data"] == {"language": "es", "ignore_timestamps": "true"}
    assert "audio" in _FakeClient.request["files"]


def test_manual_review_uses_fish_and_spanish_aware_comparison(monkeypatch) -> None:
    settings = main.settings.model_copy(update={
        "tts_provider": "fishaudio",
        "fish_audio_api_key": "test-fish-key",
    })
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)

    response = TestClient(main.app).post(
        "/api/audio/review",
        files={"file": ("sample.mp3", b"audio-bytes", "audio/mpeg")},
        data={"source_text": "El camino mide 9 m."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["recognized_text"] == "El camino mide nueve metros."
    assert payload["issues"] == []
    assert _FakeClient.request["url"] == "https://api.fish.audio/v1/asr"


def test_review_capability_tracks_fish_credentials(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "fishaudio",
        "fish_audio_api_key": "test-fish-key",
    }))

    assert main.audio_review_is_configured() is True
    response = TestClient(main.app).get("/api/capabilities")
    assert response.status_code == 200
    assert response.json()["audio_review_configured"] is True


def test_fish_voice_list_includes_selected_female_option(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "fishaudio",
        "fish_audio_api_key": "test-fish-key",
        "fish_audio_voice_id": "ec0718028a2445fb9be8e8bfb56a0150",
        "fish_audio_extra_voice_ids": "fb9d5855f185451983404deb10788ad4,3f065d660212478a9c697fa965b4d098",
    }))

    response = TestClient(main.app).get("/api/voices")

    assert response.status_code == 200
    payload = response.json()
    voices = payload["voices"]
    assert payload["provider"] == "fishaudio"
    # Spain Spanish is listed first: that is the accent this product narrates in.
    assert [voice["name"] for voice in voices] == ["Carmen", "Isabela", "Rodrigo"]
    assert voices[0]["voice_id"] == "3f065d660212478a9c697fa965b4d098"
    assert voices[0]["language"] == "es-ES"
    assert voices[0]["peninsular"] is True
    assert voices[0]["labels"] == {"language": "Spanish", "accent": "España", "gender": "female"}
    assert [voice["peninsular"] for voice in voices[1:]] == [False, False]


def test_spain_voice_requests_spain_spanish_language(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class AudioResponse:
        content = b"ID3-test-audio"

        def raise_for_status(self) -> None:
            return None

    class AudioClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, *, headers, json):
            captured.update(json)
            return AudioResponse()

    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"fish_audio_api_key": "test-key"}))
    monkeypatch.setattr(main.httpx, "AsyncClient", AudioClient)

    asyncio.run(main._synthesize_fish_audio("Prueba de voz.", voice_id="3f065d660212478a9c697fa965b4d098"))

    assert captured["language"] == "es-ES"


def test_fish_word_timestamps_align_back_to_source_phrases() -> None:
    alignment = main._alignment_from_fish_segments("Hola mundo. Segunda frase.", [
        {"text": "Hola", "start": 0.1, "end": 0.4},
        {"text": "mundo", "start": 0.41, "end": 0.8},
        {"text": "Segunda", "start": 1.0, "end": 1.4},
        {"text": "frase", "start": 1.41, "end": 1.8},
    ])

    assert len(alignment) == 2
    assert alignment[0]["text"] == "Hola mundo"
    assert alignment[0]["start"] == 0.1
    assert alignment[0]["end"] == 0.8
    assert alignment[1]["text"] == "Segunda frase"
    assert alignment[1]["start"] == 1.0
