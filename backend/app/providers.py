"""Narration (TTS) and speech-review (ASR) providers behind one explicit interface.

Text-to-speech generates audio; speech-to-text transcribes it for quality
assurance. They are configured separately because a good transcriber is not
necessarily a voice, and the project has to keep working when one account runs
out of credit. Every provider failure is translated into a classified
:class:`ProviderError` so the interface can explain what the user has to fix.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_ASR_URL = "https://api.fish.audio/v1/asr"
DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"
DEEPGRAM_ASR_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
# Chosen by benchmark on identical Peninsular Spanish passages (2026-10-25,
# docs/quality-system.md): eleven_v4 had no detected content error with two
# independent recognizers and was the only model that also read raw dates,
# amounts and abbreviations correctly. eleven_multilingual_v2 remains the
# conservative alternative ElevenLabs documents as most stable on long form.
ELEVENLABS_DEFAULT_MODEL = "eleven_v4"

MP3_HEADERS = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
MOCK_MARKER = b"ID3\x03\x00\x00\x00\x00\x00\x00SONI2-MOCK-TEXT:"
# A silent MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz, joint stereo). Repeating
# it gives the mock provider a file real players and ffmpeg will accept.
_MOCK_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413

# The concurrency limit each provider reported on its latest reply. ElevenLabs
# sends the account's real limit in a `maximum-concurrent-requests` header, so
# narration can use the plan's actual capacity instead of a guess.
reported_concurrency: dict[str, int] = {}


class ProviderError(RuntimeError):
    """A provider failure with a Spanish explanation and a retry classification.

    ``kind`` is one of ``credentials``, ``credit``, ``rate_limit``,
    ``unsupported_voice``, ``not_configured``, ``bad_audio``, ``network``,
    ``timeout`` or ``server``. ``permanent`` marks failures that retrying
    cannot fix, so the pipeline stops spending attempts on them.
    """

    def __init__(self, kind: str, message: str, *, permanent: bool, status_code: int | None = None,
                 retry_after: float | None = None, code: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.permanent = permanent
        self.status_code = status_code
        # Seconds the provider asked us to wait (Retry-After), when it said.
        self.retry_after = retry_after
        # The provider's own status word, e.g. "concurrent_limit_exceeded".
        self.code = code


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    provider: str
    language: str
    accent: str
    gender: str
    model: str = ""
    # False when nobody could tell the accent: a library voice whose labels the
    # API key is not allowed to read, and that the operator did not declare.
    accent_known: bool = True

    @property
    def is_peninsular(self) -> bool:
        """True for voices the provider documents as Spain/Castilian Spanish."""
        return self.language.casefold() == "es-es"

    def as_dict(self) -> dict[str, object]:
        return {
            "voice_id": self.voice_id,
            "name": self.name,
            "provider": self.provider,
            "language": self.language,
            "labels": {"language": "Spanish", "accent": self.accent, "gender": self.gender},
            "peninsular": self.is_peninsular,
            "accent_verified": self.accent_known,
        }


@dataclass
class Transcript:
    text: str
    engine: str
    segments: list[dict[str, object]] = field(default_factory=list)


# Fish Audio reference voices already configured for this project. The language
# sent with each request belongs to the voice, not to a single global setting.
FISH_VOICES = (
    Voice("ec0718028a2445fb9be8e8bfb56a0150", "Rodrigo", "fishaudio", "es-MX", "México", "male"),
    Voice("fb9d5855f185451983404deb10788ad4", "Isabela", "fishaudio", "es-CO", "Colombia", "female"),
    Voice("3f065d660212478a9c697fa965b4d098", "Carmen", "fishaudio", "es-ES", "España", "female"),
)

# Deepgram Aura-2 Spanish voices. Only the voices Deepgram documents as
# Peninsular (es-es) are marked es-ES; the rest stay Latin American so the
# accent check cannot be satisfied by a merely "Spanish" voice.
DEEPGRAM_VOICES = (
    Voice("aura-2-diana-es", "Diana", "deepgram", "es-ES", "España", "female", "aura-2"),
    Voice("aura-2-carina-es", "Carina", "deepgram", "es-ES", "España", "female", "aura-2"),
    Voice("aura-2-alvaro-es", "Álvaro", "deepgram", "es-ES", "España", "male", "aura-2"),
    Voice("aura-2-nestor-es", "Néstor", "deepgram", "es-ES", "España", "male", "aura-2"),
    Voice("aura-2-celeste-es", "Celeste", "deepgram", "es-CO", "Colombia", "female", "aura-2"),
    Voice("aura-2-sirio-es", "Sirio", "deepgram", "es-MX", "México", "male", "aura-2"),
)

MOCK_VOICES = (
    Voice("mock-es-es-female", "Simulada (España)", "mock", "es-ES", "España", "female", "mock"),
    Voice("mock-es-mx-male", "Simulada (México)", "mock", "es-MX", "México", "male", "mock"),
)

_CATALOG: dict[str, tuple[Voice, ...]] = {
    "fishaudio": FISH_VOICES,
    "deepgram": DEEPGRAM_VOICES,
    "mock": MOCK_VOICES,
}


_ACCENTS = {"es-es": "España", "es-mx": "México", "es-co": "Colombia", "es-ar": "Argentina",
            "es-cl": "Chile", "es-us": "Estados Unidos", "es-419": "Latinoamérica"}


def elevenlabs_voice(voice_id: str, name: str, language: str = "", model: str = "") -> Voice:
    """An ElevenLabs voice the operator configured.

    ElevenLabs voices are chosen by id from the voice library, so there is no
    fixed catalog. The accent is known only when the operator declares it: the
    library labels need a key allowed to read voices, and a Spain-only product
    must not assume one.
    """
    declared = language.strip()
    return Voice(voice_id, name, "elevenlabs", declared or "es", _ACCENTS.get(declared.casefold(), ""), "",
                 model or ELEVENLABS_DEFAULT_MODEL, accent_known=bool(declared))


def voices_for(provider: str) -> tuple[Voice, ...]:
    return _CATALOG.get(provider.casefold(), ())


def find_voice(provider: str, voice_id: str, catalog: tuple[Voice, ...] | None = None) -> Voice | None:
    voices = voices_for(provider) if catalog is None else catalog
    return next((voice for voice in voices if voice.voice_id == voice_id), None)


def default_voice(provider: str, catalog: tuple[Voice, ...] | None = None) -> Voice | None:
    """Prefer a documented Spain-Spanish voice; the product narrates for Spain."""
    catalog = voices_for(provider) if catalog is None else catalog
    return next((voice for voice in catalog if voice.is_peninsular), catalog[0] if catalog else None)


def resolve_voice(
    provider: str, voice_id: str, *, require_peninsular: bool = False, catalog: tuple[Voice, ...] | None = None,
) -> Voice:
    """Validate a voice before any expensive generation starts.

    ``catalog`` lists the voices of providers configured at runtime, such as
    ElevenLabs; the fixed catalogs are used otherwise.
    """
    provider = provider.casefold()
    catalog = voices_for(provider) if catalog is None else catalog
    if not catalog:
        raise ProviderError(
            "not_configured",
            f"El proveedor de voz «{provider}» no está disponible. Revisa TTS_PROVIDER en la configuración.",
            permanent=True,
        )
    voice = find_voice(provider, voice_id, catalog) if voice_id else default_voice(provider, catalog)
    if voice is None:
        names = ", ".join(item.voice_id for item in catalog)
        raise ProviderError(
            "unsupported_voice",
            f"La voz seleccionada no pertenece al proveedor activo. Voces disponibles: {names}.",
            permanent=True,
        )
    if require_peninsular and not voice.accent_known:
        raise ProviderError(
            "unsupported_voice",
            f"No se ha podido comprobar que la voz «{voice.name}» sea español de España. Si lo es, decláralo "
            "con ELEVENLABS_VOICE_LANGUAGE=es-ES; si no importa el acento, desactiva REQUIRE_PENINSULAR_VOICE.",
            permanent=True,
        )
    if require_peninsular and not voice.is_peninsular:
        raise ProviderError(
            "unsupported_voice",
            f"La voz «{voice.name}» es {voice.accent} y no español de España. "
            "Elige una voz es-ES antes de generar el audiolibro.",
            permanent=True,
        )
    return voice


def is_mp3(payload: bytes) -> bool:
    return bool(payload) and payload.startswith(MP3_HEADERS)


# --------------------------------------------------------------------------- #
# Mock provider: lets the whole pipeline run with no account and no spending.
# --------------------------------------------------------------------------- #

def mock_audio(text: str) -> bytes:
    """Deterministic placeholder audio that carries its own text for mock ASR.

    The payload is a real ID3-prefixed MP3 of silence, so players and ffmpeg
    accept it, with the spoken text recorded in the tag so the mock recognizer
    can return exactly what was "spoken" and exercise the comparison path.
    """
    encoded = text.encode("utf-8")
    header = MOCK_MARKER + len(encoded).to_bytes(4, "big") + encoded
    frames = max(2, min(400, len(text) // 8 + 2))
    return header + _MOCK_FRAME * frames


def mock_transcript(audio: bytes) -> str:
    if not audio.startswith(MOCK_MARKER):
        raise ProviderError(
            "bad_audio",
            "El transcriptor simulado sólo puede leer audio generado por el proveedor simulado.",
            permanent=True,
        )
    offset = len(MOCK_MARKER)
    length = int.from_bytes(audio[offset:offset + 4], "big")
    return audio[offset + 4:offset + 4 + length].decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #

def _provider_label(provider: str) -> str:
    return {"fishaudio": "Fish Audio", "deepgram": "Deepgram", "elevenlabs": "ElevenLabs",
            "mock": "proveedor simulado"}.get(provider.casefold(), provider)


def _error_detail(response: httpx.Response) -> tuple[str, str]:
    """``(status word, message)`` from a JSON error body such as ElevenLabs sends."""
    try:
        payload = response.json()
    except Exception:
        return "", ""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("status") or detail.get("code") or ""), str(detail.get("message") or "")[:240]
    if isinstance(detail, str):
        return "", detail[:240]
    return "", ""


def _classify_by_detail(provider: str, status: int, word: str, message: str, action: str) -> ProviderError | None:
    """Errors whose HTTP status alone says the wrong thing.

    ElevenLabs answers an exhausted balance with 401, the same status as a bad
    key, so the body's status word decides. Its own message is kept because it
    states exact amounts, such as how many credits remain.
    """
    label = _provider_label(provider)
    said = f" {label} dice: «{message}»" if message else ""
    if word == "quota_exceeded":
        return ProviderError("credit", f"La cuenta de {label} no tiene créditos suficientes para {action}."
                             f"{said} Recarga créditos o espera a la renovación del plan.",
                             permanent=True, status_code=status)
    if word == "missing_permissions":
        return ProviderError("credentials", f"La clave de {label} no tiene permiso para {action}.{said} "
                             "Edita los permisos de la clave en tu cuenta.", permanent=True, status_code=status)
    if word in {"invalid_api_key", "detected_unusual_activity"}:
        return ProviderError("credentials", f"{label} rechazó la clave.{said}", permanent=True, status_code=status)
    if word in {"voice_not_found", "voice_does_not_exist"}:
        return ProviderError("unsupported_voice", f"{label} no encuentra la voz configurada.{said}",
                             permanent=True, status_code=status)
    if word in {"payment_required", "paid_plan_required", "subscription_required"}:
        return ProviderError("credit", f"Esta voz o este modelo necesitan un plan de pago en {label}.{said}",
                             permanent=True, status_code=status)
    if word in {"too_many_concurrent_requests", "concurrent_limit_exceeded", "system_busy", "rate_limit_exceeded"}:
        return ProviderError("rate_limit", f"{label} está ocupado o se superó el límite de peticiones simultáneas."
                             f"{said} Se reintentará en unos segundos.", permanent=False, status_code=status)
    if word in {"validation_error", "invalid_request", "text_too_long", "max_character_limit_exceeded"}:
        return ProviderError("bad_audio", f"{label} rechazó la petición.{said}", permanent=True, status_code=status)
    return None


def _retry_after(response: httpx.Response) -> float | None:
    value = (response.headers or {}).get("retry-after", "")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds <= 600 else None


def classify_http_error(provider: str, error: httpx.HTTPStatusError, *, operation: str) -> ProviderError:
    """Turn an upstream status into something the user can act on."""
    classified = _classify_http_error(provider, error, operation=operation)
    classified.retry_after = _retry_after(error.response)
    classified.code = classified.code or _error_detail(error.response)[0]
    return classified


def _classify_http_error(provider: str, error: httpx.HTTPStatusError, *, operation: str) -> ProviderError:
    label = _provider_label(provider)
    status = error.response.status_code
    action = "generar audio" if operation == "tts" else "transcribir audio"
    word, message = _error_detail(error.response)
    specific = _classify_by_detail(provider, status, word, message, action)
    if specific:
        return specific
    if status in {401, 403}:
        return ProviderError("credentials", f"{label} rechazó las credenciales (HTTP {status}). "
                            "Revisa la clave de API del servidor y reinicia Soni2.", permanent=True, status_code=status)
    if status in {402, 429}:
        # 402 is an exhausted balance; 429 is a rate limit or a spent quota.
        kind = "credit" if status == 402 else "rate_limit"
        detail = (f"La cuenta de {label} no tiene saldo suficiente para {action} (HTTP 402). "
                  "Recarga los créditos o cambia de proveedor."
                  if status == 402 else
                  f"{label} alcanzó un límite de uso o se quedó sin cuota (HTTP 429). "
                  "Espera unos minutos o comprueba el consumo de la cuenta.")
        # A rate limit clears with time; an empty balance does not.
        return ProviderError(kind, detail, permanent=status == 402, status_code=status)
    if status == 404:
        return ProviderError("unsupported_voice", f"{label} no reconoce la voz o el modelo solicitado (HTTP 404).",
                             permanent=True, status_code=status)
    if status == 413:
        return ProviderError("bad_audio", f"El contenido supera el tamaño que admite {label} (HTTP 413).",
                             permanent=True, status_code=status)
    if status in {400, 422}:
        return ProviderError("bad_audio", f"{label} rechazó la petición o sus parámetros (HTTP {status}).",
                             permanent=True, status_code=status)
    return ProviderError("server", f"{label} devolvió un error del servidor (HTTP {status}). Inténtalo más tarde.",
                         permanent=False, status_code=status)


def classify_request_error(provider: str, error: Exception) -> ProviderError:
    label = _provider_label(provider)
    if isinstance(error, httpx.TimeoutException):
        return ProviderError("timeout", f"{label} tardó demasiado en responder. Inténtalo de nuevo.", permanent=False)
    return ProviderError("network", f"Soni2 no pudo conectar con {label}. Comprueba la conexión del servidor.",
                         permanent=False)


async def _post(provider: str, operation: str, client: httpx.AsyncClient, *args, **kwargs) -> httpx.Response:
    try:
        response = await client.post(*args, **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise classify_http_error(provider, exc, operation=operation) from exc
    except httpx.RequestError as exc:
        raise classify_request_error(provider, exc) from exc
    return response


# --------------------------------------------------------------------------- #
# Text to speech
# --------------------------------------------------------------------------- #

# Characters ElevenLabs accepts in one request, per model (GET /v1/models,
# measured 2026-10-25). Narration units are far smaller; this is the guard.
ELEVENLABS_MAX_CHARACTERS = {
    "eleven_v4": 10_000, "eleven_v4_turbo": 10_000, "eleven_v3": 5_000, "eleven_v3_conversational": 5_000,
    "eleven_multilingual_v2": 10_000, "eleven_flash_v2_5": 40_000, "eleven_turbo_v2_5": 40_000,
}


def max_request_characters(provider: str, model: str) -> int:
    if provider == "elevenlabs":
        return ELEVENLABS_MAX_CHARACTERS.get(model, 5_000)
    return {"deepgram": 2_000, "fishaudio": 10_000}.get(provider, 10_000)


async def synthesize(
    text: str, voice: Voice, *, speed: float = 1.0, api_key: str = "", model: str = "", timeout: float = 180,
    previous_text: str = "", next_text: str = "", seed: int | None = None,
    voice_settings: dict | None = None, meta: dict | None = None,
) -> bytes:
    """Narrate one passage. ``previous_text``/``next_text`` are the neighbouring
    passages, sent where the provider can use them to keep the joins smooth.

    ``seed`` makes an ElevenLabs take reproducible on a best-effort basis;
    ``meta`` receives what the provider reported about the request (request
    id, characters billed), so every attempt can be traced and costed.
    """
    provider = voice.provider
    if provider == "mock":
        if meta is not None:
            meta.update({"provider": "mock", "characters_billed": 0, "seed": seed})
        return mock_audio(text)
    if not api_key:
        raise ProviderError("not_configured",
                            f"Falta la clave de API de {_provider_label(provider)}. Configúrala en el servidor.",
                            permanent=True)
    if provider == "fishaudio":
        audio = await _synthesize_fish(text, voice, speed, api_key, model or "drama-3", timeout)
    elif provider == "deepgram":
        audio = await _synthesize_deepgram(text, voice, api_key, timeout)
    elif provider == "elevenlabs":
        limit = max_request_characters(provider, model or voice.model)
        if len(text) > limit:
            raise ProviderError("bad_audio", f"El fragmento tiene {len(text)} caracteres y el modelo admite "
                                f"{limit} por petición.", permanent=True)
        audio = await _synthesize_elevenlabs(text, voice, speed, api_key, model or voice.model, timeout,
                                             previous_text, next_text, seed, voice_settings, meta)
    else:
        raise ProviderError("not_configured", f"El proveedor de narración «{provider}» no está implementado.",
                            permanent=True)
    if not is_mp3(audio):
        raise ProviderError("bad_audio",
                            f"{_provider_label(provider)} devolvió datos que no son un MP3 válido.", permanent=False)
    return audio


async def _synthesize_fish(text: str, voice: Voice, speed: float, api_key: str, model: str, timeout: float) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "fishaudio", "tts", client, FISH_TTS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "model": model, "Accept": "audio/mpeg"},
            json={"text": text, "reference_id": voice.voice_id, "format": "mp3", "language": voice.language,
                  "normalize": True, "prosody": {"speed": speed, "normalize_loudness": True}, "chunk_length": 300},
        )
    return response.content


async def _synthesize_deepgram(text: str, voice: Voice, api_key: str, timeout: float) -> bytes:
    # Aura-2 exposes no speed control, so callers are told speed is unavailable
    # rather than having it silently ignored (see supports_speed below).
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "deepgram", "tts", client, DEEPGRAM_TTS_URL,
            params={"model": voice.voice_id, "encoding": "mp3"},
            headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
            json={"text": text},
        )
    return response.content


async def _synthesize_elevenlabs(
    text: str, voice: Voice, speed: float, api_key: str, model: str, timeout: float,
    previous_text: str, next_text: str, seed: int | None = None, voice_settings: dict | None = None,
    meta: dict | None = None,
) -> bytes:
    body: dict[str, object] = {"text": text, "model_id": model or ELEVENLABS_DEFAULT_MODEL}
    if seed is not None:
        body["seed"] = int(seed) % 4_294_967_296
    # Request stitching: the neighbouring paragraphs let the voice carry its
    # intonation across the join instead of starting every paragraph afresh.
    # ElevenLabs bills only `text`; the context is free (measured: a 20-character
    # request with 200 characters of context cost 20 credits).
    if previous_text:
        body["previous_text"] = previous_text
    if next_text:
        body["next_text"] = next_text
    settings_override = {key: value for key, value in (voice_settings or {}).items() if value is not None}
    if abs(speed - 1.0) > 1e-6:
        settings_override["speed"] = speed
    if settings_override:
        # Only when asked: a voice_settings object replaces the settings the
        # voice was published with, which normal speed should keep.
        body["voice_settings"] = settings_override
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "elevenlabs", "tts", client, f"{ELEVENLABS_TTS_URL}/{quote(voice.voice_id, safe='')}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json=body,
        )
    _remember_concurrency("elevenlabs", response)
    if meta is not None:
        headers = getattr(response, "headers", None) or {}
        cost = str(headers.get("character-cost", ""))
        meta.update({
            "provider": "elevenlabs", "model": body["model_id"], "seed": body.get("seed"),
            "request_id": str(headers.get("request-id", "")) or None,
            "characters_billed": int(cost) if cost.isdigit() else None,
            "characters_sent": len(text),
        })
    return response.content


def _remember_concurrency(provider: str, response: object) -> None:
    value = str((getattr(response, "headers", None) or {}).get("maximum-concurrent-requests", ""))
    if value.isdigit() and int(value) > 0:
        reported_concurrency[provider] = int(value)


def supports_speed(provider: str) -> bool:
    """Fish Audio and ElevenLabs expose a narration speed parameter; Deepgram does not."""
    return provider.casefold() in {"fishaudio", "elevenlabs", "mock"}


# --------------------------------------------------------------------------- #
# Speech to text (quality review)
# --------------------------------------------------------------------------- #

async def transcribe(
    audio: bytes, *, provider: str, api_key: str = "", filename: str = "review.mp3",
    content_type: str = "audio/mpeg", with_timestamps: bool = False, language: str = "es",
    timeout: float = 240,
) -> Transcript:
    provider = provider.casefold()
    if provider == "mock":
        return Transcript(mock_transcript(audio), "mock-recognizer", [])
    if not api_key:
        raise ProviderError("not_configured",
                            f"Falta la clave de API de {_provider_label(provider)} para revisar el audio.",
                            permanent=True)
    if provider == "fishaudio":
        result = await _transcribe_fish(audio, api_key, filename, content_type, with_timestamps, language, timeout)
    elif provider == "deepgram":
        result = await _transcribe_deepgram(audio, api_key, content_type, with_timestamps, language, timeout)
    elif provider == "elevenlabs":
        result = await _transcribe_elevenlabs(audio, api_key, filename, content_type, language, timeout)
    else:
        raise ProviderError("not_configured", f"El transcriptor «{provider}» no está implementado.", permanent=True)
    if not result.text.strip():
        raise ProviderError("bad_audio",
                            f"{_provider_label(provider)} no devolvió ninguna transcripción para este audio.",
                            permanent=False)
    return result


async def _transcribe_fish(
    audio: bytes, api_key: str, filename: str, content_type: str, with_timestamps: bool,
    language: str, timeout: float,
) -> Transcript:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "fishaudio", "asr", client, FISH_ASR_URL,
            headers={"Authorization": f"Bearer {api_key}", "model": "transcribe-1"},
            data={"language": language.split("-")[0], "ignore_timestamps": "false" if with_timestamps else "true"},
            files={"audio": (filename, audio, content_type or "audio/mpeg")},
        )
    payload = response.json()
    segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    return Transcript(str(payload.get("text", "")), "fish-audio-transcribe-1", segments)


async def _transcribe_deepgram(
    audio: bytes, api_key: str, content_type: str, with_timestamps: bool, language: str, timeout: float,
) -> Transcript:
    params = {"model": "nova-3", "language": language, "punctuate": "true", "smart_format": "true"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "deepgram", "asr", client, DEEPGRAM_ASR_URL, params=params,
            headers={"Authorization": f"Token {api_key}", "Content-Type": content_type or "audio/mpeg"},
            content=audio,
        )
    payload = response.json()
    alternative: dict[str, object] = {}
    channels = (payload.get("results") or {}).get("channels") or []
    if channels and isinstance(channels[0], dict):
        alternatives = channels[0].get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            alternative = alternatives[0]
    segments: list[dict[str, object]] = []
    # Deepgram always returns word timings and confidences; keeping them costs
    # nothing and lets review point at the exact second a word was misheard.
    # They are normalized onto the {text,start,end} shape the alignment code
    # already uses for Fish Audio segments.
    for word in alternative.get("words") or []:
        if isinstance(word, dict) and "start" in word and "end" in word:
            segments.append({"text": str(word.get("punctuated_word") or word.get("word", "")),
                             "start": word["start"], "end": word["end"],
                             "confidence": word.get("confidence")})
    return Transcript(str(alternative.get("transcript", "")), "deepgram-nova-3", segments)


ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_STT_MODEL = "scribe_v2"


async def _transcribe_elevenlabs(
    audio: bytes, api_key: str, filename: str, content_type: str, language: str, timeout: float,
) -> Transcript:
    """ElevenLabs Scribe: a second recognizer, trained and served separately
    from Deepgram's, used to adjudicate when the first one disagrees."""
    code = {"es": "spa", "en": "eng"}.get(language.split("-")[0], "")
    data = {"model_id": ELEVENLABS_STT_MODEL, "tag_audio_events": "false", "timestamps_granularity": "word"}
    if code:
        data["language_code"] = code
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post(
            "elevenlabs", "asr", client, ELEVENLABS_STT_URL, headers={"xi-api-key": api_key},
            data=data, files={"file": (filename, audio, content_type or "audio/mpeg")},
        )
    payload = response.json()
    segments = []
    for word in payload.get("words") or []:
        if isinstance(word, dict) and word.get("type") == "word" and "start" in word:
            logprob = word.get("logprob")
            segments.append({"text": str(word.get("text", "")).strip(), "start": word["start"], "end": word["end"],
                             "confidence": round(math.exp(logprob), 4) if isinstance(logprob, (int, float)) else None})
    return Transcript(str(payload.get("text", "")), f"elevenlabs-{ELEVENLABS_STT_MODEL}", segments)


ELEVENLABS_SHARED_VOICES_URL = "https://api.elevenlabs.io/v1/shared-voices"
ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"


async def elevenlabs_voice_profile(voice_id: str, api_key: str, *, timeout: float = 15) -> dict | None:
    """What ElevenLabs itself says about a voice: name, locale, and the models
    on which that accent is verified.

    A voice in the account is read from /v1/voices; a library voice used by
    id is found in /v1/shared-voices. Returns None when neither says.
    """
    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(f"{ELEVENLABS_VOICES_URL}/{quote(voice_id, safe='')}", headers=headers)
            if response.status_code == 200:
                payload = response.json()
                labels = payload.get("labels") or {}
                return _voice_profile(voice_id, payload.get("name", ""), labels.get("accent", ""),
                                      payload.get("verified_languages") or [], "account")
            response = await client.get(ELEVENLABS_SHARED_VOICES_URL, headers=headers,
                                        params={"search": voice_id, "page_size": 5})
            if response.status_code == 200:
                for entry in response.json().get("voices") or []:
                    if entry.get("voice_id") == voice_id:
                        return _voice_profile(voice_id, entry.get("name", ""), entry.get("accent", ""),
                                              entry.get("verified_languages") or [], "shared_library",
                                              locale=entry.get("locale") or "")
        except (httpx.HTTPError, ValueError):
            return None
    return None


def _voice_profile(voice_id: str, name: str, accent: str, verified: list, source: str, locale: str = "") -> dict:
    peninsular_models = sorted({str(item.get("model_id")) for item in verified
                                if isinstance(item, dict) and (item.get("locale") == "es-ES")
                                and str(item.get("accent", "")).casefold() in {"peninsular", "castilian", "spain"}})
    return {"voice_id": voice_id, "name": name, "accent": accent, "locale": locale, "source": source,
            "peninsular_models": peninsular_models}


def cache_fingerprint(provider: str, voice: Voice, model: str, speed: float, text: str) -> str:
    """Stable cache key: identical requests to identical settings reuse audio."""
    speed_key = f"{speed:.3f}" if supports_speed(provider) else "n/a"
    # An ElevenLabs voice id fully identifies the voice; its language is only
    # what the operator declared, and declaring it must not void paid audio.
    language = "" if provider.casefold() == "elevenlabs" else voice.language
    identity = "\u0000".join([provider.casefold(), voice.voice_id, language, model, speed_key, text])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
