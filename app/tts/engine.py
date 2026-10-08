"""Production MeloTTS adapter with lazy lifecycle, retries, and resume support."""

from __future__ import annotations

import logging
import time
import wave
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable

from .base import SynthesisMetrics, TTSEngine
from .config import ProductionTTSConfig
from .exceptions import TTSConfigurationError, TTSSynthesisError

logger = logging.getLogger(__name__)


class MeloTTSEngine(TTSEngine):
    """Spanish MeloTTS adapter; the model is loaded once per engine instance."""

    model_name = "MeloTTS"

    def __init__(
        self,
        config: ProductionTTSConfig | None = None,
        *,
        model_loader: Callable[[ProductionTTSConfig], Any] | None = None,
    ) -> None:
        self.config = config or ProductionTTSConfig()
        self._model_loader = model_loader or _load_melo_model
        self._model: Any | None = None
        self._speaker_id: Any | None = None
        self._metrics: SynthesisMetrics | None = None

    @property
    def last_metrics(self) -> SynthesisMetrics | None:
        return self._metrics

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            self._model, self._speaker_id = self._model_loader(self.config)
        except TTSConfigurationError:
            raise
        except Exception as exc:
            raise TTSConfigurationError(
                "Unable to load MeloTTS. Install the production extra and verify the model setup."
            ) from exc

    def synthesize(self, text: str, output_path: Path, voice_id: str | None = None) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text.")
        output_path = Path(output_path)
        if self.config.cache_enabled and not self.config.force_regenerate and _valid_wav(output_path):
            logger.info("TTS cache hit path=%s", output_path)
            return output_path

        self._ensure_model()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        speaker = voice_id or self.config.voice
        started = time.perf_counter()
        last_error: Exception | None = None
        retries = 0
        for attempt in range(self.config.max_retries + 1):
            retries = attempt
            try:
                self._synthesize_once(text, output_path, speaker)
                audio_seconds = _wav_duration(output_path)
                elapsed = time.perf_counter() - started
                self._metrics = SynthesisMetrics(
                    sentence_id=output_path.stem,
                    text_length=len(text),
                    model=self.model_name,
                    device=self.config.device,
                    synthesis_seconds=elapsed,
                    audio_seconds=audio_seconds,
                    retries=retries,
                )
                logger.info(
                    "tts_synthesis sentence_id=%s text_length=%d model=%s device=%s "
                    "synthesis_seconds=%.3f audio_seconds=%.3f rtf=%.3f retries=%d",
                    output_path.stem,
                    len(text),
                    self.model_name,
                    self.config.device,
                    elapsed,
                    audio_seconds,
                    self._metrics.real_time_factor,
                    retries,
                )
                return output_path
            except TTSConfigurationError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "tts_synthesis_failed sentence_id=%s retry=%d error=%s",
                    output_path.stem,
                    attempt,
                    exc,
                )
                if output_path.exists():
                    output_path.unlink()
                if attempt < self.config.max_retries and self.config.retry_delay:
                    time.sleep(self.config.retry_delay)
        raise TTSSynthesisError(
            f"MeloTTS failed for sentence '{output_path.stem}' after {retries + 1} attempt(s)."
        ) from last_error

    def _synthesize_once(self, text: str, output_path: Path, speaker: str) -> None:
        arguments = (text, self._speaker_id_for(speaker), str(output_path))
        if self.config.timeout is None:
            self._model.tts_to_file(*arguments, speed=1.0)
            return
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="melotts")
        future = executor.submit(self._model.tts_to_file, *arguments, speed=1.0)
        try:
            future.result(timeout=self.config.timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"MeloTTS exceeded the configured timeout of {self.config.timeout} seconds."
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _speaker_id_for(self, speaker: str) -> Any:
        if speaker in self._model.hps.data.spk2id:
            return self._model.hps.data.spk2id[speaker]
        raise TTSConfigurationError(f"Unknown MeloTTS speaker '{speaker}'.")


def _load_melo_model(config: ProductionTTSConfig) -> tuple[Any, Any]:
    try:
        from melo.api import TTS
    except ImportError as exc:
        raise TTSConfigurationError(
            "MeloTTS is not installed. Install with: pip install -e '.[production-tts]'"
        ) from exc
    tts_kwargs: dict[str, Any] = {
        "language": config.language.upper(),
        "device": config.device,
    }
    if config.model_path is not None:
        if not config.model_path.exists():
            raise TTSConfigurationError(f"Configured TTS model path does not exist: {config.model_path}")
        if config.model_path.is_file():
            tts_kwargs.update(
                ckpt_path=str(config.model_path),
                use_hf=False,
            )
        else:
            config_path = config.model_path / f"{config.language.lower()}.json"
            if config_path.is_file():
                tts_kwargs.update(config_path=str(config_path), use_hf=False)
    model = TTS(**tts_kwargs)
    speaker_ids = model.hps.data.spk2id
    if config.voice not in speaker_ids:
        raise TTSConfigurationError(f"Unknown MeloTTS speaker '{config.voice}'.")
    return model, speaker_ids[config.voice]


def _valid_wav(path: Path) -> bool:
    try:
        return path.is_file() and _wav_duration(path) > 0
    except (OSError, EOFError, wave.Error):
        return False


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        rate = audio.getframerate()
        frames = audio.getnframes()
        if rate <= 0 or frames <= 0:
            raise ValueError(f"Generated WAV is empty or invalid: {path}")
        return frames / rate
