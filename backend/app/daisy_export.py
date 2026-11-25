from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from app import audio_master
from app.audio_cache import link_or_copy


logger = logging.getLogger(__name__)

DTBOOK_NS = "http://www.daisy.org/z3986/2005/dtbook/"
SMIL_NS = "http://www.w3.org/2001/SMIL20/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
RESOURCE_NS = "http://www.daisy.org/z3986/2005/resource/"
OPF_NS = "http://openebook.org/namespaces/oeb-package/1.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


# Already-compressed payloads. Deflating MP3 costs a great deal of time and
# saves about 2%: on a book-sized delivery that was minutes of pure waste, so
# these are stored and only the text is compressed.
_STORED_SUFFIXES = {".mp3", ".mp4", ".m4a", ".ogg", ".opus", ".zip", ".png", ".jpg", ".jpeg", ".gif"}


def archive_compression(path: str) -> int:
    """The compression to use for one entry, by file type."""
    suffix = PurePosixPath(path).suffix.lower()
    return zipfile.ZIP_STORED if suffix in _STORED_SUFFIXES else zipfile.ZIP_DEFLATED


def add_to_archive(archive: zipfile.ZipFile, source: Path, name: str) -> None:
    """Write one file into the archive with the right compression for its type."""
    archive.write(source, name, compress_type=archive_compression(name))


class AudioFiles(Sequence[bytes]):
    """Audio segments on disk, read only when they are used.

    A production job hands the package its units this way: when every
    chapter is already built, the units are counted but never read, and a
    two-hour book is not held in memory to be ignored.
    """

    def __init__(self, paths: list[Path]) -> None:
        self.paths = list(paths)

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, slice):
            return [path.read_bytes() for path in self.paths[index]]
        return self.paths[index].read_bytes()


def audio_bytes(part: bytes | Path) -> bytes:
    """The bytes of audio given either as bytes or as a file."""
    return part.read_bytes() if isinstance(part, Path) else part


# The DTBook's style sheet: the Z39.86 committee's dtbook.2005.basic.css, the
# file ONCE sent and its own books carry, copied unchanged (ONCE requirement H).
# Z39.86-2005 §12.2 says the declaration names a medium; the media attribute is
# a setting, off by default so the line is exactly the one ONCE asked for.
DTBOOK_STYLESHEET = "dtbookbasic.css"
STYLESHEET_SOURCE = Path(__file__).with_name("package_files") / DTBOOK_STYLESHEET
LAYOUTS = ("nested", "flat")
# Who made the package: dtb:producer and dtb:generator, and the publisher of a
# book whose publisher is not given (dc:Publisher is required, Z39.86-2005 §8.2).
PRODUCER = "Soni2"


@dataclass(frozen=True)
class PackagePaths:
    """Where each kind of file sits in the package, and how files refer to each other.

    ``nested`` (the layout delivered until now) keeps the SMIL files in
    ``smil/`` and the audio in ``audio/``; ``flat`` puts every file beside the
    OPF, no folder at all, which is what older DAISY players handle best.
    References are always relative to the file that makes them.
    """

    layout: str = "nested"

    @property
    def flat(self) -> bool:
        return self.layout == "flat"

    def audio_href(self, name: str) -> str:
        """From the package root (OPF, NCX, DTBook)."""
        return name if self.flat else f"audio/{name}"

    def smil_href(self, name: str) -> str:
        return name if self.flat else f"smil/{name}"

    def audio_from_smil(self, name: str) -> str:
        return name if self.flat else f"../audio/{name}"

    def root_from_smil(self, name: str) -> str:
        return name if self.flat else f"../{name}"

    def image_href(self, name: str) -> str:
        """A photograph, from the package root (the DTBook names it from there)."""
        return name if self.flat else f"images/{name}"

    def audio_file(self, root: Path, name: str) -> Path:
        return root / self.audio_href(name)

    def smil_file(self, root: Path, name: str) -> Path:
        return root / self.smil_href(name)


def _indent(element: ET.Element, level: int = 0, space: str = "  ") -> None:
    """Indent the element tree two spaces per level, never inside text.

    An element that holds text (a paragraph with a note reference in it) is
    mixed content: whitespace added there would become part of what it says,
    so it is written exactly as built.
    """
    children = list(element)
    if not children or (element.text or "").strip() or any((child.tail or "").strip() for child in children):
        return
    inner = "\n" + space * (level + 1)
    element.text = inner
    for child in children:
        _indent(child, level + 1, space)
        child.tail = inner
    children[-1].tail = "\n" + space * level


def _with_note_refs(element: ET.Element, text: str, refs: list[dict] | None, note_ids: dict[int, str]) -> int:
    """Write ``text`` into ``element`` with its note references back in place.

    Each reference is a DTBook ``noteref`` pointing at its note. A reference
    whose note is not in this section, or whose surrounding text is no longer
    the same (the paragraph was edited), is left out rather than misplaced.
    Returns how many were placed.
    """
    placed: list[tuple[int, str, str]] = []
    for ref in sorted(refs or [], key=lambda item: item.get("at", -1)):
        at, label, before = int(ref.get("at", -1)), str(ref.get("label", "")), ref.get("before", "")
        number = int(label) if label.isdigit() else None
        if number not in note_ids or not 0 <= at <= len(text):
            continue
        context = before[-at:] if at else ""
        if text[at - len(context):at] != context:
            continue
        if placed and at < placed[-1][0]:
            continue
        placed.append((at, label, note_ids[number]))
    element.text = text[:placed[0][0]] if placed else text
    for index, (at, label, note_id) in enumerate(placed):
        noteref = ET.SubElement(element, _q(DTBOOK_NS, "noteref"), {"idref": f"#{note_id}", "class": "once-noteref"})
        noteref.text = label
        following = placed[index + 1][0] if index + 1 < len(placed) else len(text)
        noteref.tail = text[at:following]
    return len(placed)


def package_layout(value: str | None) -> str:
    layout = (value or "nested").strip().lower()
    if layout not in LAYOUTS:
        raise ValueError(f"Unknown DAISY package layout «{value}»: use nested or flat.")
    return layout


def _write_xml(path: Path, root: ET.Element, doctype: str | None = None, default_namespace: str | None = None,
               *, stylesheet: str | None = None, stylesheet_media: str = "") -> None:
    """Write one package document: declaration, DOCTYPE, indented body.

    Every XML file of the package goes through here, so all of them are
    indented the same way (two spaces per level). Indentation only adds
    whitespace between elements, never inside the text of a heading or a
    paragraph; the DTD validation and the completeness checks run on the
    indented files. ``stylesheet`` adds one ``xml-stylesheet`` processing
    instruction right after the XML declaration, before the DOCTYPE: where
    ONCE asked for it ("tras la codificación") and where DAISY Pipeline puts it.
    """
    # The default namespace is given per document. ElementTree's global
    # register_namespace("") is overwritten by every SMIL file written in the
    # loop, which made the DTBook come out as <ns0:dtbook><ns0:p>…: legal XML,
    # but invalid against the DTD and unreadable for DTD-based DAISY readers.
    if default_namespace:
        # ElementTree's own default_namespace option rejects unqualified
        # attributes (id, src), so the document's own tags are written
        # unprefixed and the namespace declared once on the root.
        prefix = "{" + default_namespace + "}"
        for element in root.iter():
            if isinstance(element.tag, str) and element.tag.startswith(prefix):
                element.tag = element.tag[len(prefix):]
        root.set("xmlns", default_namespace)
    _indent(root)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    if default_namespace == OPF_NS:
        # <dc-metadata> declares the dc namespace itself, as the OEB 1.2 DTD
        # requires; ElementTree would also put it on <package>, where the DTD
        # does not allow it.
        body = body.replace(f' xmlns:dc="{DC_NS}"', "", 1) if body.count(f'xmlns:dc="{DC_NS}"') > 1 else body
    prefix = '<?xml version="1.0" encoding="UTF-8"?>\n'
    if stylesheet:
        media = f' media="{stylesheet_media}"' if stylesheet_media else ""
        prefix += f'<?xml-stylesheet href="{stylesheet}" type="text/css"{media}?>\n'
    if doctype:
        prefix += f"{doctype}\n"
    path.write_text(prefix + body + "\n", encoding="utf-8")


def join_mp3(segments: list[bytes], *, use_ffmpeg: bool = True) -> bytes:
    """Join MP3 segments into one clean stream, through ffmpeg when available.

    Exposed so the production job can build one chapter's audio on its own and
    reuse it when that chapter has not changed.
    """
    return _join_mp3(segments, use_ffmpeg=use_ffmpeg and bool(shutil.which("ffmpeg")))


def _mp3_stream_format(data: bytes) -> tuple[int, int, int, int] | None:
    """(MPEG version, layer, sample rate, channel mode) of the first audio frame."""
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        offset = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | data[9] & 0x7F)
    limit = min(len(data) - 4, offset + 65536)
    while offset < limit:
        frame = _mp3_frame(data, offset)
        if frame and _mp3_frame(data, offset + frame[0]):
            return ((data[offset + 1] >> 3) & 0x3, (data[offset + 1] >> 1) & 0x3,
                    (data[offset + 2] >> 2) & 0x3, data[offset + 3] >> 6)
        offset += 1
    return None


def _audio_frames(data: bytes) -> bytes:
    """Only the MPEG audio frames of one MP3 file, for joining without ffmpeg.

    A provider's file starts with an ID3 tag and an Xing/Info frame that states
    that file's own length; glued into the middle of a chapter they make
    players misreport the length and stumble at the join. Copying the frames
    alone gives one clean stream. A payload that is not MPEG audio, or the mock
    provider's self-describing clip, is returned unchanged.
    """
    if b"SONI2-MOCK-TEXT:" in data[:40]:
        return data
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        offset = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | data[9] & 0x7F)
    frames: list[bytes] = []
    while offset < len(data) - 4:
        frame = _mp3_frame(data, offset)
        if frame and (offset + frame[0] >= len(data) or _mp3_frame(data, offset + frame[0])):
            payload = data[offset:offset + frame[0]]
            # The length header of a VBR/CBR file is itself a silent frame
            # whose payload names "Xing" or "Info": leave it out.
            if not (not frames and (b"Xing" in payload[:64] or b"Info" in payload[:64])):
                frames.append(payload)
            offset += frame[0]
        else:
            offset += 1
    return b"".join(frames) if frames else data


def _join_mp3(segments: list[bytes], *, use_ffmpeg: bool) -> bytes:
    """Join MP3 files; with ffmpeg the result is a single, clean stream.

    Gluing the files byte by byte (the fallback without ffmpeg) leaves every
    file's ID3 tag and length header in the middle of the audio: players then
    report the first paragraph's length for a whole chapter and can stumble at
    the joins. Files that share one stream format, as one voice's always do,
    are copied frame for frame, which is fast and loses nothing; anything else
    is re-encoded.
    """
    if not use_ffmpeg:
        return b"".join(_audio_frames(segment) for segment in segments)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is required for safe production MP3 assembly.")
    if not segments:
        raise ValueError("Cannot assemble an empty MP3 sequence.")
    with tempfile.TemporaryDirectory(prefix="soni2-mp3-") as folder:
        root = Path(folder)
        names = []
        for index, data in enumerate(segments):
            path = root / f"segment-{index:05d}.mp3"
            path.write_bytes(data)
            names.append(path)
        manifest = root / "segments.ffconcat"
        manifest.write_text("ffconcat version 1.0\n" + "".join(f"file '{path.as_posix()}'\n" for path in names), encoding="utf-8")
        target = root / "joined.mp3"
        formats = {_mp3_stream_format(segment) for segment in segments}
        codec = (["-c:a", "copy"] if len(formats) == 1 and None not in formats
                 else ["-c:a", "libmp3lame", "-q:a", "3"])
        result = subprocess.run(
            [executable, "-hide_banner", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
             "-map", "0:a:0", *codec, "-write_xing", "0", str(target)],
            capture_output=True, timeout=300, check=False,
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg could not assemble MP3 audio: {result.stderr.decode('utf-8', 'replace')[:300]}")
        return target.read_bytes()


# MPEG audio frame tables, indexed as in the frame header.
_MP3_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame(data: bytes, offset: int) -> tuple[int, int, int] | None:
    """(frame length, samples, sample rate) of a valid frame header at offset."""
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
    if layer == 1:
        return (12 * bitrate // rate + padding) * 4, 384, rate
    samples = 1152 if layer == 2 or version == 1 else 576
    return samples // 8 * bitrate // rate + padding, samples, rate


def mp3_duration(data: bytes) -> float:
    """Playing time of an MP3 in seconds, counted from its frame headers.

    Counting frames is exact for constant and variable bitrate alike and needs
    no external tool. Bytes that are not MPEG audio (tags, or the placeholder
    payloads some tests use) are skipped; a payload with no frames at all is
    estimated at 128 kbit/s, and at least a tenth of a second, so it still gets
    a clip that survives rounding to milliseconds.
    """
    offset = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | data[9] & 0x7F
        offset = 10 + size
    seconds = 0.0
    frames = 0
    while offset < len(data) - 4:
        frame = _mp3_frame(data, offset)
        # Accept a header only when the next frame also lines up, so a stray
        # 0xFF byte inside a tag is not counted as audio.
        if frame and (offset + frame[0] >= len(data) or _mp3_frame(data, offset + frame[0])):
            seconds += frame[1] / frame[2]
            frames += 1
            offset += frame[0]
        else:
            offset += 1
    return seconds if frames else max(0.1, len(data) * 8 / 128_000)


def first_frame_estimate(head: bytes, size: int) -> tuple[float | None, bool]:
    """How long a tool that trusts an MP3's first frame says it is: ``(seconds, vbr)``.

    javazoom, the MP3 reader of ZedVal and DAISY Pipeline 1, reads the first
    frame after any ID3v2 tag. A Xing header there gives the frame count
    (``vbr`` True); without one the file is taken as constant bitrate and its
    whole size, tags included, is divided by that frame's bitrate. ``head`` is
    the start of the file (a few hundred kilobytes are plenty), ``size`` its
    length. ``None`` when no MPEG audio frame is found.
    """
    offset = 0
    if head[:3] == b"ID3" and len(head) >= 10:
        offset = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | head[9] & 0x7F)
    while offset < len(head) - 4:
        frame = _mp3_frame(head, offset)
        if frame and (offset + frame[0] >= len(head) or _mp3_frame(head, offset + frame[0])):
            break
        offset += 1
    else:
        return None, False
    length, samples, rate = frame
    version = 1 if (head[offset + 1] >> 3) & 0x3 == 3 else 2
    layer = 4 - ((head[offset + 1] >> 1) & 0x3)
    bitrate = _MP3_BITRATES[(version, layer)][head[offset + 2] >> 4] * 1000
    payload = head[offset:offset + length]
    xing = payload.find(b"Xing")
    if xing >= 0 and len(payload) >= xing + 12:
        if int.from_bytes(payload[xing + 4:xing + 8], "big") & 0x1:
            return int.from_bytes(payload[xing + 8:xing + 12], "big") * samples / rate, True
        return None, True
    return size / (bitrate // 8), False


def playable_length(data: bytes) -> float:
    """How long an audio file is for its SMIL clips, in seconds.

    The frames counted, unless a validator that trusts the first frame
    (ZedVal: size over bitrate) reads a constant-bitrate file a little
    shorter; then that, so no clipEnd can ever fall "after the end of the
    file" for it. A file it misreads by more than a frame or two is not
    constant bitrate, and the timing checks say so instead.
    """
    real = mp3_duration(data)
    estimate, vbr = first_frame_estimate(data[:262144], len(data))
    if estimate is not None and not vbr and real - 0.05 < estimate < real:
        return estimate
    return real


def _clock(seconds: float) -> str:
    """A SMIL full clock value, e.g. 0:01:02.345."""
    return _clock_ms(max(0, round(seconds * 1000)))


def _clock_ms(millis: int) -> str:
    """A SMIL full clock value from whole milliseconds."""
    hours, rest = divmod(max(0, int(millis)), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return f"{hours}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


def smil_boundaries(clips: list[tuple[float, float]], duration: float) -> list[tuple[int, int]]:
    """The SMIL clip of each passage of one audio file, in whole milliseconds.

    ``clips`` are where each passage's speech sits in the file. A SMIL file is
    played clip by clip, so a stretch no clip covers is never heard: the
    pauses between paragraphs and the silence at the edges of the file would
    be skipped, and the SMIL file would be shorter than its audio, which is
    what made ``dtb:totalElapsedTime`` and ``dtb:totalTime`` disagree with the
    clips. So the clips tile the whole file: the first begins at 0 (with the
    file's opening silence), each one ends where the next passage's speech
    begins (it keeps the pause after it), and the last ends at the end of the
    file, rounded down so no clipEnd can exceed the audio (Z39.86-2005 §7.3).
    """
    total = int(duration * 1000)  # floor: never past the real end
    count = len(clips)
    if not count:
        return []
    if total < count:
        raise ValueError("The audio file is too short to hold one clip per passage.")
    starts = [0] + [int(round(begin * 1000)) for begin, _end in clips[1:]]
    for index in range(1, count):
        # Strictly increasing, and leaving at least a millisecond for every clip after it.
        starts[index] = min(max(starts[index], starts[index - 1] + 1), total - (count - index))
    return [(start, end) for start, end in zip(starts, starts[1:] + [total])]


def measure_segment_clips(segments: list[bytes], chapter_audio: bytes) -> list[tuple[float, float]]:
    """Where each segment sits inside its chapter's joined audio.

    Durations are measured per segment, then scaled to the joined file's real
    length: re-encoding shifts every boundary by a few milliseconds, and
    spreading that across the chapter keeps the last sentence from drifting.
    """
    lengths = [mp3_duration(segment) for segment in segments]
    measured = sum(lengths)
    total = mp3_duration(chapter_audio)
    scale = total / measured if measured and total else 1.0
    clips: list[tuple[float, float]] = []
    position = 0.0
    for length in lengths:
        end = position + length * scale
        clips.append((position, end))
        position = end
    return clips


def package_basename(title: str) -> str:
    """The book's own name for its package files: «El faro» -> El_faro.

    Accents are folded and anything but letters, digits, dots and hyphens
    becomes an underscore. The names appear inside SMIL and OPF references,
    where spaces and non-ASCII characters break some DAISY players.
    """
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9.-]+", "_", ascii_title).strip("._-")[:80].rstrip("._-")
    return name or "libro"


def book_audio_name(title: str) -> str:
    """The complete audiobook's file name, delivered beside the package: «El faro» -> El_faro.mp3."""
    base = package_basename(title)
    # A book titled like a chapter file would read as one of them.
    return f"{base}-completo.mp3" if re.fullmatch(r"chapter-\d{3}", base) else f"{base}.mp3"


def _group_long_paragraph(text: str, max_chars: int = 800) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]
    if not sentences:
        return []

    groups: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            boundary = sentence.rfind(" ", 0, max_chars + 1)
            if boundary < max_chars // 2:
                boundary = max_chars
            groups.append(sentence[:boundary].strip())
            sentence = sentence[boundary:].strip()
        if current and len(current) + len(sentence) + 1 > max_chars:
            groups.append(current)
            current = ""
        current = f"{current} {sentence}".strip()
    if current:
        groups.append(current)
    return groups


def make_paragraphs(text: str) -> list[str]:
    blocks = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n\s*\n", text)]
    paragraphs = [part for block in blocks if block for part in _group_long_paragraph(block)]
    if not paragraphs and text.strip():
        paragraphs = _group_long_paragraph(re.sub(r"\s+", " ", text).strip())
    return paragraphs


SMIL_DOCTYPE = ('<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-2//EN" '
                '"http://www.daisy.org/z3986/2005/dtbsmil-2005-2.dtd">')


def _smil_head(smil: ET.Element, book_id: str, title: str, elapsed_ms: int) -> ET.Element:
    """The metadata Z39.86-2005 requires in every SMIL file.

    ``elapsed_ms`` is the playing time of every SMIL file before this one,
    summed from their clips in whole milliseconds, so it agrees exactly with
    what a validator adds up from the clips themselves.
    """
    head = ET.SubElement(smil, _q(SMIL_NS, "head"))
    for name, content in (("dtb:uid", book_id), ("dtb:generator", "Soni2"),
                          ("dtb:totalElapsedTime", _clock_ms(elapsed_ms)), ("dc:Title", title)):
        ET.SubElement(head, _q(SMIL_NS, "meta"), {"name": name, "content": content})
    return head


# Skippable structures (Z39.86-2005 §7.4.3): page numbers and notes are time
# containers with a customTest, declared in each SMIL file that uses them and
# gathered in the NCX head (§8.4.4). A page number is a silent marker, so it is
# skipped by default; a note is read in place ("Nota 1." … "Fin de nota 1."),
# so it plays by default, and a listener can turn either off.
CUSTOM_TESTS = {
    "pagenum": {"defaultState": "false", "override": "visible", "bookStruct": "PAGE_NUMBER"},
    "note": {"defaultState": "true", "override": "visible", "bookStruct": "NOTE"},
}
# The navigation class of each kind of section in the ONCE structure.
SECTION_CLASSES = {"once_title": "once-title", "once_bibliographic": "once-bibliographic",
                   "once_index": "once-index", "chapter": "chapter", "subchapter": "subchapter",
                   "photo_insert": "photo-insert"}
INITIAL_SECTION_KINDS = {"once_title", "once_bibliographic", "once_index"}


def _declare_custom_tests(head: ET.Element, tests: set[str]) -> None:
    if not tests:
        return
    attributes = ET.SubElement(head, _q(SMIL_NS, "customAttributes"))
    for test in sorted(tests):
        options = CUSTOM_TESTS[test]
        ET.SubElement(attributes, _q(SMIL_NS, "customTest"),
                      {"id": test, "defaultState": options["defaultState"], "override": options["override"]})


_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def page_kind(label: str) -> tuple[str, int | None]:
    """(DAISY page type, numeric value) of a page label: "12" normal, "iv" front, other special."""
    text = label.strip()
    if re.fullmatch(r"\d{1,6}", text):
        return "normal", int(text)
    if re.fullmatch(r"[ivxlcdm]{1,8}", text, flags=re.I):
        total, previous = 0, 0
        for character in reversed(text.lower()):
            value = _ROMAN[character]
            total += value if value >= previous else -value
            previous = max(previous, value)
        return "front", total if total > 0 else None
    return "special", None


def build_daisy_package(
    title: str,
    paragraphs: list[str],
    audio_segments: list[bytes],
    output_dir: Path,
    *,
    chapters: list[tuple[str, list[str]]] | None = None,
    author: str = "",
    edition: str = "",
    publication_year: str = "",
    publisher: str = "",
    front_matter: list[tuple[str, str, bytes, str]] | None = None,
    chapter_audio: Sequence[bytes | Path] | None = None,
    chapter_clips: list[list[tuple[float, float]]] | None = None,
    use_ffmpeg: bool = False,
    clips_out: list[tuple[str, float, float]] | None = None,
    chapter_starts_out: list[float] | None = None,
    mastering: audio_master.MasteringOptions | None = None,
    layout: list[dict] | None = None,
    identifiers: list[tuple[str, str]] | None = None,
    segment_texts: list[str] | None = None,
    segment_continues: list[bool] | None = None,
    on_chapter: Callable[[int, int], None] | None = None,
    package_layout_name: str = "nested",
    stylesheet: str | None = DTBOOK_STYLESHEET,
    stylesheet_media: str = "",
    images: dict[int, tuple[bytes, str, str]] | None = None,
    identifier: str | None = None,
    book_audio_path: Path | None = None,
    dtb_publisher: str = "",
    production_date: str | None = None,
    narrator: str = "",
    source_isbn: str = "",
) -> Path:
    """Write a DAISY 3 package whose files carry the book's own name.

    Audio lives once per chapter, directly in ``audio/``: ``audio/chapter-001.mp3``,
    ``audio/chapter-002.mp3``…; each paragraph's SMIL entry points at its
    stretch of that file with clipBegin/clipEnd rather than at a separate
    per-paragraph MP3. When ``clips_out`` is given it receives, for every
    segment in order, the path of its chapter file relative to ``audio/`` and
    the clip's start and end seconds.

    ``segment_texts`` (what each segment says) and ``segment_continues``
    (segment ``i + 1`` carries on the same paragraph) let mastering place the
    pauses inside and between paragraphs when the chapters are built here.

    ``layout`` (one entry per chapter) gives the ONCE structure: each entry's
    ``kind`` and ``level``, and for every paragraph its ``role`` ("heading",
    "text", "note", "closing", …), the ``page_breaks`` that begin at it and its
    ``note`` number. The initial files then sit in the DTBook front matter,
    subchapters nest as ``level2``, page numbers become silent markers and
    notes skippable ``note`` structures. Without it the package is written
    exactly as before. ``identifiers`` adds ``(scheme, value)`` identifiers
    such as the ABSYS number to the package metadata.

    ``chapter_audio`` may give each chapter as a file: a chapter already in its
    delivery form is then linked into the package rather than read and written
    again, and ``audio_segments`` (an ``AudioFiles``) is only read for a chapter
    built here. Only one chapter is ever held in memory.

    ``package_layout_name`` is ``nested`` (``audio/`` and ``smil/``) or
    ``flat`` (every file beside the OPF). ``stylesheet`` is the CSS the DTBook
    declares and the package carries (``None``: none); ``stylesheet_media``
    adds the media attribute to its declaration. ``images`` are the image
    files of the photographs the layout announces (number -> bytes,
    extension, media type), written as they are into ``images/``.

    ``identifier`` is the book's ``dtb:uid``: give the same one to every build
    of the same book, so a corrected version is the same book to a player
    (a random one is made otherwise). ``book_audio_path`` is where the
    complete audiobook, the chapter files one after the other, is written: it
    is not part of the DAISY file set (nothing in the book plays it, and
    Z39.86 allows no such file in the manifest), so it lives outside the
    package; ``None`` builds none.

    Metadata follows Z39.86-2005 §8.2: ``dc:Publisher`` and ``dc:Date`` are the
    publisher and date of this DAISY book (``dtb_publisher``, and
    ``production_date`` in ISO 8601, today by default), both required; the
    printed book's ``publisher``, ``publication_year`` and ``edition`` go to
    ``dtb:sourcePublisher``, ``dtb:sourceDate`` and ``dtb:sourceEdition``, and
    its ``source_isbn`` to ``dc:Source``. ``narrator`` names the voice.
    """
    chapters = chapters or [(title, paragraphs)]
    paragraphs = [paragraph for _, body in chapters for paragraph in body]
    if not paragraphs or len(paragraphs) != len(audio_segments):
        raise ValueError("Text and audio segments do not match.")
    if layout is not None:
        if len(layout) != len(chapters):
            raise ValueError("The structure does not describe every chapter.")
        if any(len(entry.get("units") or []) != len(body) for entry, (_title, body) in zip(layout, chapters)):
            raise ValueError("The structure does not describe every paragraph.")
        if front_matter:
            raise ValueError("The ONCE structure carries its own initial files; front matter cannot be added too.")

    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("DAISY output directory must be empty before package generation.")
    paths = PackagePaths(package_layout(package_layout_name))
    output_dir.mkdir(parents=True, exist_ok=True)
    if not paths.flat:
        (output_dir / "audio").mkdir(exist_ok=True)
        (output_dir / "smil").mkdir(exist_ok=True)
    book_id = (identifier or "").strip() or f"soni2-{uuid.uuid4()}"
    base = package_basename(title)
    dtbook_name, ncx_name, opf_name, res_name = (f"{base}.{ext}" for ext in ("xml", "ncx", "opf", "res"))
    front_matter = front_matter or []
    front_names: list[str] = []
    for _label, _text, _audio, filename in front_matter:
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".mp3" or filename in front_names:
            raise ValueError("Front-matter audio filenames must be unique MP3 filenames without directories.")
        front_names.append(filename)
    # One final audio file per chapter, all side by side in audio/, in reading
    # order: chapter-001.mp3, chapter-002.mp3… Earlier revisions wrote each
    # chapter three times and each segment twice, then gave every chapter a
    # folder of its own; every byte of audio now has exactly one home, and a
    # player or a person finds all the chapters in one place.
    chapter_audio_paths = [f"chapter-{index:03d}.mp3" for index in range(1, len(chapters) + 1)]
    if chapter_audio is not None and len(chapter_audio) != len(chapters):
        raise ValueError("Chapter audio count does not match chapter count.")
    if set(front_names).intersection(chapter_audio_paths):
        raise ValueError("Front-matter audio filenames cannot reuse chapter audio filenames.")
    if len(set(chapter_audio_paths)) != len(chapter_audio_paths):
        raise ValueError("Two chapters cannot share one audio file name.")
    if segment_texts is not None and len(segment_texts) != len(audio_segments):
        raise ValueError("Every segment needs its text to place its pauses.")
    # Each chapter's file in the package, in order: the book is joined from them.
    chapter_audio_files: list[Path] = []
    # (chapter audio path, clip start, clip end) for every segment, in order:
    # where each passage's speech sits, for reports and fault locations.
    segment_clips: list[tuple[str, float, float]] = []
    # The same segments as their SMIL clips: whole milliseconds that tile each
    # chapter file from its first sample to its last (see smil_boundaries).
    smil_clips: list[tuple[str, int, int]] = []
    # Each chapter file's SMIL playing time, in milliseconds.
    chapter_ms: list[int] = []
    # Every delivered file gets the silence at its edges and the constant
    # bitrate (audio_master.finish_file), whenever ffmpeg may be used. Only
    # what is missing is added, so chapters a production job already finished
    # pass through unchanged.
    delivery = mastering or audio_master.MasteringOptions()
    finishing = (use_ffmpeg or bool(mastering and mastering.active)) and audio_master.available()
    segment_offset = 0
    for chapter_index, (relative_path, (_chapter_title, chapter_body)) in enumerate(
        zip(chapter_audio_paths, chapters, strict=True)
    ):
        if on_chapter:
            on_chapter(chapter_index, len(chapters))
        count = len(chapter_body)
        first = segment_offset

        def chapter_segments() -> list[bytes]:
            return list(audio_segments[first:first + count])

        source: Path | None = None
        if chapter_audio is not None:
            # Already-built chapter audio: its clips come with it.
            given = chapter_audio[chapter_index]
            source = given if isinstance(given, Path) else None
            audio_content = audio_bytes(given)
            clips = (chapter_clips[chapter_index] if chapter_clips is not None
                     else measure_segment_clips(chapter_segments(), audio_content))
        elif mastering is not None and mastering.active:
            # Trimmed, paced, levelled and finished, with the real position of
            # every paragraph measured rather than estimated.
            texts = (segment_texts[segment_offset:segment_offset + count]
                     if segment_texts is not None else None)
            continues = (segment_continues[segment_offset:segment_offset + count]
                         if segment_continues is not None else None)
            segments_here = chapter_segments()
            mastered = audio_master.master_chapter(segments_here, mastering, texts=texts, continues=continues)
            audio_content = mastered.audio
            clips = mastered.clips or measure_segment_clips(segments_here, audio_content)
            del segments_here
        else:
            segments_here = chapter_segments()
            audio_content = _join_mp3(segments_here, use_ffmpeg=use_ffmpeg)
            clips = measure_segment_clips(segments_here, audio_content)
            del segments_here
        if finishing:
            finished = audio_master.finish_file(audio_content, delivery)
            if finished.finished:
                if finished.audio is not audio_content:
                    source = None  # changed: written, not linked
                audio_content = finished.audio
                clips = [(begin + finished.lead_added, end + finished.lead_added) for begin, end in clips]
            else:
                # Never silently: the timing check also reports the missing edges.
                logger.warning("daisy.finish: %s was not finished: %s", relative_path, finished.detail)
        target = paths.audio_file(output_dir, relative_path)
        if source is not None:
            # The chapter as built is the delivered file: linked, not copied.
            link_or_copy(source, target)
        else:
            target.write_bytes(audio_content)
        chapter_audio_files.append(target)
        segment_clips.extend((relative_path, begin, end) for begin, end in clips)
        boundaries = smil_boundaries(clips, playable_length(audio_content))
        smil_clips.extend((relative_path, begin, end) for begin, end in boundaries)
        chapter_ms.append(boundaries[-1][1])
        segment_offset += count
        del audio_content
    front_audio: list[bytes] = []
    for filename, (_label, _text, audio, _configured_name) in zip(front_names, front_matter, strict=True):
        if finishing:
            finished = audio_master.finish_file(audio, delivery)
            audio = finished.audio if finished.finished else audio
        front_audio.append(audio)
        paths.audio_file(output_dir, filename).write_bytes(audio)
    ordered_audio: list[bytes | Path] = [*front_audio, *chapter_audio_files]
    book_clips: list[tuple[float, float]] = []
    if book_audio_path is not None:
        # The complete audiobook travels beside the package, never inside it.
        book_path = book_audio_path
        book_path.parent.mkdir(parents=True, exist_ok=True)
        if finishing:
            # The files already start and end with their silence: the book is
            # those files one after the other, frames copied, nothing re-encoded.
            joined_book = audio_master.join_book(ordered_audio, delivery, destination=book_path)
            if not book_path.is_file():
                book_path.write_bytes(joined_book.audio)
            book_clips = list(joined_book.clips)
        elif mastering is not None and mastering.active:
            mastered_book = audio_master.master_book([audio_bytes(part) for part in ordered_audio], mastering)
            book_path.write_bytes(mastered_book.audio)
            book_clips = list(getattr(mastered_book, "clips", []) or [])
        else:
            book_path.write_bytes(_join_mp3([audio_bytes(part) for part in ordered_audio], use_ffmpeg=use_ffmpeg))
    if len(book_clips) != len(ordered_audio):
        # Joined end to end: each part starts where the previous one ended.
        position = 0.0
        book_clips = []
        for part in ordered_audio:
            length = mp3_duration(audio_bytes(part))
            book_clips.append((position, position + length))
            position += length
    if clips_out is not None:
        clips_out.extend(segment_clips)
    if chapter_starts_out is not None:
        # Where each chapter begins inside the complete audiobook, so a time in
        # a chapter file can be given as a time in the whole book.
        chapter_starts_out.extend(start for start, _end in book_clips[len(front_audio):])

    dtbook = ET.Element(
        _q(DTBOOK_NS, "dtbook"),
        {"version": "2005-3", _q(XML_NS, "lang"): "es-ES"},
    )
    head = ET.SubElement(dtbook, _q(DTBOOK_NS, "head"))
    produced = (production_date or "").strip() or date.today().isoformat()
    publisher_name = dtb_publisher.strip() or PRODUCER
    # Z39.86-2005 requires dtb:uid and dc:Title in the DTBook head; the rest
    # repeats the package metadata, as DAISY Pipeline's books do.
    for name, content in (("dtb:uid", book_id), ("dc:Title", title), ("dc:Creator", author),
                          ("dc:Publisher", publisher_name), ("dc:Date", produced),
                          ("dc:Language", "es-ES"), ("dc:Identifier", book_id), ("dc:Format", "ANSI/NISO Z39.86-2005")):
        if content:
            ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": name, "content": content})
    book = ET.SubElement(dtbook, _q(DTBOOK_NS, "book"))
    frontmatter = ET.SubElement(book, _q(DTBOOK_NS, "frontmatter"))
    ET.SubElement(frontmatter, _q(DTBOOK_NS, "doctitle")).text = title
    if author:
        ET.SubElement(frontmatter, _q(DTBOOK_NS, "docauthor")).text = author
    bodymatter = ET.SubElement(book, _q(DTBOOK_NS, "bodymatter"))
    # Where each SMIL file starts on the book's timeline (dtb:totalElapsedTime),
    # in whole milliseconds summed from the clips actually written.
    elapsed = 0
    smil_names: list[str] = []
    # (label, SMIL reference, class, level) of every navigation point.
    chapter_targets: list[tuple[str, str, str, int]] = []
    # (page label, SMIL reference) of every page, of every note and of every photograph.
    page_targets: list[tuple[str, str]] = []
    note_targets: list[tuple[str, str]] = []
    photo_targets: list[tuple[str, str]] = []
    image_names: dict[int, str] = {}
    for number, (data, extension, media_type) in sorted((images or {}).items()):
        name = f"photo-{number:03d}.{extension}"
        image_names[number] = name
        target = output_dir / paths.image_href(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Every SMIL time container in reading order: playOrder follows it.
    reading_order: dict[str, int] = {}
    custom_tests_used: set[str] = set()
    # The audio of each navigation label, by the label's SMIL reference: the
    # heading as it is read, so a player can say where it is (as the NCX of a
    # DAISY Pipeline book does) without anything being narrated again.
    label_audio: dict[str, tuple[str, int, int]] = {}

    def mark(reference: str) -> str:
        reading_order.setdefault(reference, len(reading_order))
        return reference

    def label_clip(index: int) -> tuple[str, int, int]:
        """The audio of segment ``index`` for the NCX: exactly its SMIL clip.

        A label that said only the speech inside the clip started and ended at
        other times than the heading it names, which a reviewer comparing the
        NCX with the SMIL reads as a timing error. DAISY Pipeline's books, ONCE's
        own included, give the label the very clip the SMIL plays.
        """
        audio_path, begin, end = smil_clips[index]
        return paths.audio_href(audio_path), begin, end

    if layout is not None:
        elapsed = _write_structured_text(
            frontmatter, bodymatter, chapters, layout, smil_clips, chapter_ms, output_dir,
            dtbook_name, book_id, smil_names, chapter_targets, page_targets, note_targets, mark,
            custom_tests_used, paths, image_names, photo_targets, label_audio=label_audio, label_clip=label_clip,
        )
    for index, ((label, text, _raw, filename), _audio) in enumerate(zip(front_matter, front_audio), start=1):
        front_id = f"frontmatter-{index}"
        smil_name = f"frontmatter-{index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": front_id, "class": "frontmatter"})
        ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": f"{front_id}-title"}).text = label
        paragraph_id = f"{front_id}-text"
        par_id = f"{front_id}-par"
        ET.SubElement(level, _q(DTBOOK_NS, "p"),
                      {"id": paragraph_id, "smilref": f"{paths.smil_href(smil_name)}#{par_id}"}).text = text
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        front_ms = int(playable_length(_audio) * 1000)
        _smil_head(smil, book_id, label, elapsed)
        elapsed += front_ms
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"),
                                 {"id": f"seq-{front_id}", "dur": _clock_ms(front_ms)})
        par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
        ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"{paths.root_from_smil(dtbook_name)}#{paragraph_id}"})
        ET.SubElement(par, _q(SMIL_NS, "audio"), {
            "src": paths.audio_from_smil(filename), "clipBegin": _clock_ms(0), "clipEnd": _clock_ms(front_ms),
        })
        _write_xml(paths.smil_file(output_dir, smil_name), smil,
                   SMIL_DOCTYPE, SMIL_NS)
        chapter_targets.append((label, mark(f"{paths.smil_href(smil_name)}#{par_id}"), "frontmatter", 1))
    audio_index = 0
    for chapter_index, (chapter_title, chapter_paragraphs) in enumerate(chapters, start=1):
        if not chapter_paragraphs or layout is not None:
            continue
        chapter_id = f"chapter-{chapter_index}"
        smil_name = f"chapter-{chapter_index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": chapter_id})
        heading_id = f"{chapter_id}-title"
        heading = ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": heading_id})
        heading.text = chapter_title

        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        _smil_head(smil, book_id, chapter_title, elapsed)
        elapsed += chapter_ms[chapter_index - 1]
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"),
                                 {"id": f"seq-{chapter_id}", "dur": _clock_ms(chapter_ms[chapter_index - 1])})
        first_par_id = ""
        # Production jobs synthesize a chapter title as the first segment. Map
        # that audio to the DTBook heading so the spoken and displayed structure
        # stay aligned without duplicating the title as a body paragraph.
        spoken_heading = bool(chapter_paragraphs and chapter_paragraphs[0].strip() == chapter_title.strip())
        if spoken_heading:
            audio_path, clip_begin, clip_end = smil_clips[audio_index]
            label_audio[f"{paths.smil_href(smil_name)}#par-heading-{chapter_index:04d}"] = label_clip(audio_index)
            audio_index += 1
            par_id = f"par-heading-{chapter_index:04d}"
            heading.set("smilref", f"{paths.smil_href(smil_name)}#{par_id}")
            first_par_id = par_id
            par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"{paths.root_from_smil(dtbook_name)}#{heading_id}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": paths.audio_from_smil(audio_path), "clipBegin": _clock_ms(clip_begin), "clipEnd": _clock_ms(clip_end),
            })
            chapter_paragraphs = chapter_paragraphs[1:]
        for paragraph in chapter_paragraphs:
            audio_path, clip_begin, clip_end = smil_clips[audio_index]
            audio_index += 1
            paragraph_id = f"p-{audio_index:04d}"
            par_id = f"par-{audio_index:04d}"
            if not first_par_id:
                first_par_id = par_id
            ET.SubElement(level, _q(DTBOOK_NS, "p"),
                          {"id": paragraph_id, "smilref": f"{paths.smil_href(smil_name)}#{par_id}"}).text = paragraph
            par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"{paths.root_from_smil(dtbook_name)}#{paragraph_id}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": paths.audio_from_smil(audio_path), "clipBegin": _clock_ms(clip_begin), "clipEnd": _clock_ms(clip_end),
            })
        chapter_targets.append((chapter_title, mark(f"{paths.smil_href(smil_name)}#{first_par_id}"), "chapter", 1))
        page_match = re.fullmatch(r"P[aá]gina\s+(\d+)", chapter_title.strip(), flags=re.IGNORECASE)
        if page_match:
            page_targets.append((page_match.group(1), f"{paths.smil_href(smil_name)}#{first_par_id}"))
        _write_xml(paths.smil_file(output_dir, smil_name), smil,
                   SMIL_DOCTYPE, SMIL_NS)

    _write_xml(
        output_dir / dtbook_name,
        dtbook,
        '<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">',
        DTBOOK_NS, stylesheet=stylesheet, stylesheet_media=stylesheet_media,
    )
    if stylesheet:
        # The style sheet travels with the book, beside the DTBook that names it.
        shutil.copyfile(STYLESHEET_SOURCE, output_dir / stylesheet)
    # The NCX title is read with the ONCE title file's heading, when the book has one.
    title_reference = next((reference for _label, reference, nav_class, _level in chapter_targets
                            if nav_class == SECTION_CLASSES["once_title"]), "")
    _write_ncx(output_dir / ncx_name, book_id, title, chapter_targets, page_targets, note_targets, reading_order,
               custom_tests_used, unique_page_ids=layout is not None, photo_targets=photo_targets,
               label_audio=label_audio, title_audio=label_audio.get(title_reference), author=author)
    _write_resources(output_dir / res_name)

    ET.register_namespace("dc", DC_NS)
    package = ET.Element(_q(OPF_NS, "package"), {"unique-identifier": "uid"})
    metadata = ET.SubElement(package, _q(OPF_NS, "metadata"))
    dc_metadata = ET.SubElement(
        metadata,
        _q(OPF_NS, "dc-metadata"),
        {"xmlns:dc": DC_NS, "xmlns:oebpackage": OPF_NS},
    )
    ET.SubElement(dc_metadata, _q(DC_NS, "Title")).text = title
    if author:
        ET.SubElement(dc_metadata, _q(DC_NS, "Creator")).text = author
    # Required (Z39.86-2005 §8.2): who publishes this DAISY book, and when it
    # was made. The printed book's publisher and year are the source's.
    ET.SubElement(dc_metadata, _q(DC_NS, "Publisher")).text = publisher_name
    ET.SubElement(dc_metadata, _q(DC_NS, "Date")).text = produced
    ET.SubElement(dc_metadata, _q(DC_NS, "Identifier"), {"id": "uid", "scheme": "DTB"}).text = book_id
    for scheme, value in identifiers or []:
        if value.strip():
            ET.SubElement(dc_metadata, _q(DC_NS, "Identifier"),
                          {"scheme": re.sub(r"[^A-Za-z0-9._-]", "", scheme) or "local"}).text = value.strip()
    if source_isbn.strip():
        # The printed book the DAISY book is made from, as ONCE's books name it.
        ET.SubElement(dc_metadata, _q(DC_NS, "Source")).text = source_isbn.strip()
    ET.SubElement(dc_metadata, _q(DC_NS, "Language")).text = "es-ES"
    ET.SubElement(dc_metadata, _q(DC_NS, "Format")).text = "ANSI/NISO Z39.86-2005"
    x_metadata = ET.SubElement(metadata, _q(OPF_NS, "x-metadata"))
    source_year = publication_year.strip()
    for name, content in (
        ("dtb:multimediaType", "audioFullText"),
        ("dtb:multimediaContent", "audio,text"),
        # The playing time of all SMIL files (§3.2.3): the same millisecond
        # sum as the last file's totalElapsedTime plus that file's own time.
        ("dtb:totalTime", _clock_ms(elapsed)),
        ("dtb:audioFormat", "MP3"),
        ("dtb:producer", PRODUCER),
        ("dtb:producedDate", produced),
        ("dtb:narrator", narrator.strip()),
        ("dtb:sourcePublisher", publisher.strip()),
        # ISO 8601 or nothing: a year written any other way stays in the bibliographic file only.
        ("dtb:sourceDate", source_year if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", source_year) else ""),
        ("dtb:sourceEdition", edition.strip()),
    ):
        if content:
            ET.SubElement(x_metadata, _q(OPF_NS, "meta"), {"name": name, "content": content})
    manifest = ET.SubElement(package, _q(OPF_NS, "manifest"))
    items = [
        # Z39.86-2005: the manifest lists every file of the book, the package
        # file included, and nothing else (the complete audiobook is not a
        # file of the DAISY book: no SMIL plays it).
        ("opf", opf_name, "text/xml"),
        ("dtbook", dtbook_name, "application/x-dtbook+xml"),
        ("ncx", ncx_name, "application/x-dtbncx+xml"),
        ("resource", res_name, "application/x-dtbresource+xml"),
    ]
    if stylesheet:
        items.append(("dtbook-css", stylesheet, "text/css"))
    for number, name in sorted(image_names.items()):
        items.append((f"photo-{number:03d}", paths.image_href(name), (images or {})[number][2]))
    items.extend((f"smil-{index:04d}", paths.smil_href(name), "application/smil") for index, name in enumerate(smil_names, start=1))
    items.extend(
        (f"chapter-audio-{index:04d}", paths.audio_href(path), "audio/mpeg")
        for index, path in enumerate(chapter_audio_paths, start=1)
    )
    items.extend((f"front-audio-{index:04d}", paths.audio_href(name), "audio/mpeg") for index, name in enumerate(front_names, start=1))
    for item_id, href, media_type in items:
        ET.SubElement(manifest, _q(OPF_NS, "item"), {"id": item_id, "href": href, "media-type": media_type})
    # No toc="ncx": that attribute is OPF 2.0 (EPUB). OEB 1.2, which DAISY 3
    # uses, finds the NCX through the manifest.
    spine = ET.SubElement(package, _q(OPF_NS, "spine"))
    for index, _name in enumerate(smil_names, start=1):
        ET.SubElement(spine, _q(OPF_NS, "itemref"), {"idref": f"smil-{index:04d}"})
    _write_xml(
        output_dir / opf_name,
        package,
        '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN" "http://openebook.org/dtds/oeb-1.2/oebpkg12.dtd">',
        OPF_NS,
    )
    return output_dir


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _write_structured_text(
    frontmatter: ET.Element, bodymatter: ET.Element, chapters: list[tuple[str, list[str]]], layout: list[dict],
    smil_clips: list[tuple[str, int, int]], chapter_ms: list[int], output_dir: Path,
    dtbook_name: str, book_id: str, smil_names: list[str], chapter_targets: list[tuple[str, str, str, int]],
    page_targets: list[tuple[str, str]], note_targets: list[tuple[str, str]], mark, custom_tests_used: set[str],
    paths: PackagePaths, image_names: dict[int, str] | None = None,
    photo_targets: list[tuple[str, str]] | None = None, *,
    label_audio: dict[str, tuple[str, int, int]] | None = None,
    label_clip: Callable[[int], tuple[str, int, int]] | None = None,
) -> int:
    """The DTBook levels and SMIL files of a book with the ONCE structure.

    Each section keeps its own SMIL file and audio file. Initial files sit in
    the front matter; a subchapter is a ``level2`` inside its chapter; a page of
    the original is a ``pagenum`` with a silent, skippable SMIL ``par`` (no
    audio: the number is a navigation point, not read aloud); a note is a
    ``note`` whose ``seq`` is skippable. Returns the book's total time in
    milliseconds, the sum of the SMIL files' own times.
    """
    elapsed = 0
    audio_index = 0
    note_count = 0
    page_count = 0
    frame_count = 0
    # Each page of the original is marked once, where it first occurs: a page
    # list may name a page only once (ZedVal ncx_pageTargUniqValTypeComb), and
    # "go to page 2" must lead to one place. A planilla's initial files take
    # the pages of the printed copy's first pages; the source's own front
    # matter, when it is also read, repeats them and loses the marker.
    pages_marked: set[tuple[str, object]] = set()
    repeated_pages: list[str] = []
    image_names = image_names or {}
    photo_groups: dict[int, ET.Element] = {}
    # Every note of the book, (section, number, id), numbered as written below.
    book_notes: list[tuple[int, int, str]] = []
    upcoming = 0
    for section_number, entry in enumerate(layout, start=1):
        previous_note = None
        for unit in entry["units"]:
            if (unit.get("role") or "text") == "note":
                if previous_note is None or unit.get("note") != previous_note:
                    upcoming += 1
                    if isinstance(unit.get("note"), int):
                        book_notes.append((section_number, unit["note"], f"note-{upcoming}"))
                previous_note = unit.get("note")
            else:
                previous_note = None
    current_chapter: ET.Element | None = None
    current_initial = False
    levels: list[ET.Element] = []
    for number, ((chapter_title, chapter_paragraphs), entry) in enumerate(zip(chapters, layout), start=1):
        kind = entry.get("kind") or "chapter"
        initial = kind in INITIAL_SECTION_KINDS
        # A level 2 nests in the level 1 before it, on its own side of the
        # book: the bibliographic data inside file 1 in the front matter, a
        # subchapter inside its chapter; never a chapter inside an initial file.
        nested = entry.get("level") == 2 and current_chapter is not None and initial == current_initial
        parent = current_chapter if nested else (frontmatter if initial else bodymatter)
        level_name, heading_name = ("level2", "h2") if nested else ("level1", "h1")
        section_id = f"chapter-{number}"
        level = ET.SubElement(parent, _q(DTBOOK_NS, level_name),
                              {"id": section_id, "class": SECTION_CLASSES.get(kind, "chapter")})
        levels.append(level)
        if not nested:
            current_chapter, current_initial = level, initial
        smil_name = f"chapter-{number:04d}.smil"
        smil_names.append(smil_name)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        head = _smil_head(smil, book_id, chapter_title, elapsed)
        elapsed += chapter_ms[number - 1]
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"),
                                 {"id": f"seq-{section_id}", "dur": _clock_ms(chapter_ms[number - 1])})
        units = entry["units"]
        heading_id = f"{section_id}-title"
        # A reference points at the first note with its number from here on:
        # a note is read at the end of its chapter, which may be inside a
        # subchapter by then, and numbering may start again in each chapter.
        note_ids: dict[int, str] = {}
        for section_number, note_number, note_id in book_notes:
            if section_number >= number:
                note_ids.setdefault(note_number, note_id)
        if not units or units[0].get("role") != "heading":
            # A title the book does not say labels navigation only.
            ET.SubElement(level, _q(DTBOOK_NS, heading_name), {"id": heading_id}).text = chapter_title
        tests: set[str] = set()
        target = ""
        note_element: ET.Element | None = None
        note_sequence: ET.Element | None = None
        note_number = None
        # Boxed or shaded text: a DTBook sidebar (render="required": it is read),
        # announced by producer's notes, with its own SMIL seq.
        frame_element: ET.Element | None = None
        frame_sequence: ET.Element | None = None
        frame_id = None
        for text, unit in zip(chapter_paragraphs, units):
            role = unit.get("role") or "text"
            if role != "note" or unit.get("note") != note_number:
                note_element = note_sequence = None
                note_number = None
            frame = unit.get("frame")
            if not frame or frame.get("id") != frame_id:
                frame_element = frame_sequence = None
                frame_id = None
            if frame and frame_element is None:
                frame_count += 1
                frame_id = frame.get("id")
                frame_element = ET.SubElement(level, _q(DTBOOK_NS, "sidebar"), {
                    "id": f"sidebar-{frame_count}", "render": "required",
                    "class": "shaded" if frame.get("kind") == "shaded" else "boxed"})
                frame_sequence = ET.SubElement(sequence, _q(SMIL_NS, "seq"), {
                    "id": f"seq-sidebar-{frame_count}", "class": "sidebar"})
            for page in unit.get("page_breaks") or []:
                page_type, page_value = page_kind(page)
                key = (page_type, page_value if page_value is not None else page.strip())
                if key in pages_marked:
                    repeated_pages.append(page)
                    continue
                pages_marked.add(key)
                page_count += 1
                page_id, par_id = f"page-{page_count:04d}", f"par-page-{page_count:04d}"
                ET.SubElement(frame_element if frame_element is not None else level, _q(DTBOOK_NS, "pagenum"), {
                    "id": page_id, "page": page_kind(page)[0], "smilref": f"{paths.smil_href(smil_name)}#{par_id}",
                }).text = page
                par = ET.SubElement(frame_sequence if frame_sequence is not None else sequence, _q(SMIL_NS, "par"),
                                    {"id": par_id, "class": "pagenum", "customTest": "pagenum"})
                ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"{paths.root_from_smil(dtbook_name)}#{page_id}"})
                page_targets.append((page, mark(f"{paths.smil_href(smil_name)}#{par_id}")))
                tests.add("pagenum")
            audio_path, clip_begin, clip_end = smil_clips[audio_index]
            audio_index += 1
            if role == "heading":
                element = ET.SubElement(level, _q(DTBOOK_NS, heading_name), {"id": heading_id})
                par_id = f"par-heading-{number:04d}"
                container = sequence
            elif role == "note":
                if note_element is None or note_sequence is None:
                    note_count += 1
                    note_number = unit.get("note")
                    note_element = ET.SubElement(level, _q(DTBOOK_NS, "note"),
                                                 {"id": f"note-{note_count}", "class": "once-note"})
                    note_sequence = ET.SubElement(sequence, _q(SMIL_NS, "seq"), {
                        "id": f"seq-note-{note_count}", "class": "note", "customTest": "note"})
                    note_targets.append((f"Nota {note_number or note_count}",
                                         mark(f"{paths.smil_href(smil_name)}#seq-note-{note_count}")))
                    tests.add("note")
                element = ET.SubElement(note_element, _q(DTBOOK_NS, "p"), {"id": f"p-{audio_index:04d}"})
                par_id = f"par-{audio_index:04d}"
                container = note_sequence
            elif role in {"photo", "photo_caption"} and (unit.get("photo") or {}).get("number"):
                # A photograph of an insert: an imggroup with its image, the
                # "Fotografía N." that is read (a producer's note) and the
                # book's own caption. Never a description.
                number_of_photo = int(unit["photo"]["number"])
                group = photo_groups.get(number_of_photo)
                if group is None:
                    group = ET.SubElement(level, _q(DTBOOK_NS, "imggroup"),
                                          {"id": f"photo-{number_of_photo}", "class": "once-photo"})
                    photo_groups[number_of_photo] = group
                    if number_of_photo in image_names:
                        ET.SubElement(group, _q(DTBOOK_NS, "img"), {
                            "id": f"img-{number_of_photo}", "src": paths.image_href(image_names[number_of_photo]),
                            "alt": text.strip().rstrip(".")})
                if role == "photo":
                    element = ET.SubElement(group, _q(DTBOOK_NS, "prodnote"), {
                        "id": f"p-{audio_index:04d}", "render": "required", "class": "once-photo"})
                else:
                    element = ET.SubElement(group, _q(DTBOOK_NS, "caption"), {"id": f"p-{audio_index:04d}"})
                par_id = f"par-{audio_index:04d}"
                container = sequence
                if role == "photo" and photo_targets is not None:
                    photo_targets.append((text.strip().rstrip("."), f"{paths.smil_href(smil_name)}#{par_id}"))
            elif frame_element is not None and frame_sequence is not None:
                # "Texto en recuadro." and its end are the producer's words: prodnote.
                announcement = role in {"frame_open", "frame_close"}
                attributes = {"id": f"p-{audio_index:04d}"}
                if announcement:
                    attributes.update({"render": "required", "class": "once-" + role.replace("_", "-")})
                element = ET.SubElement(frame_element, _q(DTBOOK_NS, "prodnote" if announcement else "p"),
                                        attributes)
                par_id = f"par-{audio_index:04d}"
                container = frame_sequence
            else:
                attributes = {"id": f"p-{audio_index:04d}"}
                if role != "text":
                    attributes["class"] = "once-" + role.replace("_", "-")
                element = ET.SubElement(level, _q(DTBOOK_NS, "p"), attributes)
                par_id = f"par-{audio_index:04d}"
                container = sequence
            if role == "text" and unit.get("note_refs"):
                _with_note_refs(element, text, unit.get("note_refs"), note_ids)
            else:
                element.text = text
            element.set("smilref", f"{paths.smil_href(smil_name)}#{par_id}")
            par = ET.SubElement(container, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"{paths.root_from_smil(dtbook_name)}#{element.get('id')}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": paths.audio_from_smil(audio_path), "clipBegin": _clock_ms(clip_begin), "clipEnd": _clock_ms(clip_end),
            })
            reference = mark(f"{paths.smil_href(smil_name)}#{par_id}")
            if role == "heading" and not target and label_audio is not None and label_clip is not None:
                # The section's navigation label says its heading, as the heading is read.
                label_audio[reference] = label_clip(audio_index - 1)
            target = target or reference
        _declare_custom_tests(head, tests)
        custom_tests_used.update(tests)
        chapter_targets.append((chapter_title, target, SECTION_CLASSES.get(kind, "chapter"), 2 if nested else 1))
        _write_xml(paths.smil_file(output_dir, smil_name), smil, SMIL_DOCTYPE, SMIL_NS)
    # The DTBook DTD wants something after a level's heading. A section that is
    # only a heading (a part title followed at once by its first chapter) gets
    # an empty paragraph: it is not synchronized and nothing is read for it.
    for level in levels:
        if not any(_local(child.tag) not in {"pagenum", "h1", "h2"} for child in level):
            ET.SubElement(level, _q(DTBOOK_NS, "p"))
    if not len(bodymatter):
        raise ValueError("A DAISY book needs at least one chapter after its initial files.")
    if repeated_pages:
        logger.warning("daisy.pages: pages marked twice, kept where they first occur: %s",
                       ", ".join(repeated_pages[:20]))
    return elapsed


# What each skippable structure is called, for a player to say what it skips
# (Z39.86-2005 §10): every smilCustomTest of the NCX needs an entry in the
# resource file's NCX scope (ZedVal's resource_skipStructResEntry). In Spanish,
# and in English as in the resource file of DAISY Pipeline's books.
SKIPPABLE_LABELS = (
    ("page-set", "PAGE_NUMBER", "página", "page"),
    ("note-set", "NOTE", "nota", "note"),
    ("noteref-set", "NOTE_REFERENCE", "llamada de nota", "note reference"),
    ("annotation-set", "ANNOTATION", "anotación", "annotation"),
    ("line-set", "LINE_NUMBER", "número de línea", "line"),
    ("sidebar-set", "OPTIONAL_SIDEBAR", "recuadro", "sidebar"),
    ("prodnote-set", "OPTIONAL_PRODUCER_NOTE", "nota del productor", "producer note"),
)


def _write_resources(path: Path) -> None:
    """The resource file: the name of every skippable structure, and of chapters."""
    # The resource DTD declares no xml:lang on <resources>; the language is
    # stated on each <resource>.
    resources = ET.Element(_q(RESOURCE_NS, "resources"), {"version": "2005-1"})
    scope = ET.SubElement(resources, _q(RESOURCE_NS, "scope"), {"nsuri": NCX_NS})
    for set_id, book_struct, spanish, english in SKIPPABLE_LABELS:
        node_set = ET.SubElement(scope, _q(RESOURCE_NS, "nodeSet"),
                                 {"id": set_id, "select": f"//smilCustomTest[@bookStruct='{book_struct}']"})
        for language, label in (("es", spanish), ("en", english)):
            resource = ET.SubElement(node_set, _q(RESOURCE_NS, "resource"), {_q(XML_NS, "lang"): language})
            ET.SubElement(resource, _q(RESOURCE_NS, "text")).text = label
    node_set = ET.SubElement(scope, _q(RESOURCE_NS, "nodeSet"),
                             {"id": "chapter-labels", "select": "//navPoint[@class='chapter']"})
    resource = ET.SubElement(node_set, _q(RESOURCE_NS, "resource"), {"id": "chapter-label-es", _q(XML_NS, "lang"): "es"})
    ET.SubElement(resource, _q(RESOURCE_NS, "text")).text = "Capítulo"
    _write_xml(
        path,
        resources,
        '<!DOCTYPE resources PUBLIC "-//NISO//DTD resource 2005-1//EN" "http://www.daisy.org/z3986/2005/resource-2005-1.dtd">',
        RESOURCE_NS,
    )


def _label(parent: ET.Element, tag: str, text: str, audio: tuple[str, int, int] | None) -> ET.Element:
    """A navLabel, docTitle or docAuthor: its text, and the audio that says it when there is one."""
    label = ET.SubElement(parent, _q(NCX_NS, tag))
    ET.SubElement(label, _q(NCX_NS, "text")).text = text
    if audio is not None:
        src, begin, end = audio
        ET.SubElement(label, _q(NCX_NS, "audio"), {"src": src, "clipBegin": _clock_ms(begin), "clipEnd": _clock_ms(end)})
    return label


def _write_ncx(
    path: Path, book_id: str, title: str, chapter_targets: list[tuple[str, str, str, int]],
    page_targets: list[tuple[str, str]], note_targets: list[tuple[str, str]], reading_order: dict[str, int],
    custom_tests: set[str], *, unique_page_ids: bool, photo_targets: list[tuple[str, str]] | None = None,
    label_audio: dict[str, tuple[str, int, int]] | None = None, title_audio: tuple[str, int, int] | None = None,
    author: str = "",
) -> None:
    """The navigation file: nested sections, pages, notes.

    ``playOrder`` follows the reading order and increases by one for each
    distinct SMIL time container (Z39.86-2005 §8.4.3): a section and the page
    marker it begins with point at different containers and get different
    numbers, while two entries pointing at the same container share one.

    A section whose heading is read has that audio in its ``navLabel``
    (``label_audio``, by the section's SMIL reference), and the book's title
    the audio of the title file (``title_audio``): a player browsing the
    navigation says each heading with the book's own voice. Page labels stay
    text only: a page number is never read aloud.
    """
    label_audio = label_audio or {}
    referenced = {reference for _label, reference, _class, _level in chapter_targets}
    referenced |= {reference for _label, reference in page_targets}
    referenced |= {reference for _label, reference in note_targets}
    referenced |= {reference for _label, reference in photo_targets or []}
    ordered = sorted(referenced, key=lambda reference: reading_order.get(reference, len(reading_order)))
    play = {reference: str(position) for position, reference in enumerate(ordered, start=1)}

    ncx = ET.Element(_q(NCX_NS, "ncx"), {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"})
    ncx_head = ET.SubElement(ncx, _q(NCX_NS, "head"))
    values = [value for value in (page_kind(label)[1] for label, _reference in page_targets) if value]
    for name, content in (
        ("dtb:uid", book_id),
        ("dtb:generator", PRODUCER),
        ("dtb:depth", str(max((level for _label, _reference, _class, level in chapter_targets), default=1))),
        ("dtb:totalPageCount", str(len(page_targets))),
        ("dtb:maxPageNumber", str(max(values, default=0))),
    ):
        ET.SubElement(ncx_head, _q(NCX_NS, "meta"), {"name": name, "content": content})
    for test in sorted(custom_tests):
        ET.SubElement(ncx_head, _q(NCX_NS, "smilCustomTest"), {"id": test, **CUSTOM_TESTS[test]})
    _label(ncx, "docTitle", title, title_audio)
    if author:
        _label(ncx, "docAuthor", author, None)
    nav_map = ET.SubElement(ncx, _q(NCX_NS, "navMap"))
    stack: list[tuple[int, ET.Element]] = []
    for order, (label, reference, nav_class, level) in enumerate(chapter_targets, start=1):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else nav_map
        nav_point = ET.SubElement(parent, _q(NCX_NS, "navPoint"),
                                  {"id": f"nav-{order}", "playOrder": play[reference], "class": nav_class})
        _label(nav_point, "navLabel", label, label_audio.get(reference))
        ET.SubElement(nav_point, _q(NCX_NS, "content"), {"src": reference})
        stack.append((level, nav_point))
    if page_targets:
        page_list = ET.SubElement(ncx, _q(NCX_NS, "pageList"))
        page_label = ET.SubElement(page_list, _q(NCX_NS, "navLabel"))
        ET.SubElement(page_label, _q(NCX_NS, "text")).text = "Páginas"
        for index, (label, reference) in enumerate(page_targets, start=1):
            kind, value = page_kind(label)
            attributes = {"id": f"page-{index:04d}" if unique_page_ids else f"page-{label}"}
            if value:
                attributes["value"] = str(value)
            attributes.update({"type": kind, "playOrder": play[reference]})
            page_target = ET.SubElement(page_list, _q(NCX_NS, "pageTarget"), attributes)
            page_nav_label = ET.SubElement(page_target, _q(NCX_NS, "navLabel"))
            ET.SubElement(page_nav_label, _q(NCX_NS, "text")).text = label
            ET.SubElement(page_target, _q(NCX_NS, "content"), {"src": reference})
    if note_targets:
        nav_list = ET.SubElement(ncx, _q(NCX_NS, "navList"), {"class": "note"})
        list_label = ET.SubElement(nav_list, _q(NCX_NS, "navLabel"))
        ET.SubElement(list_label, _q(NCX_NS, "text")).text = "Notas"
        for index, (label, reference) in enumerate(note_targets, start=1):
            nav_target = ET.SubElement(nav_list, _q(NCX_NS, "navTarget"),
                                       {"id": f"note-target-{index}", "class": "note", "playOrder": play[reference]})
            target_label = ET.SubElement(nav_target, _q(NCX_NS, "navLabel"))
            ET.SubElement(target_label, _q(NCX_NS, "text")).text = label
            ET.SubElement(nav_target, _q(NCX_NS, "content"), {"src": reference})
    if photo_targets:
        # Jump from photograph to photograph, as from note to note.
        nav_list = ET.SubElement(ncx, _q(NCX_NS, "navList"), {"class": "photo"})
        list_label = ET.SubElement(nav_list, _q(NCX_NS, "navLabel"))
        ET.SubElement(list_label, _q(NCX_NS, "text")).text = "Fotografías"
        for index, (label, reference) in enumerate(photo_targets, start=1):
            nav_target = ET.SubElement(nav_list, _q(NCX_NS, "navTarget"),
                                       {"id": f"photo-target-{index}", "class": "photo", "playOrder": play[reference]})
            target_label = ET.SubElement(nav_target, _q(NCX_NS, "navLabel"))
            ET.SubElement(target_label, _q(NCX_NS, "text")).text = label
            ET.SubElement(nav_target, _q(NCX_NS, "content"), {"src": reference})
    _write_xml(
        path,
        ncx,
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
        NCX_NS,
    )
