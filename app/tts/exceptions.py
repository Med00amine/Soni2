"""Production TTS errors."""


class TTSEngineError(RuntimeError):
    """Base class for actionable TTS failures."""


class TTSConfigurationError(TTSEngineError):
    """Raised for invalid or unavailable engine configuration."""


class TTSSynthesisError(TTSEngineError):
    """Raised after a synthesis request exhausts its retries."""
