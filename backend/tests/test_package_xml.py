"""Every XML file of the package: indented, valid, with the style sheet ONCE's books declare.

Also the two folder layouts: ``nested`` (audio/ and smil/, as delivered until
now) and ``flat`` (every file beside the OPF). Both must pass the same checks:
structure, official DTDs, timing, completeness and the ONCE structure.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import audio_master, completeness, daisy_dtd, daisy_timing, once_structure, once_validation, planilla
from app.book_structure import extract_book_structure
from app.daisy_export import DTBOOK_STYLESHEET, build_daisy_package, make_paragraphs
from app.daisy_validation import validate_daisy_package
from app.narration_plan import apply_narration_selection, build_narration_plan

CORPUS = Path(__file__).parent / "corpus"
DEMO = CORPUS / "el-faro-de-la-isla-almendra.xml"
FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413
MP3 = FRAME * 40
ONCE_DECLARATION = '<?xml-stylesheet href="dtbookbasic.css" type="text/css"?>'
SHEET = {"file2_page": "2", "index_page": "3", "index_depth": 1, "production_date": "octubre de 2026"}
DTBOOK_NS = "{http://www.daisy.org/z3986/2005/dtbook/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"


def _once(tmp_path: Path, layout_name: str, **options) -> tuple[Path, object]:
    structure = extract_book_structure(DEMO.name, DEMO.read_bytes())
    plan = build_narration_plan([(c.title, c.paragraphs, c.title_generated) for c in structure.chapters])
    selected = apply_narration_selection(plan, None)
    metadata = {"title": structure.title, "author": structure.author}
    built = once_structure.build(selected, once_structure.selected_provenance(selected, structure), metadata,
                                 planilla.from_dict(SHEET))
    chapters, layout = [], []
    for section, record in zip(built.sections, built.records()):
        texts = [paragraph.text for paragraph in section.paragraphs]
        chapters.append((section.title, ([section.title] if section.heading_spoken else [])
                         + [part for text in texts for part in make_paragraphs(text)]))
        layout.append({"kind": section.kind, "level": section.level,
                       "units": once_structure.unit_layout(record, texts, section.heading_spoken, make_paragraphs)})
    segments = [MP3 for _title, body in chapters for _text in body]
    package = build_daisy_package(built.resolved.title, [text for _t, body in chapters for text in body], segments,
                                  tmp_path / layout_name, chapters=chapters, author=built.resolved.author,
                                  layout=layout, package_layout_name=layout_name, **options)
    return package, built


def _records(built) -> list[dict]:
    return [{"title": section.title, "paragraphs": [paragraph.text for paragraph in section.paragraphs],
             "title_generated": section.title_generated, "once": record}
            for section, record in zip(built.sections, built.records())]


def _xml_files(package: Path) -> list[Path]:
    return sorted(path for path in package.rglob("*") if path.suffix in {".xml", ".smil", ".ncx", ".opf", ".res"})


# --------------------------------------------------------------------------- #
# Indentation (requirement I)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("layout_name", ["nested", "flat"])
def test_every_xml_file_is_indented_two_spaces_per_level(tmp_path, layout_name) -> None:
    package, _built = _once(tmp_path, layout_name)
    files = _xml_files(package)
    assert {path.suffix for path in files} == {".xml", ".smil", ".ncx", ".opf", ".res"}
    for path in files:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == '<?xml version="1.0" encoding="UTF-8"?>', path.name
        assert lines[1].startswith("<!DOCTYPE "), path.name
        assert len(lines) > 5, f"{path.name} is on one line"
        # One element per line, nested two spaces deeper than its parent.
        body = [line for line in lines if line.strip() and not line.startswith(("<?", "<!"))]
        assert all((len(line) - len(line.lstrip(" "))) % 2 == 0 for line in body), path.name
        assert max(len(line) - len(line.lstrip(" ")) for line in body) >= 4, path.name
        ET.parse(path)  # still well formed


def test_indentation_changes_no_text_and_no_attribute(tmp_path) -> None:
    package, built = _once(tmp_path, "nested")
    dtbook = ET.parse(next(package.glob("*.xml"))).getroot()
    # The text of every heading and paragraph is exactly the narrated text: no
    # whitespace was added inside it.
    synchronized = [element for element in dtbook.iter() if element.get("smilref")
                    and element.tag != f"{DTBOOK_NS}pagenum"]
    for element in synchronized:
        text = "".join(element.itertext())
        assert text == text.strip() and "\n" not in text, text
    # Namespaces and the attributes the structure depends on are all there.
    assert dtbook.tag == f"{DTBOOK_NS}dtbook" and dtbook.get("version") == "2005-3"
    assert dtbook.get("{http://www.w3.org/XML/1998/namespace}lang") == "es-ES"
    assert all(element.get("smilref", "").startswith("smil/") for element in synchronized)
    checks = once_validation.verify(package, _records(built), built.resolved.as_dict())
    assert {check["status"] for check in checks} == {"passed"}, checks


# --------------------------------------------------------------------------- #
# The style sheet (requirement H)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("layout_name", ["nested", "flat"])
def test_the_dtbook_declares_the_style_sheet_once_and_the_package_carries_it(tmp_path, layout_name) -> None:
    package, _built = _once(tmp_path, layout_name)
    dtbook = next(package.glob("*.xml"))
    text = dtbook.read_text(encoding="utf-8")
    # Exactly ONCE's declaration, once, in the prolog: after the DOCTYPE, before the root.
    assert text.count("<?xml-stylesheet") == 1
    lines = text.splitlines()
    assert lines[2] == ONCE_DECLARATION
    assert lines[3].startswith("<dtbook ")
    # The file it names is beside the DTBook (the path is relative to it) and in the manifest.
    css = dtbook.parent / DTBOOK_STYLESHEET
    assert css.is_file() and "pagenum" in css.read_text(encoding="utf-8")
    items = {item.get("href"): item.get("media-type") for item in ET.parse(next(package.glob("*.opf"))).getroot()
             .iter(f"{OPF}item")}
    assert items[DTBOOK_STYLESHEET] == "text/css"
    # No other document declares a style sheet.
    assert all("<?xml-stylesheet" not in path.read_text(encoding="utf-8") for path in _xml_files(package)
               if path != dtbook)
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"


def test_the_style_sheet_can_name_its_medium_or_be_left_out(tmp_path) -> None:
    with_media, _ = _once(tmp_path / "a", "nested", stylesheet_media="screen")
    assert ('<?xml-stylesheet href="dtbookbasic.css" type="text/css" media="screen"?>'
            in next(with_media.glob("*.xml")).read_text(encoding="utf-8"))
    without, _ = _once(tmp_path / "b", "nested", stylesheet=None)
    assert "<?xml-stylesheet" not in next(without.glob("*.xml")).read_text(encoding="utf-8")
    assert not (without / DTBOOK_STYLESHEET).exists()
    assert DTBOOK_STYLESHEET not in next(without.glob("*.opf")).read_text(encoding="utf-8")
    validate_daisy_package(without)


# --------------------------------------------------------------------------- #
# Folder layouts (requirement J)
# --------------------------------------------------------------------------- #

def test_the_nested_layout_is_unchanged_by_default(tmp_path) -> None:
    package, _built = _once(tmp_path, "nested")
    assert sorted(path.name for path in package.iterdir() if path.is_dir()) == ["audio", "smil"]
    assert sorted((package / "audio").iterdir()) and sorted((package / "smil").iterdir())


def test_the_flat_layout_has_no_folder_and_every_reference_resolves(tmp_path) -> None:
    package, built = _once(tmp_path, "flat")
    assert not [path for path in package.iterdir() if path.is_dir()]
    names = {path.name for path in package.iterdir()}
    assert {"chapter-001.mp3", "chapter-0001.smil", DTBOOK_STYLESHEET} <= names
    # Chapter audio and the complete audiobook are both still there.
    full = next(item for item in ET.parse(next(package.glob("*.opf"))).getroot().iter(f"{OPF}item")
                if item.get("id") == "full-audiobook")
    assert (package / full.get("href")).is_file()
    smil = (package / "chapter-0004.smil").read_text(encoding="utf-8")
    assert 'src="chapter-004.mp3"' in smil and "../" not in smil
    validate_daisy_package(package)
    assert daisy_dtd.validate_package(package)["status"] == "passed"
    assert daisy_timing.summary(daisy_timing.verify(package))["status"] == "passed"
    checks = once_validation.verify(package, _records(built), built.resolved.as_dict())
    assert {check["status"] for check in checks} == {"passed"}, checks


@pytest.mark.skipif(not audio_master.available(), reason="needs ffmpeg")
def test_both_layouts_pass_the_whole_chain_with_real_audio(tmp_path) -> None:
    import subprocess

    def tone(name: str, seconds: float) -> bytes:
        target = tmp_path / f"{name}.mp3"
        subprocess.run([audio_master.ffmpeg_path(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", "-ar", "44100",
                        "-c:a", "libmp3lame", "-q:a", "2", str(target)], check=True, capture_output=True)
        return target.read_bytes()

    chapters = [("Capítulo 1", ["Capítulo 1", "Amanecía."]), ("Capítulo 2", ["Capítulo 2", "Llovía."])]
    units, audio = [], []
    for chapter_index, (_title, body) in enumerate(chapters):
        for text in body:
            data = tone(f"u{len(units)}", 0.8 + 0.1 * len(units))
            path = tmp_path / f"u{len(units)}.mp3"
            path.write_bytes(data)
            audio.append(data)
            units.append({"sentence_id": f"s{len(units)}", "chapter_index": chapter_index, "text": text,
                          "state": "verified", "audio_path": path.name})
    records = [{"title": title, "paragraphs": body[1:], "title_generated": False} for title, body in chapters]
    lead, tail = audio_master.MasteringOptions().edges
    for layout_name in ("nested", "flat"):
        package = build_daisy_package("Libro", [text for _t, body in chapters for text in body], audio,
                                      tmp_path / f"pkg-{layout_name}", chapters=chapters,
                                      mastering=audio_master.MasteringOptions(), package_layout_name=layout_name)
        chain = completeness.verify(records, units, tmp_path, package, set(),
                                    timing={"leading": lead, "trailing": tail, "bitrate_kbps": 128, "chapter_gap": 2.0})
        failed = [check for check in chain["checks"] if check["status"] != "passed"]
        assert not failed, (layout_name, failed)


def test_an_unknown_layout_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError):
        build_daisy_package("Libro", ["Uno."], [MP3], tmp_path / "pkg", package_layout_name="deep")


def test_a_production_job_can_deliver_the_flat_layout(tmp_path, monkeypatch) -> None:
    import asyncio
    import io
    import json
    import zipfile

    from fastapi.testclient import TestClient

    from app import jobs as job_store
    from app import main
    from app.audio_cache import AudioCache

    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
        "quality_review_enabled": True, "daisy_package_layout": "flat"}))
    monkeypatch.setattr(main, "storage_root", tmp_path / "data")
    monkeypatch.setattr(main, "database_path", tmp_path / "data" / "jobs.sqlite3")
    monkeypatch.setattr(main, "audio_cache", AudioCache(tmp_path / "data" / "audio-cache"))
    monkeypatch.setattr(main, "_start_or_resume_job", lambda job_id: asyncio.sleep(0))
    book = "Capítulo 1\n\nAmanecía despacio.\n\nCapítulo 2\n\nLa casa tenía ventanas.\n"
    client = TestClient(main.app)
    response = client.post("/api/books/jobs", files={"file": ("libro.txt", io.BytesIO(book.encode()), "text/plain")})
    job_id = response.json()["job_id"]
    asyncio.run(main.run_production_job(job_id))
    job = job_store.get_job(main.database_path, job_id)
    assert job["state"] == "completed", job.get("error")
    job_dir = Path(job["source_path"]).parent
    chain = json.loads((job_dir / "completeness.json").read_text(encoding="utf-8"))
    assert chain["status"] == "passed", chain
    with zipfile.ZipFile(job_dir / f"{job_id}-once-delivery.zip") as archive:
        names = archive.namelist()
        report = archive.read("quality-report.html").decode("utf-8")
    package_files = [name for name in names if name.startswith("DAISY 3/")]
    # One folder for the whole package, nothing inside it nested further.
    assert all(name.count("/") == 1 for name in package_files), package_files
    assert "DAISY 3/chapter-001.mp3" in names and "DAISY 3/dtbookbasic.css" in names
    # The report plays each paragraph from the flat package.
    assert "DAISY 3/chapter-001.mp3#t=" in report and "DAISY 3/audio/" not in report
