"""ElevenLabs narration: the request it receives, its errors, and its voices."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, providers
from app.providers import ProviderError

VOICE = "o0SveC0zgHFuCsEO3vHR"


def _settings(**overrides):
    return main.settings.model_copy(update={
        "tts_provider": "elevenlabs", "elevenlabs_api_key": "el-key", "elevenlabs_voice_id": VOICE,
        "elevenlabs_voice_name": "Gabo", "require_peninsular_voice": False, **overrides,
    })


class _Recorder:
    """A stand-in for httpx.AsyncClient that records the request it is sent."""

    calls: list[dict] = []
    status = 200
    headers = {"maximum-concurrent-requests": "5", "character-cost": "12"}
    body: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url, **kwargs):
        type(self).calls.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        if type(self).status != 200:
            return httpx.Response(type(self).status, json=type(self).body, request=request)
        return httpx.Response(200, content=b"ID3\x04audio", headers=type(self).headers, request=request)


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.calls = []
    _Recorder.status = 200
    _Recorder.body = None
    monkeypatch.setattr(providers.httpx, "AsyncClient", _Recorder)
    return _Recorder


def test_narration_request_carries_the_neighbouring_text_as_context(recorder) -> None:
    voice = providers.elevenlabs_voice(VOICE, "Gabo")
    audio = asyncio.run(providers.synthesize(
        "Buenos días a todos.", voice, api_key="el-key",
        previous_text="Amanecía despacio.", next_text="La casa tenía ventanas.",
    ))
    assert audio.startswith(b"ID3")
    call = recorder.calls[0]
    assert call["url"] == f"{providers.ELEVENLABS_TTS_URL}/{VOICE}"
    assert call["params"] == {"output_format": "mp3_44100_128"}
    assert call["headers"]["xi-api-key"] == "el-key"
    assert call["json"] == {
        "text": "Buenos días a todos.", "model_id": providers.ELEVENLABS_DEFAULT_MODEL,
        "previous_text": "Amanecía despacio.", "next_text": "La casa tenía ventanas.",
    }


def test_normal_speed_keeps_the_voices_own_settings(recorder) -> None:
    voice = providers.elevenlabs_voice(VOICE, "Gabo")
    asyncio.run(providers.synthesize("Hola.", voice, api_key="k"))
    # Sending voice_settings would replace the settings the voice was published with.
    assert "voice_settings" not in recorder.calls[0]["json"]
    asyncio.run(providers.synthesize("Hola.", voice, api_key="k", speed=0.9))
    assert recorder.calls[1]["json"]["voice_settings"] == {"speed": 0.9}


def test_the_plan_concurrency_limit_is_learned_from_the_reply(recorder, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings())
    # Before any reply: the free plan's limit, the safe guess.
    assert main.narration_concurrency() == 2
    asyncio.run(main.synthesize("Hola.", 1.0, VOICE))
    assert providers.reported_concurrency["elevenlabs"] == 5
    # One slot is kept free for a preview or a review click.
    assert main.narration_concurrency() == 4


@pytest.mark.parametrize("status,word,kind,permanent", [
    (401, "quota_exceeded", "credit", True),
    (401, "missing_permissions", "credentials", True),
    (401, "invalid_api_key", "credentials", True),
    (429, "too_many_concurrent_requests", "rate_limit", False),
    (429, "system_busy", "rate_limit", False),
    (400, "voice_not_found", "unsupported_voice", True),
    (402, "payment_required", "credit", True),
])
def test_elevenlabs_errors_say_what_is_really_wrong(recorder, status, word, kind, permanent) -> None:
    recorder.status = status
    recorder.body = {"detail": {"status": word, "message": "You have 23 credits remaining."}}
    with pytest.raises(ProviderError) as error:
        asyncio.run(providers.synthesize("Hola.", providers.elevenlabs_voice(VOICE, "Gabo"), api_key="k"))
    assert error.value.kind == kind
    assert error.value.permanent is permanent
    assert "ElevenLabs" in error.value.message
    # ElevenLabs' own words are kept: they state exact amounts.
    assert "23 credits remaining" in error.value.message


def test_an_empty_balance_is_not_mistaken_for_a_bad_key(recorder) -> None:
    # ElevenLabs uses 401 for both; the old status-only rule called it a key problem.
    recorder.status = 401
    recorder.body = {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}
    with pytest.raises(ProviderError) as error:
        asyncio.run(providers.synthesize("Hola.", providers.elevenlabs_voice(VOICE, "Gabo"), api_key="k"))
    assert error.value.kind == "credit"
    assert "créditos" in error.value.message


def test_configured_voices_come_from_the_settings(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(elevenlabs_extra_voice_ids="extraVoice1, " + VOICE))
    voices = main.tts_voices()
    assert [voice.voice_id for voice in voices] == [VOICE, "extraVoice1"]
    assert voices[0].name == "Gabo"
    assert voices[0].accent_known is False
    assert main.configured_voice_id() == VOICE
    assert main.resolve_narration_voice().voice_id == VOICE


def test_a_voice_whose_accent_is_unknown_is_not_passed_off_as_spain_spanish(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(require_peninsular_voice=True))
    with pytest.raises(ProviderError) as error:
        main.resolve_narration_voice()
    assert error.value.kind == "unsupported_voice"
    assert "ELEVENLABS_VOICE_LANGUAGE" in error.value.message
    # Declared by the operator, it passes.
    monkeypatch.setattr(main, "settings", _settings(require_peninsular_voice=True, elevenlabs_voice_language="es-ES"))
    voice = main.resolve_narration_voice()
    assert voice.is_peninsular and voice.accent == "España"


def test_declaring_the_accent_does_not_void_audio_already_paid_for(monkeypatch) -> None:
    unknown = providers.elevenlabs_voice(VOICE, "Gabo")
    declared = providers.elevenlabs_voice(VOICE, "Gabo", "es-ES")
    assert (providers.cache_fingerprint("elevenlabs", unknown, "eleven_multilingual_v2", 1.0, "Hola.")
            == providers.cache_fingerprint("elevenlabs", declared, "eleven_multilingual_v2", 1.0, "Hola."))


def test_the_page_is_told_the_accent_could_not_be_checked(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings())
    client = TestClient(main.app)
    listing = client.get("/api/voices").json()
    assert listing["provider"] == "elevenlabs"
    assert listing["voices"][0]["name"] == "Gabo"
    assert listing["voices"][0]["accent_verified"] is False
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["tts_configured"] is True
    assert capabilities["voice_name"] == "Gabo"
    assert capabilities["voice_accent_verified"] is False
    assert capabilities["voice_speed_control"] is True


def test_long_text_is_split_with_each_part_given_its_neighbour(recorder, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings())
    long_text = ("Una frase de prueba bastante larga. " * 70).strip()
    asyncio.run(main.synthesize(long_text, 1.0, VOICE, context=("Antes.", "Después.")))
    bodies = [call["json"] for call in recorder.calls]
    assert len(bodies) == 2
    assert bodies[0]["previous_text"] == "Antes." and bodies[0]["next_text"] == bodies[1]["text"]
    assert bodies[1]["previous_text"] == bodies[0]["text"] and bodies[1]["next_text"] == "Después."
