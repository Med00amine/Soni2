"""Master the narration so a whole book sounds like one continuous reading.

A text-to-speech provider returns each passage as an independent clip. Pasted
together, the result gives away that it was assembled: every clip starts and
ends with a different amount of dead air, so sentences either collide or leave
an awkward hole; levels drift between clips, so the listener keeps reaching for
the volume; and the joins click, because an MP3 frame boundary is not a zero
crossing.

Three things fix that, and they are all measurable:

* **Trim the ragged edges.** Silence is removed from the start and end of every
  passage, so the pacing comes from this module rather than from whatever the
  provider happened to leave behind.
* **Put the pauses back deliberately.** A consistent gap between paragraphs and
  a longer one between chapters is what a human reader does, and what a blind
  listener needs in order to follow the structure.
* **Normalise the loudness.** One EBU R128 pass per chapter to a fixed target
  means the book plays at the same comfortable level throughout, on any device.

Every step needs ffmpeg. Without it the audio is byte-concatenated exactly as
before and the caller is told, so a degraded build is visible rather than
silent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Anything below this is dead air rather than speech. Generous enough to catch
# the provider's padding, quiet enough to keep a softly spoken first syllable.
SILENCE_THRESHOLD_DB = -50
# A short fade across each join: an MP3 frame boundary is not a zero crossing,
# and the step between two clips is audible as a click without it.
JOIN_FADE_SECONDS = 0.012


@dataclass
class MasteringResult:
    audio: bytes
    mastered: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"mastered": self.mastered, "detail": self.detail}


@dataclass
class JoinedAudio(MasteringResult):
    """Joined audio plus where each passage's speech sits inside it.

    ``clips`` is what DAISY needs to highlight the right sentence while the
    audio plays. It is measured, not estimated, so it cannot drift.
    """

    clips: list[tuple[float, float]] = field(default_factory=list)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


def _run(arguments: list[str], *, timeout: float = 1800) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(arguments, capture_output=True, timeout=timeout, check=False)


def _trim_filter(gap_seconds: float, *, trim: bool = True) -> str:
    """Trim a passage's dead air, fade its edges, then add the pause after it.

    ``silenceremove`` only trims the start of a stream, so the end is trimmed
    by reversing the audio, trimming the new start, and reversing it back.
    """
    steps = ["aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo"]
    if trim:
        steps += [
            f"silenceremove=start_periods=1:start_silence=0:start_threshold={SILENCE_THRESHOLD_DB}dB:detection=peak",
            "areverse",
            f"silenceremove=start_periods=1:start_silence=0:start_threshold={SILENCE_THRESHOLD_DB}dB:detection=peak",
            "areverse",
        ]
    steps.append(f"afade=t=in:st=0:d={JOIN_FADE_SECONDS}")
    if gap_seconds > 0:
        steps.append(f"apad=pad_dur={gap_seconds:.3f}")
    return ",".join(steps)


def _trim_and_pad(executable: str, source: Path, destination: Path, gap_seconds: float,
                  *, trim: bool = True) -> str:
    """Trim one passage's dead air, fade its edges, append its pause.

    ``trim=False`` keeps the passage whole and only adds the pause, for audio
    quiet enough that trimming would remove all of it.
    """
    arguments = [executable, "-hide_banner", "-v", "error", "-y", "-i", str(source),
                 "-af", _trim_filter(gap_seconds, trim=trim),
                 "-c:a", "libmp3lame", "-q:a", "2", "-write_xing", "0", str(destination)]
    result = _run(arguments)
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        return result.stderr.decode("utf-8", "replace")[:400] or "ffmpeg produced no audio"
    return ""


def join_passages(
    segments: list[bytes], *, gap_ms: int = 0, final_gap_ms: int = 0,
) -> JoinedAudio:
    """Join narrated passages with trimmed edges and deliberate pauses.

    Each passage is trimmed and padded on its own, so the position of every
    passage in the result is known exactly rather than estimated. DAISY players
    use those positions to follow the text while the audio plays, and a guess
    would drift further with every paragraph.
    """
    if not segments:
        raise ValueError("Cannot assemble an empty narration sequence.")
    executable = ffmpeg_path()
    if executable is None:
        return JoinedAudio(b"".join(segments), False,
                           "ffmpeg no está disponible: el audio se concatena sin pulir.", [])

    gaps = [gap_ms / 1000] * len(segments)
    gaps[-1] = final_gap_ms / 1000

    with tempfile.TemporaryDirectory(prefix="soni2-master-") as folder:
        root = Path(folder)
        prepared: list[Path] = []
        clips: list[tuple[float, float]] = []
        position = 0.0
        for index, (data, gap) in enumerate(zip(segments, gaps)):
            source = root / f"raw-{index:05d}.mp3"
            source.write_bytes(data)
            destination = root / f"cut-{index:05d}.mp3"
            error = _trim_and_pad(executable, source, destination, gap)
            if error:
                return JoinedAudio(b"".join(segments), False,
                                   f"ffmpeg no pudo pulir el audio: {error}", [])
            length = duration_seconds(destination.read_bytes())
            original = duration_seconds(data)
            if length is None:
                return JoinedAudio(b"".join(segments), False,
                                   "No se pudo medir la duración del audio pulido.", [])
            # A passage that is quiet throughout would be trimmed away entirely.
            # Keep the original rather than silently dropping a paragraph of the
            # book; only its pause is added.
            speech = length - gap
            if original and speech < min(0.05, original * 0.1):
                error = _trim_and_pad(executable, source, destination, gap, trim=False)
                if error:
                    return JoinedAudio(b"".join(segments), False,
                                       f"ffmpeg no pudo preparar un pasaje muy silencioso: {error}", [])
                length = duration_seconds(destination.read_bytes())
                if length is None:
                    return JoinedAudio(b"".join(segments), False,
                                       "No se pudo medir la duración del audio pulido.", [])
            # The pause belongs to the gap after the passage, not to its speech,
            # so the text stops being highlighted when the reading stops.
            clips.append((position, max(position, position + length - gap)))
            position += length
            prepared.append(destination)

        if len(prepared) == 1:
            return JoinedAudio(prepared[0].read_bytes(), True,
                               "Bordes recortados y pausa aplicada.", clips)

        manifest = root / "passages.ffconcat"
        manifest.write_text(
            "ffconcat version 1.0\n" + "".join(f"file '{path.as_posix()}'\n" for path in prepared),
            encoding="utf-8",
        )
        joined = root / "joined.mp3"
        # Every prepared file came out of the same encoder, so the frames can be
        # copied: nothing is re-encoded and no boundary moves.
        result = _run([executable, "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(manifest), "-map", "0:a:0", "-c:a", "copy",
                       "-write_xing", "0", str(joined)])
        if result.returncode != 0 or not joined.is_file() or joined.stat().st_size == 0:
            result = _run([executable, "-hide_banner", "-v", "error", "-y", "-f", "concat",
                           "-safe", "0", "-i", str(manifest), "-map", "0:a:0",
                           "-c:a", "libmp3lame", "-q:a", "2", "-write_xing", "0", str(joined)])
        if result.returncode != 0 or not joined.is_file() or joined.stat().st_size == 0:
            return JoinedAudio(b"".join(segments), False,
                               "ffmpeg no pudo unir los pasajes pulidos.", [])
        return JoinedAudio(joined.read_bytes(), True,
                           f"Bordes recortados y pausas de {gap_ms} ms aplicadas.", clips)


_LOUDNORM_KEYS = {
    "input_i": "measured_I",
    "input_tp": "measured_TP",
    "input_lra": "measured_LRA",
    "input_thresh": "measured_thresh",
    "target_offset": "offset",
}


def _measure_loudness(executable: str, source: Path, target_lufs: float) -> dict[str, str] | None:
    """First loudnorm pass: measure, so the second pass can correct exactly."""
    result = _run([executable, "-hide_banner", "-nostats", "-i", str(source),
                   "-af", f"loudnorm=I={target_lufs}:TP=-2.0:LRA=11:print_format=json",
                   "-f", "null", "-"])
    text = result.stderr.decode("utf-8", "replace")
    match = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", text, flags=re.S)
    if not match:
        return None
    try:
        measured = json.loads(match[-1])
    except json.JSONDecodeError:
        return None
    if any(key not in measured for key in _LOUDNORM_KEYS):
        return None
    # ffmpeg reports -inf for silence, which cannot be corrected towards.
    if any(str(measured[key]).lstrip("-").startswith("inf") for key in _LOUDNORM_KEYS):
        return None
    return {parameter: str(measured[key]) for key, parameter in _LOUDNORM_KEYS.items()}


def normalize_loudness(audio: bytes, *, target_lufs: float = -18.0) -> MasteringResult:
    """Bring one finished unit to a fixed loudness with a two-pass EBU R128 run.

    Two passes rather than one: a single streaming pass adapts as it goes,
    which makes a long narration breathe up and down. Measuring first and
    applying one constant correction keeps the reading's own dynamics.
    """
    if not audio:
        raise ValueError("Cannot normalise empty audio.")
    executable = ffmpeg_path()
    if executable is None:
        return MasteringResult(audio, False, "ffmpeg no está disponible: no se ajusta el volumen.")
    with tempfile.TemporaryDirectory(prefix="soni2-loudness-") as folder:
        root = Path(folder)
        source = root / "input.mp3"
        source.write_bytes(audio)
        measured = _measure_loudness(executable, source, target_lufs)
        if measured is None:
            return MasteringResult(audio, False,
                                   "No se pudo medir el volumen de este audio; se deja sin ajustar.")
        settings = ":".join(f"{key}={value}" for key, value in measured.items())
        destination = root / "output.mp3"
        result = _run([executable, "-hide_banner", "-v", "error", "-y", "-i", str(source),
                       "-af", f"loudnorm=I={target_lufs}:TP=-2.0:LRA=11:{settings}:linear=true",
                       # loudnorm works at 192 kHz; without this the encoder
                       # picks 48 kHz, and a chapter whose levelling was kept
                       # ended up in a different format from one where it was not.
                       "-ar", "44100",
                       "-c:a", "libmp3lame", "-q:a", "2", "-write_xing", "0", str(destination)])
        if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
            return MasteringResult(audio, False, "ffmpeg no pudo ajustar el volumen.")
        return MasteringResult(destination.read_bytes(), True,
                               f"Volumen normalizado a {target_lufs:g} LUFS (EBU R128).")


def measure_loudness_lufs(audio: bytes) -> float | None:
    """The integrated loudness of some audio, for checking the result."""
    executable = ffmpeg_path()
    if executable is None or not audio:
        return None
    with tempfile.TemporaryDirectory(prefix="soni2-measure-") as folder:
        source = Path(folder) / "input.mp3"
        source.write_bytes(audio)
        result = _run([executable, "-hide_banner", "-nostats", "-i", str(source),
                       "-af", "ebur128=framelog=quiet", "-f", "null", "-"])
        text = result.stderr.decode("utf-8", "replace")
        match = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", text)
        return float(match[-1]) if match else None


def duration_seconds(audio: bytes) -> float | None:
    """Exact length of some audio, for checking pauses, pacing and sync.

    MP3 is measured by counting its frames. ffprobe's format duration is only
    an estimate from the file size for a VBR stream without a Xing header,
    which is what the passages are encoded as, and in the Docker image (where
    ffprobe exists) those estimates put every SMIL clip of a chapter up to a
    quarter of a second away from its speech.
    """
    from app.audio_integrity import inspect_stream

    stream = inspect_stream(audio) if audio else None
    if stream and stream["frames"] and not set(stream["errors"]) - {"truncated_final_frame"}:
        return stream["duration_seconds"]
    probe = shutil.which("ffprobe")
    executable = probe or ffmpeg_path()
    if executable is None or not audio:
        return None
    with tempfile.TemporaryDirectory(prefix="soni2-duration-") as folder:
        source = Path(folder) / "input.mp3"
        source.write_bytes(audio)
        if probe:
            result = _run([probe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=noprint_wrappers=1:nokey=1", str(source)])
            text = result.stdout.decode("utf-8", "replace").strip()
            try:
                return float(text)
            except ValueError:
                return None
        # No ffprobe: decode and count the samples ffmpeg reports.
        result = _run([executable, "-hide_banner", "-nostats", "-i", str(source),
                       "-f", "null", "-"])
        text = result.stderr.decode("utf-8", "replace")
        match = re.findall(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)", text)
        if not match:
            return None
        hours, minutes, seconds = match[-1]
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


@dataclass(frozen=True)
class MasteringOptions:
    """How a finished book should be paced and levelled.

    The defaults follow common audiobook practice: around -18 LUFS is
    comfortable on phones and screen readers without clipping, a short pause
    between paragraphs, and a longer one between chapters so a listener who
    cannot see the structure still hears it.
    """

    enabled: bool = True
    target_lufs: float = -18.0
    paragraph_gap_ms: int = 350
    chapter_gap_ms: int = 900

    @property
    def active(self) -> bool:
        return self.enabled and available()


def master_chapter(passages: list[bytes], options: MasteringOptions) -> JoinedAudio:
    """One chapter: trimmed, paced between paragraphs, levelled as a whole.

    The returned ``clips`` are where each paragraph's speech sits in the
    chapter, which is what DAISY synchronises the text against.
    """
    if not options.active:
        return JoinedAudio(b"".join(passages), False,
                           "Pulido desactivado o ffmpeg no disponible.", [])
    joined = join_passages(passages, gap_ms=options.paragraph_gap_ms, final_gap_ms=0)
    if not joined.mastered:
        return joined
    levelled = normalize_loudness(joined.audio, target_lufs=options.target_lufs)
    # A linear loudness correction changes no boundary, so the measured clips
    # still hold. Anything that did change the length would invalidate them.
    before = duration_seconds(joined.audio)
    after = duration_seconds(levelled.audio)
    if levelled.mastered and before and after and abs(before - after) > 0.05:
        return JoinedAudio(joined.audio, True,
                           f"{joined.detail} El ajuste de volumen alteró la duración, "
                           "así que se conserva el audio sin ajustar para no romper la sincronización.",
                           joined.clips)
    detail = f"{joined.detail} {levelled.detail}".strip()
    # Pacing succeeded even if the level could not be measured, so the chapter
    # is still better than a raw concatenation.
    return JoinedAudio(levelled.audio, True, detail, joined.clips)


def master_book(chapters: list[bytes], options: MasteringOptions) -> MasteringResult:
    """The whole book from chapters that are already trimmed and levelled.

    They only need the longer between-chapter pause; normalising again would
    measure a level that is already correct.
    """
    if not options.active:
        return MasteringResult(b"".join(chapters), False,
                               "Pulido desactivado o ffmpeg no disponible.")
    if len(chapters) == 1:
        return MasteringResult(chapters[0], True, "Un solo capítulo: ya está pulido.")
    return join_passages(chapters, gap_ms=options.chapter_gap_ms, final_gap_ms=0)
