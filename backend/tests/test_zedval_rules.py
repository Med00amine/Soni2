"""ZedVal's Z39.86-2005 requirements, checked on built packages (app.zedval_rules).

A package Soni2 builds must meet every requirement; and each rule must catch
the fault it exists for, including the ones that made ONCE's validator report
"timing errors" on the first deliveries: variable-bitrate chapters without a
length header (clips "after the end of the file"), and elapsed times summed
from files instead of clips.
"""

from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import daisy_dtd, once_structure, planilla, zedval_rules
from app.book_structure import extract_book_structure
from app.daisy_export import build_daisy_package, first_frame_estimate, make_paragraphs, playable_length
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

CORPUS = Path(__file__).parent / "corpus"
DEMO = CORPUS / "el-faro-de-la-isla-almendra.xml"
SHEET = {"file2_page": "2", "index_page": "3", "index_depth": 1, "production_date": "octubre de 2026",
         "absys": "0369940", "isbn": "978-84-0000-000-0"}
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
RES = "{http://www.daisy.org/z3986/2005/resource/}"
DC = "{http://purl.org/dc/elements/1.1/}"


def cbr_frames(count: int, bitrate_index: int = 9) -> bytes:
    """MPEG-1 layer III frames at one bitrate (128 kbit/s), 44.1 kHz, padded as LAME pads them.

    The padding byte is added whenever the stream falls behind the exact
    bitrate, so the file's size is its length times the bitrate, as a real
    constant-bitrate encoder's is.
    """
    kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)[bitrate_index]
    exact = 144 * kbps * 1000 / 44100
    frames, written = [], 0
    for number in range(1, count + 1):
        padding = 1 if written + int(exact) + 1 <= round(number * exact) else 0
        size = int(exact) + padding
        frames.append(bytes([0xFF, 0xFB, (bitrate_index << 4) | (padding << 1), 0x64]) + b"\x00" * (size - 4))
        written += size
    return b"".join(frames)


SEGMENT = cbr_frames(48)  # 1.254 s


def _records(built) -> list[dict]:
    return [{"title": section.title, "paragraphs": [paragraph.text for paragraph in section.paragraphs],
             "title_generated": section.title_generated, "once": record}
            for section, record in zip(built.sections, built.records())]


def _package(tmp_path: Path, layout_name: str = "nested", **options) -> Path:
    """The demo book with the ONCE structure (pages included), as a production builds it."""
    structure = extract_book_structure(DEMO.name, DEMO.read_bytes())
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure),
                                 {"title": structure.title, "author": structure.author}, planilla.from_dict(SHEET))
    chapters, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        chapters.append((section.title, ([section.title] if section.heading_spoken else [])
                         + [part for text in texts for part in make_paragraphs(text)]))
        layout.append({"kind": section.kind, "level": section.level,
                       "units": once_structure.unit_layout(record, texts, section.heading_spoken, make_paragraphs)})
    segments = [SEGMENT for _title, body in chapters for _text in body]
    return build_daisy_package(built.resolved.title, [text for _t, body in chapters for text in body], segments,
                               tmp_path / layout_name, chapters=chapters, author=built.resolved.author,
                               layout=layout, package_layout_name=layout_name, identifiers=[("ABSYS", "0369940")],
                               identifier="soni2-absys-0369940", dtb_publisher="ONCE", source_isbn=SHEET["isbn"],
                               publisher="Editorial Faro", publication_year="2024", edition="Primera edición",
                               narrator="Síntesis de voz - Prueba (simulada)", **options)


def _failed(checks: list[dict]) -> set[str]:
    return {problem["test"] for check in checks for problem in check["problems"]
            if problem.get("level", "requirement") == "requirement"}


# --------------------------------------------------------------------------- #
# A package Soni2 builds meets every requirement
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("layout_name", ["nested", "flat"])
def test_a_built_package_meets_every_zedval_requirement(tmp_path, layout_name) -> None:
    package = _package(tmp_path, layout_name)
    checks = zedval_rules.verify(package)
    assert not _failed(checks), [check for check in checks if check["status"] != "passed"]
    assert {check["status"] for check in checks} == {"passed"}, checks
    navigation = next(check for check in checks if check["id"] == "zedval_navigation")
    assert navigation["pages"] > 0  # the demo book's pages are there, and counted right
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"


def test_the_package_metadata_is_the_daisy_books_and_the_print_books_is_its_source(tmp_path) -> None:
    package = _package(tmp_path)
    opf = ET.parse(next(package.glob("*.opf"))).getroot()
    dc = {element.tag[len(DC):]: element.text for element in opf.iter() if element.tag.startswith(DC)
          and element.get("scheme") != "ABSYS"}
    meta = {element.get("name"): element.get("content") for element in opf.iter(f"{OPF}meta")}
    assert dc["Publisher"] == "ONCE" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dc["Date"])
    assert dc["Language"] == "es-ES" and dc["Source"] == SHEET["isbn"]
    assert dc["Identifier"] == "soni2-absys-0369940"
    assert meta["dtb:producedDate"] == dc["Date"] and meta["dtb:producer"] == "Soni2"
    assert meta["dtb:sourcePublisher"] == "Editorial Faro" and meta["dtb:sourceDate"] == "2024"
    assert meta["dtb:sourceEdition"] == "Primera edición" and meta["dtb:narrator"].startswith("Síntesis de voz")
    # The same identifier everywhere: a rebuild of the same book keeps it.
    for path in [next(package.glob("*.ncx")), next(package.glob("*.xml"))]:
        assert 'content="soni2-absys-0369940"' in path.read_text(encoding="utf-8")


def test_the_ncx_says_its_headings_with_the_books_audio(tmp_path) -> None:
    package = _package(tmp_path)
    ncx = ET.parse(next(package.glob("*.ncx"))).getroot()
    assert ncx.find(f"{NCX}docAuthor/{NCX}text") is not None
    assert ncx.find(f"{NCX}docTitle/{NCX}audio") is not None  # the title file's heading
    points = list(ncx.iter(f"{NCX}navPoint"))
    spoken = [point for point in points if point.find(f"{NCX}navLabel/{NCX}audio") is not None]
    assert spoken and len(spoken) >= len(points) - 1
    # Each label's audio is a stretch of the file its section plays, and pages stay silent.
    for point in spoken:
        audio = point.find(f"{NCX}navLabel/{NCX}audio")
        assert (package / audio.get("src")).is_file()
        assert zedval_rules.clock_ms(audio.get("clipEnd")) > zedval_rules.clock_ms(audio.get("clipBegin"))
    assert all(target.find(f"{NCX}navLabel/{NCX}audio") is None for target in ncx.iter(f"{NCX}pageTarget"))


def test_the_resource_file_names_every_skippable_structure(tmp_path) -> None:
    package = _package(tmp_path)
    res = ET.parse(next(package.glob("*.res"))).getroot()
    selects = {node.get("select") for node in res.iter(f"{RES}nodeSet")}
    for book_struct in ("PAGE_NUMBER", "NOTE", "NOTE_REFERENCE", "ANNOTATION", "LINE_NUMBER", "OPTIONAL_SIDEBAR",
                        "OPTIONAL_PRODUCER_NOTE"):
        assert f"//smilCustomTest[@bookStruct='{book_struct}']" in selects
    pages = next(node for node in res.iter(f"{RES}nodeSet") if "PAGE_NUMBER" in node.get("select"))
    labels = {resource.get("{http://www.w3.org/XML/1998/namespace}lang"): resource.findtext(f"{RES}text")
              for resource in pages}
    assert labels == {"es": "página", "en": "page"}


# --------------------------------------------------------------------------- #
# Each rule catches its fault
# --------------------------------------------------------------------------- #

@pytest.fixture()
def good(tmp_path) -> Path:
    return _package(tmp_path / "built")


def _copy(package: Path, tmp_path: Path) -> Path:
    target = tmp_path / "broken"
    shutil.copytree(package, target)
    return target


def _edit(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def test_an_elapsed_time_a_millisecond_off_is_caught(good, tmp_path) -> None:
    package = _copy(good, tmp_path)
    smil = sorted(package.rglob("*.smil"))[1]
    declared = next(meta.get("content") for meta in ET.parse(smil).getroot().iter(f"{SMIL}meta")
                    if meta.get("name") == "dtb:totalElapsedTime")
    millis = zedval_rules.clock_ms(declared) + 1
    _edit(smil, f'content="{declared}"', f'content="0:00:{millis // 1000:02d}.{millis % 1000:03d}"')
    assert "smil_metaAudioDtbTotElaAccurate" in _failed(zedval_rules.verify(package))


def test_a_total_time_a_millisecond_off_is_caught(good, tmp_path) -> None:
    package = _copy(good, tmp_path)
    opf = next(package.glob("*.opf"))
    declared = re.search(r'name="dtb:totalTime" content="([^"]+)"', opf.read_text(encoding="utf-8")).group(1)
    millis = zedval_rules.clock_ms(declared) + 1
    _edit(opf, f'content="{declared}"', f'content="0:{millis // 60000:02d}:{millis % 60000 // 1000:02d}.{millis % 1000:03d}"')
    assert "opf_xMetaTotTimeAccurate" in _failed(zedval_rules.verify(package))


def test_a_variable_bitrate_chapter_without_a_length_header_ends_after_its_audio(good, tmp_path) -> None:
    """The fault of the first deliveries: ZedVal reads the whole file at its first frame's bitrate."""
    package = _copy(good, tmp_path)
    chapter = next(package.rglob("chapter-004.mp3"))
    frames = round(chapter.stat().st_size / (144 * 128000 / 44100))
    # Same length in frames, but a loud first frame at 320 kbit/s and the rest at 32 kbit/s.
    chapter.write_bytes(cbr_frames(1, bitrate_index=14) + cbr_frames(frames - 1, bitrate_index=1))
    failed = _failed(zedval_rules.verify(package))
    assert "smil_clipEndBeforeEOF" in failed


def test_missing_or_invalid_publisher_and_date_are_caught(good, tmp_path) -> None:
    package = _copy(good, tmp_path)
    opf = next(package.glob("*.opf"))
    text = opf.read_text(encoding="utf-8")
    text = re.sub(r"\s*<dc:Publisher>[^<]*</dc:Publisher>", "", text)
    text = re.sub(r"<dc:Date>[^<]*</dc:Date>", "<dc:Date>noviembre de 2026</dc:Date>", text)
    opf.write_text(text, encoding="utf-8")
    assert {"opf_DcPubPresent", "opf_DcDateValue"} <= _failed(zedval_rules.verify(package))


def test_a_file_in_the_manifest_that_nothing_plays_is_caught(good, tmp_path) -> None:
    """The complete audiobook used to be listed in the manifest: not a file of the DAISY book."""
    package = _copy(good, tmp_path)
    opf = next(package.glob("*.opf"))
    shutil.copyfile(next(package.rglob("chapter-001.mp3")), package / "libro-completo.mp3")
    _edit(opf, "<manifest>", '<manifest>\n    <item id="full-audiobook" href="libro-completo.mp3" '
                             'media-type="audio/mpeg" />')
    checks = zedval_rules.verify(package)
    assert "opf_mnfDTBmembers" in _failed(checks)
    with pytest.raises(ValueError):
        validate_daisy_package(package)


def test_a_skippable_structure_without_a_resource_entry_is_caught(good, tmp_path) -> None:
    package = _copy(good, tmp_path)
    res = next(package.glob("*.res"))
    text = res.read_text(encoding="utf-8")
    res.write_text(re.sub(r"<nodeSet id=\"page-set\".*?</nodeSet>", "", text, flags=re.S), encoding="utf-8")
    assert "resource_skipStructResEntry" in _failed(zedval_rules.verify(package))


def test_a_page_count_that_disagrees_with_the_page_list_is_caught(good, tmp_path) -> None:
    package = _copy(good, tmp_path)
    ncx = next(package.glob("*.ncx"))
    count = re.search(r'name="dtb:totalPageCount" content="(\d+)"', ncx.read_text(encoding="utf-8")).group(1)
    _edit(ncx, f'name="dtb:totalPageCount" content="{count}"', f'name="dtb:totalPageCount" content="{int(count) + 1}"')
    assert "ncx_metaDtbtotPageCountValueCorrelated" in _failed(zedval_rules.verify(package))


# --------------------------------------------------------------------------- #
# ZedVal's arithmetic
# --------------------------------------------------------------------------- #

def test_clock_values_are_read_as_smilclock_reads_them() -> None:
    assert zedval_rules.clock_ms("0:01:02.345") == 62345
    assert zedval_rules.clock_ms("1:02.5") == 62500
    assert zedval_rules.clock_ms("12.5s") == 12500
    assert zedval_rules.clock_ms("300ms") == 300
    assert zedval_rules.clock_ms("npt=0:00:01.000") == 1000
    assert zedval_rules.clock_ms("doce") is None


def test_the_end_of_an_mp3_is_its_size_over_the_first_frames_bitrate(tmp_path) -> None:
    audio = cbr_frames(400)
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x23" + b"\x00" * 35  # a 45-byte ID3v2 tag, as ffmpeg writes
    path = tmp_path / "chapter.mp3"
    path.write_bytes(tag + audio)
    end, vbr = zedval_rules.mp3_end_ms(path)
    # Tags count as audio for it: the whole file at 16 000 bytes a second.
    assert not vbr and end == int((len(tag) + len(audio)) / 16000 * 1000)
    seconds, _vbr = first_frame_estimate(tag + audio, len(tag) + len(audio))
    assert abs(seconds - 400 * 1152 / 44100) < 0.005


def test_clips_never_pass_the_end_a_first_frame_reader_computes() -> None:
    # Frames never padded: 417 bytes each, a little under 128 kbit/s. A reader
    # that trusts the first frame finds the file 0.2 % short; the clips stop there.
    unpadded = bytes([0xFF, 0xFB, 0x90, 0x64]) + b"\x00" * 413
    audio = unpadded * 400
    real = 400 * 1152 / 44100
    estimate, _vbr = first_frame_estimate(audio, len(audio))
    assert estimate < real
    assert playable_length(audio) == estimate
    # A file misread by much more than a frame is not constant bitrate: its real length stands.
    loud_first = cbr_frames(1, bitrate_index=14) + cbr_frames(399, bitrate_index=1)
    assert playable_length(loud_first) > first_frame_estimate(loud_first, len(loud_first))[0]
