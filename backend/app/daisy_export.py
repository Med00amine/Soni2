from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from app import audio_master


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


def _write_xml(path: Path, root: ET.Element, doctype: str | None = None, default_namespace: str | None = None) -> None:
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
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    if default_namespace == OPF_NS:
        # <dc-metadata> declares the dc namespace itself, as the OEB 1.2 DTD
        # requires; ElementTree would also put it on <package>, where the DTD
        # does not allow it.
        body = body.replace(f' xmlns:dc="{DC_NS}"', "", 1) if body.count(f'xmlns:dc="{DC_NS}"') > 1 else body
    prefix = '<?xml version="1.0" encoding="UTF-8"?>\n'
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


def _clock(seconds: float) -> str:
    """A SMIL full clock value, e.g. 0:01:02.345."""
    millis = max(0, round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return f"{hours}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


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


def _smil_head(smil: ET.Element, book_id: str, title: str, elapsed: float) -> ET.Element:
    """The metadata Z39.86-2005 requires in every SMIL file."""
    head = ET.SubElement(smil, _q(SMIL_NS, "head"))
    for name, content in (("dtb:uid", book_id), ("dtb:generator", "Soni2"),
                          ("dtb:totalElapsedTime", _clock(elapsed)), ("dc:Title", title)):
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
                   "once_index": "once-index", "chapter": "chapter", "subchapter": "subchapter"}
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
    chapter_audio: list[bytes] | None = None,
    chapter_clips: list[list[tuple[float, float]]] | None = None,
    use_ffmpeg: bool = False,
    clips_out: list[tuple[str, float, float]] | None = None,
    chapter_starts_out: list[float] | None = None,
    mastering: audio_master.MasteringOptions | None = None,
    layout: list[dict] | None = None,
    identifiers: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a DAISY 3 package whose files carry the book's own name.

    Audio lives once per chapter, in ``audio/chapter-NNN/chapter-NNN.mp3``;
    each paragraph's SMIL entry points at its stretch of that file with
    clipBegin/clipEnd rather than at a separate per-paragraph MP3. When
    ``clips_out`` is given it receives, for every segment in order, the
    package-relative chapter audio path and the clip's start and end seconds.

    ``layout`` (one entry per chapter) gives the ONCE structure: each entry's
    ``kind`` and ``level``, and for every paragraph its ``role`` ("heading",
    "text", "note", "closing", …), the ``page_breaks`` that begin at it and its
    ``note`` number. The initial files then sit in the DTBook front matter,
    subchapters nest as ``level2``, page numbers become silent markers and
    notes skippable ``note`` structures. Without it the package is written
    exactly as before. ``identifiers`` adds ``(scheme, value)`` identifiers
    such as the ABSYS number to the package metadata.
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
    (output_dir / "audio").mkdir(parents=True, exist_ok=True)
    (output_dir / "smil").mkdir(parents=True, exist_ok=True)
    book_id = f"soni2-{uuid.uuid4()}"
    base = package_basename(title)
    dtbook_name, ncx_name, opf_name, res_name = (f"{base}.{ext}" for ext in ("xml", "ncx", "opf", "res"))
    full_audio_name = f"{base}.mp3"
    front_matter = front_matter or []
    front_names: list[str] = []
    for _label, _text, _audio, filename in front_matter:
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".mp3" or filename in front_names:
            raise ValueError("Front-matter audio filenames must be unique MP3 filenames without directories.")
        front_names.append(filename)
    chapter_folder_names = [f"chapter-{index:03d}" for index in range(1, len(chapters) + 1)]
    # One final audio file per chapter, inside that chapter's own directory.
    # Earlier revisions wrote each chapter three times and each segment twice;
    # every byte of audio now has exactly one home in the package.
    chapter_audio_paths = [f"{name}/{name}.mp3" for name in chapter_folder_names]
    if chapter_audio is not None and len(chapter_audio) != len(chapters):
        raise ValueError("Chapter audio count does not match chapter count.")
    if set(front_names).intersection(f"{name}.mp3" for name in chapter_folder_names):
        raise ValueError("Front-matter audio filenames cannot reuse chapter audio filenames.")
    chapter_audio_contents: list[bytes] = []
    # (chapter audio path, clip start, clip end) for every segment, in order.
    segment_clips: list[tuple[str, float, float]] = []
    segment_offset = 0
    for chapter_index, (relative_path, folder_name, (_chapter_title, chapter_body)) in enumerate(
        zip(chapter_audio_paths, chapter_folder_names, chapters, strict=True)
    ):
        count = len(chapter_body)
        chapter_segments = audio_segments[segment_offset:segment_offset + count]
        if chapter_audio is not None:
            # Already-built chapter audio: its clips come with it.
            audio_content = chapter_audio[chapter_index]
            clips = (chapter_clips[chapter_index] if chapter_clips is not None
                     else measure_segment_clips(chapter_segments, audio_content))
        elif mastering is not None and mastering.active:
            # Trimmed, paced and levelled, with the real position of every
            # paragraph measured rather than estimated.
            mastered = audio_master.master_chapter(chapter_segments, mastering)
            audio_content = mastered.audio
            clips = mastered.clips or measure_segment_clips(chapter_segments, audio_content)
        else:
            audio_content = _join_mp3(chapter_segments, use_ffmpeg=use_ffmpeg)
            clips = measure_segment_clips(chapter_segments, audio_content)
        chapter_audio_contents.append(audio_content)
        chapter_file = output_dir / "audio" / relative_path
        chapter_file.parent.mkdir(parents=True, exist_ok=True)
        chapter_file.write_bytes(audio_content)
        segment_clips.extend((relative_path, begin, end) for begin, end in clips)
        segment_offset += count
    for filename, (_label, _text, audio, _configured_name) in zip(front_names, front_matter, strict=True):
        (output_dir / "audio" / filename).write_bytes(audio)
    front_audio = [item[2] for item in front_matter]
    ordered_audio = front_audio + chapter_audio_contents
    book_clips: list[tuple[float, float]] = []
    if mastering is not None and mastering.active:
        # The chapters are already levelled; the book only needs the longer
        # pause between them so a listener hears where one ends.
        mastered_book = audio_master.master_book(ordered_audio, mastering)
        full_audio = mastered_book.audio
        book_clips = list(getattr(mastered_book, "clips", []) or [])
    else:
        full_audio = _join_mp3(ordered_audio, use_ffmpeg=use_ffmpeg)
    if len(book_clips) != len(ordered_audio):
        # Joined end to end: each part starts where the previous one ended.
        position = 0.0
        book_clips = []
        for part in ordered_audio:
            length = mp3_duration(part)
            book_clips.append((position, position + length))
            position += length
    (output_dir / full_audio_name).write_bytes(full_audio)
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
    # Z39.86-2005 requires dtb:uid and dc:Title in the DTBook head.
    for name, content in (("dtb:uid", book_id), ("dc:Title", title), ("dc:Creator", author),
                          ("dc:Language", "es-ES"), ("dc:Identifier", book_id), ("dc:Format", "ANSI/NISO Z39.86-2005")):
        if content:
            ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": name, "content": content})
    book = ET.SubElement(dtbook, _q(DTBOOK_NS, "book"))
    frontmatter = ET.SubElement(book, _q(DTBOOK_NS, "frontmatter"))
    ET.SubElement(frontmatter, _q(DTBOOK_NS, "doctitle")).text = title
    if author:
        ET.SubElement(frontmatter, _q(DTBOOK_NS, "docauthor")).text = author
    bodymatter = ET.SubElement(book, _q(DTBOOK_NS, "bodymatter"))
    # Where each SMIL file starts on the book's timeline (dtb:totalElapsedTime).
    elapsed = 0.0
    smil_names: list[str] = []
    # (label, SMIL reference, class, level) of every navigation point.
    chapter_targets: list[tuple[str, str, str, int]] = []
    # (page label, SMIL reference) of every page, and of every note.
    page_targets: list[tuple[str, str]] = []
    note_targets: list[tuple[str, str]] = []
    # Every SMIL time container in reading order: playOrder follows it.
    reading_order: dict[str, int] = {}
    custom_tests_used: set[str] = set()

    def mark(reference: str) -> str:
        reading_order.setdefault(reference, len(reading_order))
        return reference

    if layout is not None:
        elapsed = _write_structured_text(
            frontmatter, bodymatter, chapters, layout, segment_clips, chapter_audio_contents, output_dir,
            dtbook_name, book_id, smil_names, chapter_targets, page_targets, note_targets, mark,
            custom_tests_used,
        )
    for index, (label, text, _audio, filename) in enumerate(front_matter, start=1):
        front_id = f"frontmatter-{index}"
        smil_name = f"frontmatter-{index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": front_id, "class": "frontmatter"})
        ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": f"{front_id}-title"}).text = label
        paragraph_id = f"{front_id}-text"
        par_id = f"{front_id}-par"
        ET.SubElement(level, _q(DTBOOK_NS, "p"),
                      {"id": paragraph_id, "smilref": f"smil/{smil_name}#{par_id}"}).text = text
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        _smil_head(smil, book_id, label, elapsed)
        elapsed += mp3_duration(_audio)
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"), {"id": f"seq-{front_id}"})
        par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
        ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{paragraph_id}"})
        ET.SubElement(par, _q(SMIL_NS, "audio"), {
            "src": f"../audio/{filename}", "clipBegin": _clock(0), "clipEnd": _clock(mp3_duration(_audio)),
        })
        _write_xml(output_dir / "smil" / smil_name, smil,
                   SMIL_DOCTYPE, SMIL_NS)
        chapter_targets.append((label, mark(f"smil/{smil_name}#{par_id}"), "frontmatter", 1))
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
        elapsed += mp3_duration(chapter_audio_contents[chapter_index - 1])
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"), {"id": f"seq-{chapter_id}"})
        first_par_id = ""
        # Production jobs synthesize a chapter title as the first segment. Map
        # that audio to the DTBook heading so the spoken and displayed structure
        # stay aligned without duplicating the title as a body paragraph.
        spoken_heading = bool(chapter_paragraphs and chapter_paragraphs[0].strip() == chapter_title.strip())
        if spoken_heading:
            audio_path, clip_begin, clip_end = segment_clips[audio_index]
            audio_index += 1
            par_id = f"par-heading-{chapter_index:04d}"
            heading.set("smilref", f"smil/{smil_name}#{par_id}")
            first_par_id = par_id
            par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{heading_id}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": f"../audio/{audio_path}", "clipBegin": _clock(clip_begin), "clipEnd": _clock(clip_end),
            })
            chapter_paragraphs = chapter_paragraphs[1:]
        for paragraph in chapter_paragraphs:
            audio_path, clip_begin, clip_end = segment_clips[audio_index]
            audio_index += 1
            paragraph_id = f"p-{audio_index:04d}"
            par_id = f"par-{audio_index:04d}"
            if not first_par_id:
                first_par_id = par_id
            ET.SubElement(level, _q(DTBOOK_NS, "p"),
                          {"id": paragraph_id, "smilref": f"smil/{smil_name}#{par_id}"}).text = paragraph
            par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{paragraph_id}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": f"../audio/{audio_path}", "clipBegin": _clock(clip_begin), "clipEnd": _clock(clip_end),
            })
        chapter_targets.append((chapter_title, mark(f"smil/{smil_name}#{first_par_id}"), "chapter", 1))
        page_match = re.fullmatch(r"P[aá]gina\s+(\d+)", chapter_title.strip(), flags=re.IGNORECASE)
        if page_match:
            page_targets.append((page_match.group(1), f"smil/{smil_name}#{first_par_id}"))
        _write_xml(output_dir / "smil" / smil_name, smil,
                   SMIL_DOCTYPE, SMIL_NS)

    _write_xml(
        output_dir / dtbook_name,
        dtbook,
        '<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">',
        DTBOOK_NS,
    )
    _write_ncx(output_dir / ncx_name, book_id, title, chapter_targets, page_targets, note_targets, reading_order,
               custom_tests_used, unique_page_ids=layout is not None)

    # The resource DTD declares no xml:lang on <resources>; the language is
    # stated on each <resource>.
    resources = ET.Element(_q(RESOURCE_NS, "resources"), {"version": "2005-1"})
    scope = ET.SubElement(resources, _q(RESOURCE_NS, "scope"), {"nsuri": NCX_NS})
    node_set = ET.SubElement(
        scope,
        _q(RESOURCE_NS, "nodeSet"),
        {"id": "chapter-labels", "select": "//navPoint[@class='chapter']"},
    )
    resource = ET.SubElement(node_set, _q(RESOURCE_NS, "resource"), {"id": "chapter-label-es", _q(XML_NS, "lang"): "es"})
    ET.SubElement(resource, _q(RESOURCE_NS, "text")).text = "Capítulo"
    _write_xml(
        output_dir / res_name,
        resources,
        '<!DOCTYPE resources PUBLIC "-//NISO//DTD resource 2005-1//EN" "http://www.daisy.org/z3986/2005/resource-2005-1.dtd">',
        RESOURCE_NS,
    )

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
    if edition:
        ET.SubElement(dc_metadata, _q(DC_NS, "Description")).text = f"Edición: {edition}"
    if publication_year:
        ET.SubElement(dc_metadata, _q(DC_NS, "Date")).text = publication_year
    if publisher:
        ET.SubElement(dc_metadata, _q(DC_NS, "Publisher")).text = publisher
    ET.SubElement(dc_metadata, _q(DC_NS, "Identifier"), {"id": "uid", "scheme": "DTB"}).text = book_id
    for scheme, value in identifiers or []:
        if value.strip():
            ET.SubElement(dc_metadata, _q(DC_NS, "Identifier"),
                          {"scheme": re.sub(r"[^A-Za-z0-9._-]", "", scheme) or "local"}).text = value.strip()
    ET.SubElement(dc_metadata, _q(DC_NS, "Language")).text = "es"
    ET.SubElement(dc_metadata, _q(DC_NS, "Format")).text = "ANSI/NISO Z39.86-2005"
    x_metadata = ET.SubElement(metadata, _q(OPF_NS, "x-metadata"))
    for name, content in (
        ("dtb:producer", "Soni2"),
        ("dtb:multimediaContent", "audio,text"),
        ("dtb:multimediaType", "audioFullText"),
        ("dtb:audioFormat", "MP3"),
        ("dtb:totalTime", _clock(mp3_duration(full_audio))),
    ):
        ET.SubElement(x_metadata, _q(OPF_NS, "meta"), {"name": name, "content": content})
    manifest = ET.SubElement(package, _q(OPF_NS, "manifest"))
    items = [
        # Z39.86-2005: the manifest lists every file of the book, the package
        # file included.
        ("opf", opf_name, "text/xml"),
        ("dtbook", dtbook_name, "application/x-dtbook+xml"),
        ("ncx", ncx_name, "application/x-dtbncx+xml"),
        ("resource", res_name, "application/x-dtbresource+xml"),
        ("full-audiobook", full_audio_name, "audio/mpeg"),
    ]
    items.extend((f"smil-{index:04d}", f"smil/{name}", "application/smil") for index, name in enumerate(smil_names, start=1))
    items.extend(
        (f"chapter-audio-{index:04d}", f"audio/{path}", "audio/mpeg")
        for index, path in enumerate(chapter_audio_paths, start=1)
    )
    items.extend((f"front-audio-{index:04d}", f"audio/{name}", "audio/mpeg") for index, name in enumerate(front_names, start=1))
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
    segment_clips: list[tuple[str, float, float]], chapter_audio_contents: list[bytes], output_dir: Path,
    dtbook_name: str, book_id: str, smil_names: list[str], chapter_targets: list[tuple[str, str, str, int]],
    page_targets: list[tuple[str, str]], note_targets: list[tuple[str, str]], mark, custom_tests_used: set[str],
) -> float:
    """The DTBook levels and SMIL files of a book with the ONCE structure.

    Each section keeps its own SMIL file and audio file. Initial files sit in
    the front matter; a subchapter is a ``level2`` inside its chapter; a page of
    the original is a ``pagenum`` with a silent, skippable SMIL ``par`` (no
    audio: the number is a navigation point, not read aloud); a note is a
    ``note`` whose ``seq`` is skippable. Returns the book's total time.
    """
    elapsed = 0.0
    audio_index = 0
    note_count = 0
    page_count = 0
    current_chapter: ET.Element | None = None
    levels: list[ET.Element] = []
    for number, ((chapter_title, chapter_paragraphs), entry) in enumerate(zip(chapters, layout), start=1):
        kind = entry.get("kind") or "chapter"
        initial = kind in INITIAL_SECTION_KINDS
        nested = entry.get("level") == 2 and current_chapter is not None and not initial
        parent = current_chapter if nested else (frontmatter if initial else bodymatter)
        level_name, heading_name = ("level2", "h2") if nested else ("level1", "h1")
        section_id = f"chapter-{number}"
        level = ET.SubElement(parent, _q(DTBOOK_NS, level_name),
                              {"id": section_id, "class": SECTION_CLASSES.get(kind, "chapter")})
        levels.append(level)
        if not nested:
            current_chapter = None if initial else level
        smil_name = f"chapter-{number:04d}.smil"
        smil_names.append(smil_name)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        head = _smil_head(smil, book_id, chapter_title, elapsed)
        elapsed += mp3_duration(chapter_audio_contents[number - 1])
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"),
                                 {"id": f"seq-{section_id}"})
        units = entry["units"]
        heading_id = f"{section_id}-title"
        if not units or units[0].get("role") != "heading":
            # A title the book does not say labels navigation only.
            ET.SubElement(level, _q(DTBOOK_NS, heading_name), {"id": heading_id}).text = chapter_title
        tests: set[str] = set()
        target = ""
        note_element: ET.Element | None = None
        note_sequence: ET.Element | None = None
        note_number = None
        for text, unit in zip(chapter_paragraphs, units):
            role = unit.get("role") or "text"
            if role != "note" or unit.get("note") != note_number:
                note_element = note_sequence = None
                note_number = None
            for page in unit.get("page_breaks") or []:
                page_count += 1
                page_id, par_id = f"page-{page_count:04d}", f"par-page-{page_count:04d}"
                ET.SubElement(level, _q(DTBOOK_NS, "pagenum"), {
                    "id": page_id, "page": page_kind(page)[0], "smilref": f"smil/{smil_name}#{par_id}",
                }).text = page
                par = ET.SubElement(sequence, _q(SMIL_NS, "par"),
                                    {"id": par_id, "class": "pagenum", "customTest": "pagenum"})
                ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{page_id}"})
                page_targets.append((page, mark(f"smil/{smil_name}#{par_id}")))
                tests.add("pagenum")
            audio_path, clip_begin, clip_end = segment_clips[audio_index]
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
                                         mark(f"smil/{smil_name}#seq-note-{note_count}")))
                    tests.add("note")
                element = ET.SubElement(note_element, _q(DTBOOK_NS, "p"), {"id": f"p-{audio_index:04d}"})
                par_id = f"par-{audio_index:04d}"
                container = note_sequence
            else:
                attributes = {"id": f"p-{audio_index:04d}"}
                if role != "text":
                    attributes["class"] = "once-" + role.replace("_", "-")
                element = ET.SubElement(level, _q(DTBOOK_NS, "p"), attributes)
                par_id = f"par-{audio_index:04d}"
                container = sequence
            element.text = text
            element.set("smilref", f"smil/{smil_name}#{par_id}")
            par = ET.SubElement(container, _q(SMIL_NS, "par"), {"id": par_id})
            ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{element.get('id')}"})
            ET.SubElement(par, _q(SMIL_NS, "audio"), {
                "src": f"../audio/{audio_path}", "clipBegin": _clock(clip_begin), "clipEnd": _clock(clip_end),
            })
            reference = mark(f"smil/{smil_name}#{par_id}")
            target = target or reference
        _declare_custom_tests(head, tests)
        custom_tests_used.update(tests)
        chapter_targets.append((chapter_title, target, SECTION_CLASSES.get(kind, "chapter"), 2 if nested else 1))
        _write_xml(output_dir / "smil" / smil_name, smil, SMIL_DOCTYPE, SMIL_NS)
    # The DTBook DTD wants something after a level's heading. A section that is
    # only a heading (a part title followed at once by its first chapter) gets
    # an empty paragraph: it is not synchronized and nothing is read for it.
    for level in levels:
        if not any(_local(child.tag) not in {"pagenum", "h1", "h2"} for child in level):
            ET.SubElement(level, _q(DTBOOK_NS, "p"))
    if not len(bodymatter):
        raise ValueError("A DAISY book needs at least one chapter after its initial files.")
    return elapsed


def _write_ncx(
    path: Path, book_id: str, title: str, chapter_targets: list[tuple[str, str, str, int]],
    page_targets: list[tuple[str, str]], note_targets: list[tuple[str, str]], reading_order: dict[str, int],
    custom_tests: set[str], *, unique_page_ids: bool,
) -> None:
    """The navigation file: nested sections, pages, notes.

    ``playOrder`` follows the reading order and increases by one for each
    distinct SMIL time container (Z39.86-2005 §8.4.3): a section and the page
    marker it begins with point at different containers and get different
    numbers, while two entries pointing at the same container share one.
    """
    referenced = {reference for _label, reference, _class, _level in chapter_targets}
    referenced |= {reference for _label, reference in page_targets}
    referenced |= {reference for _label, reference in note_targets}
    ordered = sorted(referenced, key=lambda reference: reading_order.get(reference, len(reading_order)))
    play = {reference: str(position) for position, reference in enumerate(ordered, start=1)}

    ncx = ET.Element(_q(NCX_NS, "ncx"), {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"})
    ncx_head = ET.SubElement(ncx, _q(NCX_NS, "head"))
    values = [value for value in (page_kind(label)[1] for label, _reference in page_targets) if value]
    for name, content in (
        ("dtb:uid", book_id),
        ("dtb:depth", str(max((level for _label, _reference, _class, level in chapter_targets), default=1))),
        ("dtb:totalPageCount", str(len(page_targets))),
        ("dtb:maxPageNumber", str(max(values, default=0))),
    ):
        ET.SubElement(ncx_head, _q(NCX_NS, "meta"), {"name": name, "content": content})
    for test in sorted(custom_tests):
        ET.SubElement(ncx_head, _q(NCX_NS, "smilCustomTest"), {"id": test, **CUSTOM_TESTS[test]})
    doc_title = ET.SubElement(ncx, _q(NCX_NS, "docTitle"))
    ET.SubElement(doc_title, _q(NCX_NS, "text")).text = title
    nav_map = ET.SubElement(ncx, _q(NCX_NS, "navMap"))
    stack: list[tuple[int, ET.Element]] = []
    for order, (label, reference, nav_class, level) in enumerate(chapter_targets, start=1):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else nav_map
        nav_point = ET.SubElement(parent, _q(NCX_NS, "navPoint"),
                                  {"id": f"nav-{order}", "playOrder": play[reference], "class": nav_class})
        nav_label = ET.SubElement(nav_point, _q(NCX_NS, "navLabel"))
        ET.SubElement(nav_label, _q(NCX_NS, "text")).text = label
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
    _write_xml(
        path,
        ncx,
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
        NCX_NS,
    )
