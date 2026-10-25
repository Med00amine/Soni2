"""Technical checks on every narrated segment before it can count as done.

Two layers:

* **Stream checks** (pure Python, always run): the payload is an MPEG audio
  stream whose frame headers chain from start to end, with one sample rate,
  one channel mode, a playable duration and no truncated final frame. A file
  that fails here is corrupt, whatever the recognizer says.
* **Signal checks** (need ffmpeg): decoded samples are inspected for leading,
  trailing and internal silence, clipping, overall level, and an abrupt end
  (speech still loud in the last few milliseconds, as when generation is cut
  off mid-word).

A speaking-rate check compares the duration with the length of the spoken
text: narration far faster than speech is usually missing words, far slower is
usually stuck or padded. The limits are deliberately wide; they were checked
against real ElevenLabs Spanish narration (see docs/quality-system.md).

``status`` is ``failed`` (unusable: regenerate), ``needs_review`` (usable but
suspicious: worth a retake) or ``passed``. Passing proves the audio is
technically sound, not that it sounds right.
"""

from __future__ import annotations

import array
import math
import shutil
import subprocess

from app.daisy_export import _MP3_BITRATES, _MP3_RATES

PCM_RATE = 16000
SILENCE_DBFS = -45.0
MAX_LEADING_SILENCE = 1.5
MAX_TRAILING_SILENCE = 2.0
MAX_INTERNAL_SILENCE = 2.0
MAX_CLIPPED_RATIO = 0.002
# Characters of spoken text per second. Peninsular narration measured at about
# 13-17; outside this band words are likely missing or the voice is stuck.
MIN_CHARS_PER_SECOND = 6.0
MAX_CHARS_PER_SECOND = 30.0


def _frame(data: bytes, offset: int) -> dict | None:
    if offset + 4 > len(data) or data[offset] != 0xFF or data[offset + 1] & 0xE0 != 0xE0:
        return None
    version_bits = (data[offset + 1] >> 3) & 0x3
    layer_bits = (data[offset + 1] >> 1) & 0x3
    bitrate_index = data[offset + 2] >> 4
    rate_index = (data[offset + 2] >> 2) & 0x3
    if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    layer = 4 - layer_bits
    version = 1 if version_bits == 3 else 2
    bitrate = _MP3_BITRATES[(version, layer)][bitrate_index] * 1000
    rate = _MP3_RATES[version_bits][rate_index]
    padding = (data[offset + 2] >> 1) & 0x1
    channel_mode = data[offset + 3] >> 6
    if layer == 1:
        length, samples = (12 * bitrate // rate + padding) * 4, 384
    else:
        samples = 1152 if layer == 2 or version == 1 else 576
        length = samples // 8 * bitrate // rate + padding
    return {"length": length, "samples": samples, "rate": rate, "bitrate": bitrate,
            "channels": 1 if channel_mode == 3 else 2, "layer": layer}


def inspect_stream(data: bytes) -> dict:
    """Frame-level facts about an MP3 payload."""
    result: dict = {"bytes": len(data), "frames": 0, "duration_seconds": 0.0, "sample_rates": [],
                    "channels": [], "bitrates_kbps": [], "errors": [], "id3_bytes": 0, "skipped_bytes": 0}
    if not data:
        result["errors"].append("empty")
        return result
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | data[9] & 0x7F
        offset = 10 + size
        result["id3_bytes"] = offset
    rates, channels, bitrates = set(), set(), set()
    first_frame = None
    while offset < len(data) - 4:
        frame = _frame(data, offset)
        if frame and (offset + frame["length"] >= len(data) or _frame(data, offset + frame["length"])):
            if first_frame is None:
                first_frame = offset
            if offset + frame["length"] > len(data):
                result["errors"].append("truncated_final_frame")
                break
            result["frames"] += 1
            result["duration_seconds"] += frame["samples"] / frame["rate"]
            rates.add(frame["rate"])
            channels.add(frame["channels"])
            bitrates.add(frame["bitrate"] // 1000)
            offset += frame["length"]
        else:
            if first_frame is not None:
                result["skipped_bytes"] += 1
            offset += 1
    result["duration_seconds"] = round(result["duration_seconds"], 3)
    result["sample_rates"] = sorted(rates)
    result["channels"] = sorted(channels)
    result["bitrates_kbps"] = sorted(bitrates)
    if not result["frames"]:
        result["errors"].append("no_audio_frames")
    if len(rates) > 1:
        result["errors"].append("mixed_sample_rates")
    if len(channels) > 1:
        result["errors"].append("mixed_channel_modes")
    # Junk between frames means a damaged or badly joined stream. A few bytes
    # are normal (a trailing tag); hundreds are not.
    if result["skipped_bytes"] > 512:
        result["errors"].append("corrupt_frames")
    return result


def decode_pcm(data: bytes, *, rate: int = PCM_RATE, timeout: float = 60) -> tuple[array.array | None, str]:
    executable = shutil.which("ffmpeg")
    if not executable:
        return None, "ffmpeg no está instalado"
    try:
        completed = subprocess.run(
            [executable, "-hide_banner", "-nostats", "-v", "error", "-i", "pipe:0",
             "-f", "s16le", "-ac", "1", "-ar", str(rate), "pipe:1"],
            input=data, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "la decodificación tardó demasiado"
    if completed.returncode != 0 or len(completed.stdout) < 2:
        return None, completed.stderr.decode("utf-8", "replace")[:200] or "no se pudo decodificar"
    samples = array.array("h")
    samples.frombytes(completed.stdout[:len(completed.stdout) - len(completed.stdout) % 2])
    return samples, ""


def _dbfs(values) -> float:
    if not values:
        return -120.0
    power = sum(int(value) * int(value) for value in values) / len(values)
    return 20 * math.log10(math.sqrt(power) / 32768) if power > 0 else -120.0


def analyze_signal(samples: array.array, rate: int = PCM_RATE) -> dict:
    """Silence, level and clipping measurements on decoded mono samples."""
    window = max(1, rate // 50)  # 20 ms
    levels = [_dbfs(samples[start:start + window]) for start in range(0, len(samples), window)]
    voiced = [level > SILENCE_DBFS for level in levels]
    seconds = window / rate
    leading = next((index for index, value in enumerate(voiced) if value), len(voiced)) * seconds
    trailing = next((index for index, value in enumerate(reversed(voiced)) if value), len(voiced)) * seconds
    longest = current = 0
    first = next((index for index, value in enumerate(voiced) if value), None)
    last = len(voiced) - 1 - next((index for index, value in enumerate(reversed(voiced)) if value), len(voiced))
    if first is not None:
        for value in voiced[first:last + 1]:
            current = 0 if value else current + 1
            longest = max(longest, current)
    clipped = sum(1 for value in samples if value >= 32700 or value <= -32700)
    tail = samples[-max(1, rate * 30 // 1000):]
    return {
        "duration_seconds": round(len(samples) / rate, 3),
        "leading_silence_seconds": round(leading, 3),
        "trailing_silence_seconds": round(trailing, 3),
        "longest_internal_silence_seconds": round(longest * seconds, 3),
        "clipped_sample_ratio": round(clipped / max(1, len(samples)), 6),
        "rms_dbfs": round(_dbfs(samples), 1),
        "peak": max((abs(int(value)) for value in samples), default=0),
        "final_30ms_dbfs": round(_dbfs(tail), 1),
    }


def assess(data: bytes, spoken_text: str, *, decode: bool = True) -> dict:
    """Integrity verdict for one narrated segment."""
    stream = inspect_stream(data)
    reasons: list[str] = []
    failures = [error for error in stream["errors"] if error != "truncated_final_frame"]
    if "truncated_final_frame" in stream["errors"]:
        reasons.append("truncated_final_frame")
    result: dict = {"engine": "soni2-integrity", "stream": stream}
    duration = stream["duration_seconds"]
    characters = len("".join(spoken_text.split()))
    if failures:
        result.update({"status": "failed", "reasons": failures, "duration_seconds": duration})
        return result
    if duration < 0.2:
        result.update({"status": "failed", "reasons": ["suspiciously_short_duration"], "duration_seconds": duration})
        return result
    signal: dict | None = None
    if decode:
        samples, error = decode_pcm(data)
        if samples is None:
            result["signal"] = {"status": "unavailable", "reason": error}
            if "ffmpeg" not in error:
                result.update({"status": "failed", "reasons": ["decode_failed"], "duration_seconds": duration})
                return result
        else:
            signal = analyze_signal(samples)
            result["signal"] = signal
    speech = duration
    if signal:
        speech = max(0.05, signal["duration_seconds"] - signal["leading_silence_seconds"]
                     - signal["trailing_silence_seconds"])
        if signal["rms_dbfs"] < -50:
            reasons.append("silent_audio")
            # Silence from start to end is the longest abnormal silence there is.
            if signal["duration_seconds"] > MAX_INTERNAL_SILENCE:
                reasons.append("abnormal_silence")
        if signal["leading_silence_seconds"] > MAX_LEADING_SILENCE:
            reasons.append("long_leading_silence")
        if signal["trailing_silence_seconds"] > MAX_TRAILING_SILENCE:
            reasons.append("long_trailing_silence")
        if signal["longest_internal_silence_seconds"] > MAX_INTERNAL_SILENCE:
            reasons.append("abnormal_silence")
        if signal["clipped_sample_ratio"] > MAX_CLIPPED_RATIO:
            reasons.append("possible_clipping")
        # Speech that is still at full level in its last 30 ms, with no room
        # after it: generation stopped mid-word. Judged against the passage's
        # own level, so a quietly narrated passage is held to the same rule.
        if (signal["trailing_silence_seconds"] < 0.05
                and signal["final_30ms_dbfs"] > max(-35.0, signal["rms_dbfs"] - 4.0)):
            reasons.append("abrupt_end")
    rate = characters / speech if speech else 0.0
    result["chars_per_second"] = round(rate, 2)
    if characters >= 25 and signal and "silent_audio" not in reasons:
        if rate > MAX_CHARS_PER_SECOND:
            reasons.append("speech_too_fast_for_text")
        elif rate < MIN_CHARS_PER_SECOND:
            reasons.append("speech_too_slow_for_text")
    result.update({
        "status": "needs_review" if reasons else "passed",
        "reasons": reasons,
        "duration_seconds": duration,
        # Kept for the report and older readers of acoustic_json.
        "peak": signal["peak"] if signal else None,
        "clipped_sample_ratio": signal["clipped_sample_ratio"] if signal else None,
        "longest_silence_seconds": signal["longest_internal_silence_seconds"] if signal else None,
    })
    return result
