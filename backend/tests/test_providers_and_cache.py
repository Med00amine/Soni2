"""Provider selection, Spain-Spanish validation, error classification and caching."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import main, providers
from app.audio_cache import AudioCache, write_atomic
from app.providers import ProviderError

SPAIN_VOICE = "3f065d660212478a9c697fa965b4d098"   # Fish Audio · Carmen · es-ES
MEXICO_VOICE = "ec0718028a2445fb9be8e8bfb56a0150"  # Fish Audio · Rodrigo · es-MX


def _settings(**overrides):
    return main.settings.model_copy(update=overrides)


# --------------------------------------------------------------------------- #
# Separation of narration and speech review
# --------------------------------------------------------------------------- #

def test_tts_and_asr_providers_are_configured_independently(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="fishaudio", asr_provider="deepgram",
        fish_audio_api_key="fish-key", deepgram_api_key="dg-key",
    ))
    assert main.active_tts_provider() == "fishaudio"
    assert main.active_asr_provider() == "deepgram"
    assert main.audio_review_is_configured() is True


def test_auto_asr_falls_back_to_whichever_account_has_credentials(monkeypatch) -> None:
    # Fish credit exhausted and its key removed: review keeps working on Deepgram.
    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="fishaudio", asr_provider="auto", fish_audio_api_key="", deepgram_api_key="dg-key",
    ))
    assert main.active_asr_provider() == "deepgram"

    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="fishaudio", asr_provider="auto", fish_audio_api_key="fish-key", deepgram_api_key="",
    ))
    assert main.active_asr_provider() == "fishaudio"

    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="fishaudio", asr_provider="auto", fish_audio_api_key="", deepgram_api_key="",
    ))
    assert main.active_asr_provider() == "none"
    assert main.audio_review_is_configured() is False


# --------------------------------------------------------------------------- #
# Spain Spanish voice policy
# --------------------------------------------------------------------------- #

def test_default_voice_is_spain_spanish_for_every_provider() -> None:
    for provider in ("fishaudio", "deepgram", "mock"):
        voice = providers.default_voice(provider)
        assert voice is not None and voice.is_peninsular, provider
        assert voice.language == "es-ES"


def test_latin_american_voice_is_rejected_before_generation(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="fishaudio", fish_audio_api_key="fish-key", require_peninsular_voice=True,
    ))
    with pytest.raises(ProviderError) as error:
        main.resolve_narration_voice(MEXICO_VOICE)
    assert error.value.kind == "unsupported_voice"
    assert error.value.permanent is True
    assert "España" in error.value.message
    # The same voice is accepted when the deployment does not require Spain Spanish.
    assert main.resolve_narration_voice(MEXICO_VOICE, enforce_accent=False).voice_id == MEXICO_VOICE


def test_unknown_voice_is_rejected_rather_than_silently_substituted(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(tts_provider="fishaudio", fish_audio_api_key="fish-key"))
    with pytest.raises(ProviderError) as error:
        main.resolve_narration_voice("not-a-real-voice")
    assert error.value.kind == "unsupported_voice"


def test_spain_voice_requests_its_own_language_not_a_global_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        content = b"ID3-audio"

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, *, headers, json):
            captured.update({"url": url, **json})
            return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    voice = providers.find_voice("fishaudio", SPAIN_VOICE)
    asyncio.run(providers.synthesize("Prueba.", voice, api_key="k", model="drama-3"))
    assert captured["language"] == "es-ES"
    assert captured["reference_id"] == SPAIN_VOICE


def test_deepgram_narration_uses_the_speak_endpoint_and_voice_model(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        content = b"ID3-audio"

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, *, params, headers, json):
            captured.update({"url": url, "params": params, "headers": headers, "json": json})
            return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    voice = providers.find_voice("deepgram", "aura-2-diana-es")
    assert voice is not None and voice.is_peninsular
    asyncio.run(providers.synthesize("Hola.", voice, api_key="dg-key"))
    assert captured["url"] == providers.DEEPGRAM_TTS_URL
    assert captured["params"] == {"model": "aura-2-diana-es", "encoding": "mp3"}
    assert captured["headers"]["Authorization"] == "Token dg-key"
    assert captured["json"] == {"text": "Hola."}


def test_deepgram_transcription_reads_transcript_and_word_timings(monkeypatch) -> None:
    payload = {"results": {"channels": [{"alternatives": [{
        "transcript": "El camino mide nueve metros.",
        "words": [{"word": "el", "punctuated_word": "El", "start": 0.1, "end": 0.3},
                  {"word": "camino", "punctuated_word": "camino", "start": 0.3, "end": 0.8}],
    }]}]}}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, *, params, headers, content):
            captured.update({"url": url, "params": params, "headers": headers})
            return Response()

    captured: dict[str, object] = {}
    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    result = asyncio.run(providers.transcribe(
        b"audio", provider="deepgram", api_key="dg-key", with_timestamps=True,
    ))
    assert captured["url"] == providers.DEEPGRAM_ASR_URL
    assert captured["params"]["language"] == "es"
    assert captured["headers"]["Authorization"] == "Token dg-key"
    assert result.text == "El camino mide nueve metros."
    assert result.engine == "deepgram-nova-3"
    assert result.segments == [{"text": "El", "start": 0.1, "end": 0.3},
                               {"text": "camino", "start": 0.3, "end": 0.8}]


# --------------------------------------------------------------------------- #
# Error messages the user can act on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status,kind,permanent", [
    (401, "credentials", True),
    (403, "credentials", True),
    (402, "credit", True),
    (429, "rate_limit", False),
    (404, "unsupported_voice", True),
    (413, "bad_audio", True),
    (400, "bad_audio", True),
    (500, "server", False),
    (503, "server", False),
])
def test_provider_http_errors_are_classified_for_the_user(status, kind, permanent) -> None:
    request = httpx.Request("POST", "https://provider.invalid")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    classified = providers.classify_http_error("fishaudio", error, operation="tts")
    assert classified.kind == kind
    assert classified.permanent is permanent
    assert classified.status_code == status
    assert classified.message and "Fish Audio" in classified.message


def test_exhausted_credit_is_reported_as_a_permanent_credit_problem() -> None:
    request = httpx.Request("POST", "https://provider.invalid")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(402, request=request))
    classified = providers.classify_http_error("fishaudio", error, operation="tts")
    # Retrying an empty balance only wastes attempts, so it must not be transient.
    assert classified.permanent is True
    assert "saldo" in classified.message
    assert main._permanent_provider_failure(classified) is True


def test_rate_limit_is_retried_but_credit_exhaustion_is_not() -> None:
    assert main._permanent_provider_failure(
        ProviderError("rate_limit", "x", permanent=False, status_code=429)) is False
    assert main._permanent_provider_failure(
        ProviderError("credit", "x", permanent=True, status_code=402)) is True


def test_provider_errors_map_onto_useful_http_statuses() -> None:
    assert main._provider_http_status(ProviderError("credit", "x", permanent=True)) == 503
    assert main._provider_http_status(ProviderError("rate_limit", "x", permanent=False)) == 429
    assert main._provider_http_status(ProviderError("unsupported_voice", "x", permanent=True)) == 422
    assert main._provider_http_status(ProviderError("timeout", "x", permanent=False)) == 504
    assert main._provider_http_status(ProviderError("server", "x", permanent=False)) == 502


# --------------------------------------------------------------------------- #
# Offline mock provider
# --------------------------------------------------------------------------- #

def test_mock_provider_round_trips_text_through_valid_mp3_audio() -> None:
    audio = providers.mock_audio("La casa tiene 147 ventanas.")
    assert providers.is_mp3(audio)
    assert providers.mock_transcript(audio) == "La casa tiene 147 ventanas."
    transcript = asyncio.run(providers.transcribe(audio, provider="mock"))
    assert transcript.text == "La casa tiene 147 ventanas."
    assert transcript.engine == "mock-recognizer"


def test_mock_provider_needs_no_credentials(monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", _settings(
        tts_provider="mock", asr_provider="auto", fish_audio_api_key="", deepgram_api_key="",
    ))
    assert main.narration_is_configured() is True
    assert main.active_asr_provider() == "mock"
    assert main.audio_review_is_configured() is True


# --------------------------------------------------------------------------- #
# Cache identity: what must and must not invalidate audio
# --------------------------------------------------------------------------- #

def _key(provider: str, voice_id: str, model: str, speed: float, text: str) -> str:
    voice = providers.find_voice(provider, voice_id)
    return providers.cache_fingerprint(provider, voice, model, speed, text)


def test_identical_requests_share_one_cache_key() -> None:
    assert _key("fishaudio", SPAIN_VOICE, "drama-3", 1.0, "Hola.") == \
           _key("fishaudio", SPAIN_VOICE, "drama-3", 1.0, "Hola.")


def test_changing_text_voice_model_or_speed_changes_the_cache_key() -> None:
    base = _key("fishaudio", SPAIN_VOICE, "drama-3", 1.0, "Hola.")
    assert base != _key("fishaudio", SPAIN_VOICE, "drama-3", 1.0, "Hola!")
    assert base != _key("fishaudio", MEXICO_VOICE, "drama-3", 1.0, "Hola.")
    assert base != _key("fishaudio", SPAIN_VOICE, "speech-1.6", 1.0, "Hola.")
    assert base != _key("fishaudio", SPAIN_VOICE, "drama-3", 1.1, "Hola.")
    assert base != _key("deepgram", "aura-2-diana-es", "", 1.0, "Hola.")


def test_speed_is_ignored_for_providers_without_a_speed_control() -> None:
    # Deepgram exposes no speed parameter, so speed cannot change its audio and
    # must not fragment its cache.
    assert _key("deepgram", "aura-2-diana-es", "", 1.0, "Hola.") == \
           _key("deepgram", "aura-2-diana-es", "", 1.2, "Hola.")


def test_cache_reuses_audio_and_only_pays_once(tmp_path) -> None:
    cache = AudioCache(tmp_path / "cache")
    calls = 0

    async def factory() -> bytes:
        nonlocal calls
        calls += 1
        return b"ID3audio"

    async def scenario():
        first = await cache.get_or_create("key-a", factory)
        second = await cache.get_or_create("key-a", factory)
        third = await cache.get_or_create("key-b", factory)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first == (b"ID3audio", False)
    assert second == (b"ID3audio", True)
    assert third == (b"ID3audio", False)
    assert calls == 2
    assert cache.stats.hits == 1
    assert cache.stats.misses == 2


def test_concurrent_requests_for_one_key_only_generate_once(tmp_path) -> None:
    cache = AudioCache(tmp_path / "cache")
    calls = 0

    async def slow_factory() -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return b"ID3audio"

    async def scenario():
        return await asyncio.gather(*(cache.get_or_create("same", slow_factory) for _ in range(5)))

    results = asyncio.run(scenario())
    assert [item[0] for item in results] == [b"ID3audio"] * 5
    # A duplicate submission must not be charged five times.
    assert calls == 1
    assert cache.stats.coalesced >= 1


def test_cache_refuses_to_store_empty_audio(tmp_path) -> None:
    cache = AudioCache(tmp_path / "cache")
    with pytest.raises(ValueError):
        cache.put("key", b"")
    assert cache.get("key") is None


def test_atomic_write_leaves_no_partial_file_when_writing_fails(tmp_path) -> None:
    target = tmp_path / "artifact.mp3"
    write_atomic(target, b"good-audio")

    class Exploding(bytes):
        pass

    # A failure mid-write must leave the previous good file untouched.
    with pytest.raises(TypeError):
        write_atomic(target, "not bytes")  # type: ignore[arg-type]
    assert target.read_bytes() == b"good-audio"
    assert not list(tmp_path.glob(".*tmp"))


def test_cache_does_not_accumulate_locks_for_every_key(tmp_path) -> None:
    cache = AudioCache(tmp_path / "cache")

    async def factory() -> bytes:
        return b"ID3audio"

    async def scenario():
        for index in range(50):
            await cache.get_or_create(f"key-{index}", factory)
        # Concurrent callers share one lock, which is released with the last one.
        await asyncio.gather(*(cache.get_or_create("shared", factory) for _ in range(5)))

    asyncio.run(scenario())
    assert cache._locks == {}
    assert cache._users == {}
    assert cache.usage()["entries"] == 51


# --------------------------------------------------------------------------- #
# Narration copy: number formats the voice misreads
# --------------------------------------------------------------------------- #

def test_spanish_number_formats_the_voice_misreads_are_spelled_out() -> None:
    from app.spanish_speech import spell_numbers_for_speech as spell

    # Measured against aura-2-diana-es: "3.500" is narrated "tres punto cinco"
    # and "2,5" as "veinticinco", so both are spelled out before synthesis.
    assert spell("El presupuesto fue de 3.500 euros.") == "El presupuesto fue de tres mil quinientos euros."
    assert spell("La cuerda medía 2,5 metros.") == "La cuerda medía dos coma cinco metros."
    assert spell("Costaba 1.200 pesetas.") == "Costaba mil doscientos pesetas."
    assert spell("Eran 1.200.000 personas.") == "Eran un millón doscientos mil personas."


def test_formats_the_voice_reads_correctly_are_left_untouched() -> None:
    from app.spanish_speech import spell_numbers_for_speech as spell

    for text in ("La casa tenía 147 ventanas.", "El tren salía a las 6:45.",
                 "El 12/03/2024 firmaron.", "Subió un 12%.", "El año 1990 ha sido largo.",
                 "Capítulo 1.º", "Version 1.2.3", "Recorrió 22 millas.", "La letra m sola."):
        assert spell(text) == text, text


@pytest.mark.parametrize("text,spoken", [
    # ElevenLabs dropped the "m" of "9 m" on every take; review heard "9 de altura".
    ("Las olas superaban los 9 m de altura.", "Las olas superaban los 9 metros de altura."),
    ("Bajó hasta los 960 hPa.", "Bajó hasta los 960 hectopascales."),
    ("Mide 18 km.", "Mide 18 kilómetros."),
    ("Mide 1 m.", "Mide 1 metro."),
    ("Una lente de 2,5 m.", "Una lente de dos coma cinco metros."),
    ("Iba a 100 km/h.", "Iba a 100 kilómetros por hora."),
])
def test_unit_abbreviations_after_a_number_are_spelled_out(text, spoken) -> None:
    from app.spanish_speech import spell_numbers_for_speech as spell
    assert spell(text) == spoken


def test_the_source_text_is_never_changed_only_the_narration_copy() -> None:
    from app import sentences

    source = "El presupuesto fue de 3.500 euros."
    row = sentences.segment_sentences(source, "hash", 0)[0]
    narration = sentences.build_synthesis_text(source, {})
    assert row["text"] == source
    assert narration == "El presupuesto fue de tres mil quinientos euros."
    # A reviewer's lexicon entry still wins over the automatic rewrite.
    assert sentences.build_synthesis_text("La ONCE y 2,5 metros.", {"ONCE": "once"}) == \
        "La once y dos coma cinco metros."


def test_a_quality_retake_regenerates_instead_of_returning_the_failed_audio(tmp_path) -> None:
    cache = AudioCache(tmp_path / "cache")
    calls = 0

    async def factory() -> bytes:
        nonlocal calls
        calls += 1
        return f"ID3take-{calls}".encode()

    async def scenario():
        first = await cache.get_or_create("key", factory)
        cached = await cache.get_or_create("key", factory)
        retake = await cache.get_or_create("key", factory, refresh=True)
        return first, cached, retake

    first, cached, retake = asyncio.run(scenario())
    assert first == (b"ID3take-1", False)
    assert cached == (b"ID3take-1", True)
    # A retake must not hand back the reading that already failed review.
    assert retake == (b"ID3take-2", False)
    assert cache.get("key") == b"ID3take-2"
