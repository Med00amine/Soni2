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


def _write_xml(path: Path, root: ET.Element, doctype: str | None = None) -> None:
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
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
        return b"".join(segments)
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
    mastering: audio_master.MasteringOptions | None = None,
) -> Path:
    """Write a DAISY 3 package whose files carry the book's own name.

    Audio lives once per chapter, in ``audio/chapter-NNN/chapter-NNN.mp3``;
    each paragraph's SMIL entry points at its stretch of that file with
    clipBegin/clipEnd rather than at a separate per-paragraph MP3. When
    ``clips_out`` is given it receives, for every segment in order, the
    package-relative chapter audio path and the clip's start and end seconds.
    """
    chapters = chapters or [(title, paragraphs)]
    paragraphs = [paragraph for _, body in chapters for paragraph in body]
    if not paragraphs or len(paragraphs) != len(audio_segments):
        raise ValueError("Text and audio segments do not match.")

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
    if mastering is not None and mastering.active:
        # The chapters are already levelled; the book only needs the longer
        # pause between them so a listener hears where one ends.
        full_audio = audio_master.master_book(ordered_audio, mastering).audio
    else:
        full_audio = _join_mp3(ordered_audio, use_ffmpeg=use_ffmpeg)
    (output_dir / full_audio_name).write_bytes(full_audio)
    if clips_out is not None:
        clips_out.extend(segment_clips)

    ET.register_namespace("", DTBOOK_NS)
    dtbook = ET.Element(
        _q(DTBOOK_NS, "dtbook"),
        {"version": "2005-3", _q(XML_NS, "lang"): "es-ES"},
    )
    head = ET.SubElement(dtbook, _q(DTBOOK_NS, "head"))
    ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": "dc:Title", "content": title})
    book = ET.SubElement(dtbook, _q(DTBOOK_NS, "book"))
    bodymatter = ET.SubElement(book, _q(DTBOOK_NS, "bodymatter"))
    if author:
        ET.SubElement(head, _q(DTBOOK_NS, "meta"), {"name": "dc:Creator", "content": author})
    smil_names: list[str] = []
    chapter_targets: list[tuple[str, str, str]] = []
    page_targets: list[tuple[int, str]] = []
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
        ET.register_namespace("", SMIL_NS)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        smil_head = ET.SubElement(smil, _q(SMIL_NS, "head"))
        ET.SubElement(smil_head, _q(SMIL_NS, "meta"), {"name": "dc:Title", "content": label})
        sequence = ET.SubElement(ET.SubElement(smil, _q(SMIL_NS, "body")), _q(SMIL_NS, "seq"), {"id": f"seq-{front_id}"})
        par = ET.SubElement(sequence, _q(SMIL_NS, "par"), {"id": par_id})
        ET.SubElement(par, _q(SMIL_NS, "text"), {"src": f"../{dtbook_name}#{paragraph_id}"})
        ET.SubElement(par, _q(SMIL_NS, "audio"), {
            "src": f"../audio/{filename}", "clipBegin": _clock(0), "clipEnd": _clock(mp3_duration(_audio)),
        })
        _write_xml(output_dir / "smil" / smil_name, smil,
                   '<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-1//EN" "http://www.daisy.org/z3986/2005/dtbsmil-2005-1.dtd">')
        chapter_targets.append((label, f"smil/{smil_name}#{par_id}", "frontmatter"))
    audio_index = 0
    for chapter_index, (chapter_title, chapter_paragraphs) in enumerate(chapters, start=1):
        if not chapter_paragraphs:
            continue
        chapter_id = f"chapter-{chapter_index}"
        smil_name = f"chapter-{chapter_index:04d}.smil"
        smil_names.append(smil_name)
        level = ET.SubElement(bodymatter, _q(DTBOOK_NS, "level1"), {"id": chapter_id})
        heading_id = f"{chapter_id}-title"
        heading = ET.SubElement(level, _q(DTBOOK_NS, "h1"), {"id": heading_id})
        heading.text = chapter_title

        ET.register_namespace("", SMIL_NS)
        smil = ET.Element(_q(SMIL_NS, "smil"), {_q(XML_NS, "lang"): "es-ES"})
        smil_head = ET.SubElement(smil, _q(SMIL_NS, "head"))
        ET.SubElement(smil_head, _q(SMIL_NS, "meta"), {"name": "dc:Title", "content": chapter_title})
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
        chapter_targets.append((chapter_title, f"smil/{smil_name}#{first_par_id}", "chapter"))
        page_match = re.fullmatch(r"P[aá]gina\s+(\d+)", chapter_title.strip(), flags=re.IGNORECASE)
        if page_match:
            page_targets.append((int(page_match.group(1)), f"smil/{smil_name}#{first_par_id}"))
        _write_xml(output_dir / "smil" / smil_name, smil,
                   '<!DOCTYPE smil PUBLIC "-//NISO//DTD dtbsmil 2005-1//EN" "http://www.daisy.org/z3986/2005/dtbsmil-2005-1.dtd">')

    _write_xml(
        output_dir / dtbook_name,
        dtbook,
        '<!DOCTYPE dtbook PUBLIC "-//NISO//DTD dtbook 2005-3//EN" "http://www.daisy.org/z3986/2005/dtbook-2005-3.dtd">',
    )
    ET.register_namespace("", NCX_NS)
    ncx = ET.Element(_q(NCX_NS, "ncx"), {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"})
    ncx_head = ET.SubElement(ncx, _q(NCX_NS, "head"))
    for name, content in (
        ("dtb:uid", book_id),
        ("dtb:depth", "1"),
        ("dtb:totalPageCount", str(len(page_targets))),
        ("dtb:maxPageNumber", str(max((number for number, _ in page_targets), default=0))),
    ):
        ET.SubElement(ncx_head, _q(NCX_NS, "meta"), {"name": name, "content": content})
    doc_title = ET.SubElement(ncx, _q(NCX_NS, "docTitle"))
    ET.SubElement(doc_title, _q(NCX_NS, "text")).text = title
    nav_map = ET.SubElement(ncx, _q(NCX_NS, "navMap"))
    for order, (chapter_title, target, nav_class) in enumerate(chapter_targets, start=1):
        nav_point = ET.SubElement(nav_map, _q(NCX_NS, "navPoint"),
                                  {"id": f"nav-{order}", "playOrder": str(order), "class": nav_class})
        nav_label = ET.SubElement(nav_point, _q(NCX_NS, "navLabel"))
        ET.SubElement(nav_label, _q(NCX_NS, "text")).text = chapter_title
        ET.SubElement(nav_point, _q(NCX_NS, "content"), {"src": target})
    if page_targets:
        page_list = ET.SubElement(ncx, _q(NCX_NS, "pageList"))
        page_label = ET.SubElement(page_list, _q(NCX_NS, "navLabel"))
        ET.SubElement(page_label, _q(NCX_NS, "text")).text = "Páginas"
        for order, (page_number, target) in enumerate(page_targets, start=len(chapter_targets) + 1):
            page_target = ET.SubElement(page_list, _q(NCX_NS, "pageTarget"), {
                "id": f"page-{page_number}", "value": str(page_number),
                "type": "normal", "playOrder": str(order),
            })
            page_nav_label = ET.SubElement(page_target, _q(NCX_NS, "navLabel"))
            ET.SubElement(page_nav_label, _q(NCX_NS, "text")).text = str(page_number)
            ET.SubElement(page_target, _q(NCX_NS, "content"), {"src": target})
    _write_xml(
        output_dir / ncx_name,
        ncx,
        '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">',
    )

    ET.register_namespace("", RESOURCE_NS)
    resources = ET.Element(
        _q(RESOURCE_NS, "resources"),
        {"version": "2005-1", _q(XML_NS, "lang"): "es-ES"},
    )
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
    )

    ET.register_namespace("", OPF_NS)
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
    spine = ET.SubElement(package, _q(OPF_NS, "spine"), {"toc": "ncx"})
    for index, _name in enumerate(smil_names, start=1):
        ET.SubElement(spine, _q(OPF_NS, "itemref"), {"idref": f"smil-{index:04d}"})
    _write_xml(
        output_dir / opf_name,
        package,
        '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN" "oebpkg12.dtd">',
    )
    return output_dir
