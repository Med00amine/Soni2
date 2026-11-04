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

A fourth step works inside each passage: **natural pauses**. The voice reads a
whole paragraph in one request and leaves only short gaps between its
sentences, which a listener who cannot see the page hears as one sentence
running into the next. The gaps the voice did leave are found in the audio,
matched to the punctuation of the text it read, and lengthened to a set pause
(see ``plan_pauses``). Nothing is narrated again: only silence is added, in the
middle of silence that is already there, so no word can be cut.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Anything below this is dead air rather than speech. Generous enough to catch
# the provider's padding, quiet enough to keep a softly spoken first syllable.
SILENCE_THRESHOLD_DB = -50
# A short fade across each join: an MP3 frame boundary is not a zero crossing,
# and the step between two clips is audible as a click without it.
JOIN_FADE_SECONDS = 0.012
# The PCM every passage is mastered in: what the trim filter already produces.
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2


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
    # For each passage, the pauses added inside it: ``(at, seconds)`` where
    # ``at`` is a time in the trimmed passage before anything was added. A time
    # measured in the provider's own clip (a recognizer's word timing) is moved
    # by the pauses before it; see ``shift_for_pauses``.
    pauses: list[list[tuple[float, float]]] = field(default_factory=list)


@dataclass(frozen=True)
class PauseOptions:
    """The pause after each kind of punctuation inside a passage, in milliseconds.

    A pause the voice already made that is at least this long is left alone;
    a shorter one is lengthened to it. ``min_silence_ms`` is the shortest gap
    that counts as a pause at all: shorter quiet stretches are the closure of a
    consonant inside a word, and adding silence there would cut the word.
    """

    enabled: bool = True
    sentence_ms: int = 700
    clause_ms: int = 450
    comma_ms: int = 300
    detect_db: float = -40.0
    min_silence_ms: int = 100

    def target(self, kind: str) -> float:
        return {"sentence": self.sentence_ms, "clause": self.clause_ms, "comma": self.comma_ms}[kind] / 1000


# Punctuation the voice pauses at, followed by any closing quote or bracket,
# then a space. The end of the passage is not here: its pause is the gap the
# passage is joined with.
_PAUSE_MARK = re.compile(r"(\.{3}|[.!?…]+|[;:]|[—–]|,)([\"'»”’)\]]*)(?=\s)")
_ABBREVIATION = re.compile(
    r"(?:^|[\s(«\"'])(?:sr|sra|srta|dr|dra|d|dª|ud|uds|pág|págs|núm|nº|aprox|av|etc|vol|fig|art|cap|prof|lic|ee\.uu)\.$"
    # An initial or a dotted abbreviation: "J. R.", "S.L.", "a.m.".
    r"|(?:^|[\s.(«\"'])[^\W\d_]\.$",
    re.IGNORECASE,
)


def pause_marks(text: str) -> list[tuple[float, str]]:
    """Where the voice should pause in ``text``: ``(fraction of the speech, kind)``.

    The fraction counts letters and digits only, which is what takes time to
    say; spaces and punctuation do not. Abbreviations ("Sr.") and numbers
    ("3.500") are not sentence ends.
    """
    spoken = [index for index, character in enumerate(text) if character.isalnum()]
    if not spoken:
        return []
    marks: list[tuple[float, str]] = []
    for match in _PAUSE_MARK.finditer(text):
        mark = match.group(1)
        start = match.start(1)
        if mark == "." and _ABBREVIATION.search(text[:start + 1]):
            continue
        kind = ("sentence" if mark[0] in ".!?…" else "comma" if mark == "," else "clause")
        before = sum(1 for index in spoken if index < start)
        if 0 < before < len(spoken):
            marks.append((before / len(spoken), kind))
    return marks


# Alignment costs, in units of "one tolerance away". Skipping a full stop the
# voice surely paused at is expensive; a comma it read straight through is not.
_SKIP_MARK = {"sentence": 1.0, "clause": 0.7, "comma": 0.5}


def plan_pauses(text: str, silences: list[tuple[float, float]], duration: float,
                options: PauseOptions) -> list[tuple[float, float]]:
    """Which of the voice's own pauses to lengthen, and by how much.

    ``silences`` are the quiet stretches found in the trimmed passage, in
    order. Each punctuation mark is expected at the point of the speech its
    position in the text suggests, and the marks and the silences are aligned
    in order (an edit distance where a mark may have no pause and a pause may
    have no mark): the voice may read through a comma, and may breathe where
    the text has none. Returns ``(at, seconds)``, where ``at`` is the middle of
    the matched silence: the added silence goes where there is silence already.
    """
    if not options.enabled or duration <= 0:
        return []
    marks = pause_marks(text)
    minimum = options.min_silence_ms / 1000
    edge = 0.05
    gaps = [(start, end) for start, end in silences
            if end - start >= minimum and start > edge and end < duration - edge]
    if not marks or not gaps:
        return []
    speech = max(0.001, duration - sum(end - start for start, end in gaps))

    def expected(fraction: float) -> float:
        # A point of the speech, placed on the passage's timeline by stepping
        # over the silences before it.
        target = fraction * speech
        spoken = 0.0
        previous = 0.0
        for start, end in gaps:
            if spoken + (start - previous) >= target:
                break
            spoken += start - previous
            previous = end
        return previous + (target - spoken)

    times = [expected(fraction) for fraction, _kind in marks]
    tolerance = max(0.6, 0.06 * duration)

    def skip_gap(length: float) -> float:
        # A long silence nobody accounts for is unlikely; a short one is a breath.
        return 0.2 + 3.0 * max(0.0, length - 0.2)

    def match(index: int, gap: tuple[float, float]) -> float | None:
        start, end = gap
        distance = abs(times[index] - (start + end) / 2) / tolerance
        if distance > 3:
            return None
        kind = marks[index][1]
        fit = 0.3 if kind == "sentence" and end - start < 0.15 else 0.0
        return distance + fit

    count_marks, count_gaps = len(marks), len(gaps)
    infinity = float("inf")
    cost = [[infinity] * (count_gaps + 1) for _ in range(count_marks + 1)]
    step: list[list[str]] = [[""] * (count_gaps + 1) for _ in range(count_marks + 1)]
    cost[0][0] = 0.0
    for i in range(count_marks + 1):
        for j in range(count_gaps + 1):
            if i == j == 0:
                continue
            best, how = infinity, ""
            if i and cost[i - 1][j] + _SKIP_MARK[marks[i - 1][1]] < best:
                best, how = cost[i - 1][j] + _SKIP_MARK[marks[i - 1][1]], "mark"
            if j and cost[i][j - 1] + skip_gap(gaps[j - 1][1] - gaps[j - 1][0]) < best:
                best, how = cost[i][j - 1] + skip_gap(gaps[j - 1][1] - gaps[j - 1][0]), "gap"
            if i and j:
                value = match(i - 1, gaps[j - 1])
                if value is not None and cost[i - 1][j - 1] + value < best:
                    best, how = cost[i - 1][j - 1] + value, "match"
            cost[i][j], step[i][j] = best, how
    pairs: list[tuple[int, int]] = []
    i, j = count_marks, count_gaps
    while i or j:
        how = step[i][j]
        if how == "match":
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif how == "mark":
            i -= 1
        else:
            j -= 1
    plan: list[tuple[float, float]] = []
    for mark_index, gap_index in sorted(pairs, key=lambda pair: pair[1]):
        start, end = gaps[gap_index]
        missing = options.target(marks[mark_index][1]) - (end - start)
        # Below 40 ms the difference is not heard; leave the voice's pause.
        if missing >= 0.04:
            plan.append((round((start + end) / 2, 4), round(missing, 4)))
    return plan


def shift_for_pauses(seconds: float, pauses: list[tuple[float, float]]) -> float:
    """A time in a passage before pauses were added, after they were."""
    return seconds + sum(added for at, added in pauses if at <= seconds)


def _silences_from_log(log: str) -> list[tuple[float, float]]:
    starts = [float(value) for value in re.findall(r"silence_start: (-?\d+(?:\.\d+)?)", log)]
    ends = [float(value) for value in re.findall(r"silence_end: (-?\d+(?:\.\d+)?)", log)]
    return [(max(0.0, start), end) for start, end in zip(starts, ends) if end > start]


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


def _trim_pause_and_pad(executable: str, source: Path, destination: Path, gap_seconds: float,
                       text: str, options: PauseOptions) -> tuple[str, list[tuple[float, float]] | None]:
    """Trim one passage, lengthen its pauses to match its punctuation, append its gap.

    The passage is decoded once to PCM with ffmpeg reporting its silences on
    the way; the added pauses are runs of digital silence spliced into the
    PCM in the middle of those silences, and the result is encoded as before.
    Returns ``(error, pauses)``; ``pauses`` is None for a passage too quiet to
    trim, which the caller keeps whole instead.
    """
    decode = _run([executable, "-hide_banner", "-nostats", "-i", str(source),
                   "-af", _trim_filter(0, trim=True)
                   + f",silencedetect=noise={options.detect_db:g}dB:d={options.min_silence_ms / 1000:.3f}",
                   "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1"])
    pcm = decode.stdout
    if decode.returncode != 0:
        return decode.stderr.decode("utf-8", "replace")[-400:] or "ffmpeg could not decode the passage", []
    pcm = pcm[:len(pcm) - len(pcm) % BYTES_PER_FRAME]
    duration = len(pcm) / (SAMPLE_RATE * BYTES_PER_FRAME)
    if duration < 0.05:
        return "", None
    plan = plan_pauses(text, _silences_from_log(decode.stderr.decode("utf-8", "replace")), duration, options)
    pieces: list[bytes] = []
    cursor = 0
    for at, added in plan:
        offset = min(len(pcm), round(at * SAMPLE_RATE) * BYTES_PER_FRAME)
        pieces.append(pcm[cursor:offset])
        pieces.append(bytes(round(added * SAMPLE_RATE) * BYTES_PER_FRAME))
        cursor = offset
    pieces.append(pcm[cursor:])
    pieces.append(bytes(round(gap_seconds * SAMPLE_RATE) * BYTES_PER_FRAME))
    encode = subprocess.run(
        [executable, "-hide_banner", "-v", "error", "-y", "-f", "s16le", "-ar", str(SAMPLE_RATE),
         "-ac", str(CHANNELS), "-i", "pipe:0", "-c:a", "libmp3lame", "-q:a", "2", "-write_xing", "0",
         str(destination)],
        input=b"".join(pieces), capture_output=True, timeout=1800, check=False)
    if encode.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        return encode.stderr.decode("utf-8", "replace")[:400] or "ffmpeg produced no audio", []
    return "", plan


def passage_gaps(count: int, gap_ms: int, final_gap_ms: int = 0) -> list[int]:
    """The same pause after every passage but the last."""
    if not count:
        return []
    return [gap_ms] * (count - 1) + [final_gap_ms]


def join_passages(
    segments: list[bytes], *, gap_ms: int = 0, final_gap_ms: int = 0,
    gaps_ms: list[int] | None = None, texts: list[str] | None = None, pauses: PauseOptions | None = None,
) -> JoinedAudio:
    """Join narrated passages with trimmed edges and deliberate pauses.

    Each passage is trimmed and padded on its own, so the position of every
    passage in the result is known exactly rather than estimated. DAISY players
    use those positions to follow the text while the audio plays, and a guess
    would drift further with every paragraph.

    ``gaps_ms`` gives each passage its own pause (by default ``gap_ms`` after
    every passage and ``final_gap_ms`` after the last). With ``texts`` (what
    each passage says) and ``pauses`` enabled, the pauses inside each passage
    are lengthened to match its punctuation too; the clips stay measured, so
    the synchronisation holds.
    """
    if not segments:
        raise ValueError("Cannot assemble an empty narration sequence.")
    executable = ffmpeg_path()
    if executable is None:
        return JoinedAudio(b"".join(segments), False,
                           "ffmpeg no está disponible: el audio se concatena sin pulir.", [])

    if gaps_ms is not None and len(gaps_ms) != len(segments):
        raise ValueError("Every passage needs its own pause.")
    gaps = [value / 1000 for value in (gaps_ms if gaps_ms is not None
                                       else passage_gaps(len(segments), gap_ms, final_gap_ms))]
    inner = pauses if pauses is not None and pauses.enabled and texts is not None else None
    if inner is not None and len(texts or []) != len(segments):
        raise ValueError("Every passage needs its text to place its pauses.")

    with tempfile.TemporaryDirectory(prefix="soni2-master-") as folder:
        root = Path(folder)
        prepared: list[Path] = []
        clips: list[tuple[float, float]] = []
        added_pauses: list[list[tuple[float, float]]] = []
        position = 0.0
        for index, (data, gap) in enumerate(zip(segments, gaps)):
            source = root / f"raw-{index:05d}.mp3"
            source.write_bytes(data)
            destination = root / f"cut-{index:05d}.mp3"
            plan: list[tuple[float, float]] | None = []
            text = (texts or [""] * len(segments))[index] if inner is not None else ""
            if inner is not None and text.strip():
                error, plan = _trim_pause_and_pad(executable, source, destination, gap, text, inner)
            else:
                error = _trim_and_pad(executable, source, destination, gap)
            if error:
                return JoinedAudio(b"".join(segments), False,
                                   f"ffmpeg no pudo pulir el audio: {error}", [])
            length = duration_seconds(destination.read_bytes()) if plan is not None else 0.0
            original = duration_seconds(data)
            if length is None:
                return JoinedAudio(b"".join(segments), False,
                                   "No se pudo medir la duración del audio pulido.", [])
            # A passage that is quiet throughout would be trimmed away entirely.
            # Keep the original rather than silently dropping a paragraph of the
            # book; only its pause is added.
            speech = length - gap
            if plan is None or (original and speech < min(0.05, original * 0.1)):
                plan = []
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
            added_pauses.append(plan)
            position += length
            prepared.append(destination)

        added = sum(len(plan) for plan in added_pauses)
        detail_pauses = f" {added} pausas internas alargadas." if inner is not None else ""
        if len(prepared) == 1:
            return JoinedAudio(prepared[0].read_bytes(), True,
                               f"Bordes recortados y pausa aplicada.{detail_pauses}", clips, added_pauses)

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
        described = (f"pausas de {gap_ms} ms" if gaps_ms is None
                     else f"pausas de {min(gaps_ms[:-1] or gaps_ms)}–{max(gaps_ms[:-1] or gaps_ms)} ms")
        return JoinedAudio(joined.read_bytes(), True,
                           f"Bordes recortados y {described} aplicadas.{detail_pauses}", clips, added_pauses)


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
    comfortable on phones and screen readers without clipping, a pause of
    about a second between paragraphs, a longer one between chapters so a
    listener who cannot see the structure still hears it, and inside each
    paragraph a pause after every sentence and a shorter one at commas.
    """

    enabled: bool = True
    target_lufs: float = -18.0
    paragraph_gap_ms: int = 1000
    chapter_gap_ms: int = 2000
    pauses: PauseOptions = field(default_factory=PauseOptions)

    @property
    def active(self) -> bool:
        return self.enabled and available()

    def signature(self) -> str:
        """Everything here that changes the mastered audio, for cache identities."""
        return json.dumps({"version": 2, **asdict(self)}, sort_keys=True)

    def gaps_for(self, continues: list[bool] | None, count: int) -> list[int]:
        """The pause after each passage of a chapter.

        A long paragraph is narrated in several passages; between two parts of
        one paragraph the pause is a sentence's, not a paragraph's.
        """
        gaps = passage_gaps(count, self.paragraph_gap_ms, 0)
        if continues:
            sentence = self.pauses.sentence_ms if self.pauses.enabled else self.paragraph_gap_ms
            for index, follows in enumerate(continues[:count - 1]):
                if follows:
                    gaps[index] = min(sentence, self.paragraph_gap_ms)
        return gaps


def master_chapter(passages: list[bytes], options: MasteringOptions, *, texts: list[str] | None = None,
                   continues: list[bool] | None = None) -> JoinedAudio:
    """One chapter: trimmed, paced between and inside paragraphs, levelled as a whole.

    ``texts`` is what each passage says (its synthesis text), which places the
    pauses inside it; ``continues[i]`` is true when passage ``i + 1`` carries
    on the same paragraph. The returned ``clips`` are where each passage's
    speech sits in the chapter, which is what DAISY synchronises the text
    against, and ``pauses`` what was added inside each passage.
    """
    if not options.active:
        return JoinedAudio(b"".join(passages), False,
                           "Pulido desactivado o ffmpeg no disponible.", [])
    joined = join_passages(passages, gaps_ms=options.gaps_for(continues, len(passages)),
                           texts=texts, pauses=options.pauses if texts is not None else None)
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
                           joined.clips, joined.pauses)
    detail = f"{joined.detail} {levelled.detail}".strip()
    # Pacing succeeded even if the level could not be measured, so the chapter
    # is still better than a raw concatenation.
    return JoinedAudio(levelled.audio, True, detail, joined.clips, joined.pauses)


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
