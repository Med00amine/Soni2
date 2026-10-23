import xml.etree.ElementTree as ET
import io
import zipfile
import pytest

from fastapi.testclient import TestClient

from app.daisy_export import build_daisy_package
from app.book_structure import extract_book_structure
from app.daisy_validation import validate_daisy_package
from app import main as api
from app import providers
from app import sentences as sentences_module
from app import book_structure


DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"


def test_multichapter_package_has_matching_text_audio_navigation_and_metadata(tmp_path):
    source = b'''<book><title>Libro de prueba</title><creator>Autora de prueba</creator><edition>2</edition><date>2025</date><publisher>Editorial de prueba</publisher><chapter><title>Capitulo uno</title><p>Primer parrafo.</p><p>Segundo parrafo.</p></chapter><chapter><title>Capitulo dos</title><p>Tercer parrafo.</p></chapter></book>'''
    structure = extract_book_structure("libro.xml", source, "fallback")
    chapters = [(chapter.title, chapter.paragraphs) for chapter in structure.chapters]
    assert len(chapters) == 2
    assert structure.author == "Autora de prueba"
    package = build_daisy_package(
        structure.title,
        [paragraph for _, body in chapters for paragraph in body],
        [b"audio-1", b"audio-2", b"audio-3"],
        tmp_path / "package",
        chapters=chapters,
        author=structure.author,
        edition=structure.edition,
        publication_year=structure.publication_year,
        publisher=structure.publisher,
        front_matter=[
            ("Introducción", "Título: Libro de prueba. Autor: Autora de prueba. [texto ONCE aprobado]", b"intro-audio", "intro.mp3"),
            ("Datos bibliográficos", "[texto bibliográfico aprobado]", b"biblio-audio", "biblio.mp3"),
        ],
    )

    # The package files carry the book's own name instead of "book".
    assert sorted(path.name for path in package.iterdir() if path.is_file()) == [
        "Libro_de_prueba.mp3", "Libro_de_prueba.ncx", "Libro_de_prueba.opf", "Libro_de_prueba.res",
        "Libro_de_prueba.xml",
    ]
    dtbook = ET.parse(package / "Libro_de_prueba.xml").getroot()
    searchable_text = " ".join(node.text or "" for node in dtbook.iter())
    assert all(text in searchable_text for text in ("Primer parrafo.", "Segundo parrafo.", "Tercer parrafo."))
    levels = dtbook.findall(f".//{DTBOOK}level1")
    assert [level.find(f"{DTBOOK}h1").text for level in levels] == ["Introducción", "Datos bibliográficos", "Capitulo uno", "Capitulo dos"]
    assert len(list(package.glob("smil/*.smil"))) == 4
    # Flat audio/ holds only the two front-matter files: there is no per-paragraph
    # MP3, and each chapter's audio lives once in its own directory.
    assert sorted(path.name for path in package.glob("audio/*.mp3")) == ["biblio.mp3", "intro.mp3"]
    assert (package / "audio/chapter-001/chapter-001.mp3").read_bytes() == b"audio-1audio-2"
    assert (package / "audio/chapter-002/chapter-002.mp3").read_bytes() == b"audio-3"
    assert not list(package.glob("audio/chapter-0*.mp3"))

    smil_par_ids = set()
    for smil_path in package.glob("smil/*.smil"):
        smil = ET.parse(smil_path).getroot()
        for par in smil.findall(f".//{SMIL}par"):
            smil_par_ids.add((smil_path.relative_to(package).as_posix(), par.attrib["id"]))
            for element in (par.find(f"{SMIL}text"), par.find(f"{SMIL}audio")):
                target = (smil_path.parent / element.attrib["src"].split("#", 1)[0]).resolve()
                assert target.is_file()

    for paragraph in dtbook.findall(f".//{DTBOOK}p"):
        smil_name, fragment = paragraph.attrib["smilref"].split("#", 1)
        assert (smil_name, fragment) in smil_par_ids

    ncx = ET.parse(package / "Libro_de_prueba.ncx").getroot()
    nav_points = ncx.findall(f".//{NCX}navPoint")
    assert [point.find(f"{NCX}navLabel/{NCX}text").text for point in nav_points] == ["Introducción", "Datos bibliográficos", "Capitulo uno", "Capitulo dos"]
    for point in nav_points:
        assert (package / point.find(f"{NCX}content").attrib["src"].split("#", 1)[0]).is_file()

    opf = ET.parse(package / "Libro_de_prueba.opf").getroot()
    items = {item.attrib["id"]: item.attrib["href"] for item in opf.findall(f"{OPF}manifest/{OPF}item")}
    spine_refs = [item.attrib["idref"] for item in opf.findall(f"{OPF}spine/{OPF}itemref")]
    assert len(spine_refs) == 4
    assert all((package / items[ref]).is_file() for ref in spine_refs)
    dc_ns = "{http://purl.org/dc/elements/1.1/}"
    metadata = " ".join(node.text or "" for node in opf.findall(f".//{dc_ns}*"))
    assert "Autora de prueba" in metadata and "Editorial de prueba" in metadata and "2025" in metadata
    validate_daisy_package(package)


def test_chapter_audio_folders_and_full_audiobook_preserve_order_without_front_matter(tmp_path):
    chapters = [
        ("Capitulo uno", ["Uno. Dos.", "Tres."]),
        ("Capitulo dos", ["Cuatro."]),
    ]
    segments = [b"audio-1", b"audio-2", b"audio-3"]
    package = build_daisy_package(
        "Libro de orden", [text for _, body in chapters for text in body], segments,
        tmp_path / "ordered", chapters=chapters,
    )

    assert (package / "audio/chapter-001/chapter-001.mp3").read_bytes() == b"audio-1audio-2"
    assert (package / "audio/chapter-002/chapter-002.mp3").read_bytes() == b"audio-3"
    assert (package / "Libro_de_orden.mp3").read_bytes() == b"audio-1audio-2audio-3"
    # Only the chapter files and the full book: no per-paragraph segments.
    assert not list(package.rglob("segment-*.mp3"))
    assert sum(1 for path in package.rglob("*.mp3")) == 3
    ncx = ET.parse(package / "Libro_de_orden.ncx").getroot()
    labels = [node.find(f"{NCX}navLabel/{NCX}text").text for node in ncx.findall(f".//{NCX}navPoint")]
    assert labels == ["Capitulo uno", "Capitulo dos"]


def test_spoken_chapter_title_is_synchronized_to_dtbook_heading(tmp_path):
    chapters = [("Capítulo 1. La llegada", ["Capítulo 1. La llegada", "Amanece en la isla."])]
    package = build_daisy_package(
        "Libro", [text for _, body in chapters for text in body], [b"heading-audio", b"body-audio"],
        tmp_path / "spoken-heading", chapters=chapters,
    )
    dtbook = ET.parse(package / "Libro.xml").getroot()
    heading = dtbook.find(f".//{DTBOOK}h1")
    assert heading.text == "Capítulo 1. La llegada"
    assert heading.attrib["smilref"].endswith("#par-heading-0001")
    assert dtbook.find(f".//{DTBOOK}p").text == "Amanece en la isla."
    smil = ET.parse(package / "smil/chapter-0001.smil").getroot()
    heading_par = smil.find(f".//{SMIL}par[@id='par-heading-0001']")
    # The heading is the first stretch of the chapter's own audio file.
    heading_audio = heading_par.find(f"{SMIL}audio").attrib
    assert heading_audio["src"] == "../audio/chapter-001/chapter-001.mp3"
    assert heading_audio["clipBegin"] == "0:00:00.000"
    assert len(smil.findall(f".//{SMIL}par")) == 2


def _mp3(frames: int) -> bytes:
    """A real MP3 of silence: MPEG-1 Layer III, 128 kbit/s, 44.1 kHz."""
    return (b"\xff\xfb\x90\x64" + b"\x00" * 413) * frames


def test_mp3_duration_is_counted_from_frames_even_after_an_id3_tag() -> None:
    from app.daisy_export import mp3_duration
    # 1152 samples per frame at 44.1 kHz.
    assert abs(mp3_duration(_mp3(100)) - 100 * 1152 / 44100) < 1e-9
    tagged = b"ID3\x03\x00\x00\x00\x00\x00\x05hello" + _mp3(10)
    assert abs(mp3_duration(tagged) - 10 * 1152 / 44100) < 1e-9


def test_each_paragraph_plays_its_own_stretch_of_the_chapter_audio(tmp_path):
    chapters = [("Capitulo uno", ["Uno.", "Dos.", "Tres."]), ("Capitulo dos", ["Cuatro."])]
    segments = [_mp3(10), _mp3(20), _mp3(30), _mp3(40)]
    clips: list = []
    package = build_daisy_package(
        "El faro", [text for _, body in chapters for text in body], segments,
        tmp_path / "clips", chapters=chapters, clips_out=clips,
    )
    frame = 1152 / 44100
    smil = ET.parse(package / "smil/chapter-0001.smil").getroot()
    audio = [par.find(f"{SMIL}audio").attrib for par in smil.findall(f".//{SMIL}par")]
    assert {item["src"] for item in audio} == {"../audio/chapter-001/chapter-001.mp3"}
    # Consecutive, gapless clips: each begins where the previous one ended.
    assert [(item["clipBegin"], item["clipEnd"]) for item in audio] == [
        ("0:00:00.000", "0:00:00.261"), ("0:00:00.261", "0:00:00.784"), ("0:00:00.784", "0:00:01.567"),
    ]
    # The next chapter starts again at zero in its own file.
    second = ET.parse(package / "smil/chapter-0002.smil").getroot().find(f".//{SMIL}audio").attrib
    assert second["src"] == "../audio/chapter-002/chapter-002.mp3" and second["clipBegin"] == "0:00:00.000"
    assert clips[2][0] == "chapter-001/chapter-001.mp3"
    assert clips[2][1:] == pytest.approx((30 * frame, 60 * frame))
    opf = ET.parse(package / "El_faro.opf").getroot()
    total = opf.find(f".//{OPF}meta[@name='dtb:totalTime']").attrib["content"]
    assert total == "0:00:02.612"
    validate_daisy_package(package)


def test_package_names_are_portable_versions_of_the_title() -> None:
    from app.daisy_export import package_basename
    assert package_basename("Spanish Short Stories for Beginners Book 1") == "Spanish_Short_Stories_for_Beginners_Book_1"
    assert package_basename("Cien años de soledad: edición") == "Cien_anos_de_soledad_edicion"
    assert package_basename("¿?") == "libro"


def test_epub_skips_cover_and_navigation_and_keeps_real_chapters(monkeypatch):
    class FakeItem:
        def __init__(self, name, content):
            self.name, self.content = name, content

        def get_type(self):
            return 9  # ebooklib.ITEM_DOCUMENT

        def get_name(self):
            return self.name

        def get_content(self):
            return self.content.encode("utf-8")

    items = {
        "cover": FakeItem("portada.xhtml", "<html><h1>El faro</h1><p>Texto de portada.</p></html>"),
        "nav": FakeItem("nav.xhtml", "<html><nav><h1>Contenido</h1><ol><li>Capítulo 1</li></ol></nav></html>"),
        "c1": FakeItem("chapter1.xhtml", "<html><h1>Capítulo 1. La llegada</h1><p>Amanece en la isla.</p></html>"),
        "c2": FakeItem("chapter2.xhtml", "<html><h1>Capítulo 2. La tormenta</h1><p>Llega la tormenta.</p></html>"),
    }

    class FakeBook:
        spine = [(name, "yes") for name in ("cover", "nav", "c1", "c2")]

        def get_item_with_id(self, item_id):
            return items[item_id]

        def get_metadata(self, *_args):
            return []

    monkeypatch.setattr(book_structure.epub, "read_epub", lambda _path: FakeBook())
    monkeypatch.setattr(book_structure.ebooklib, "ITEM_DOCUMENT", 9)
    result = book_structure.extract_book_structure("sample.epub", b"epub bytes")

    assert [(chapter.title, chapter.paragraphs) for chapter in result.chapters] == [
        ("Capítulo 1. La llegada", ["Amanece en la isla."]),
        ("Capítulo 2. La tormenta", ["Llega la tormenta."]),
    ]


def test_sentence_segmentation_assigns_stable_ids_and_keeps_source_text(tmp_path):
    source = "Dra. López llegó a las 9:30. ¡Sí! La cifra 3,14 importa."
    sentences = sentences_module.segment_sentences(source, "book-hash", 2)

    assert [sentence["text"] for sentence in sentences] == [
        "Dra. López llegó a las 9:30.", "¡Sí!", "La cifra 3,14 importa.",
    ]
    assert all(sentence["sentence_id"] for sentence in sentences)
    assert sentences == sentences_module.segment_sentences(source, "book-hash", 2)
    assert "Dra. López" in source


def test_pdf_derived_page_sections_create_page_navigation(tmp_path):
    package = build_daisy_package(
        "Libro paginado", ["Texto página uno", "Texto página dos"], [b"audio1", b"audio2"],
        tmp_path / "pages", chapters=[("Página 1", ["Texto página uno"]), ("Página 2", ["Texto página dos"])],
    )
    ncx = ET.parse(package / "Libro_paginado.ncx").getroot()
    targets = ncx.findall(f".//{NCX}pageTarget")
    assert [target.attrib["value"] for target in targets] == ["1", "2"]
    assert [target.attrib["playOrder"] for target in targets] == ["3", "4"]
    validate_daisy_package(package)


def test_validator_rejects_missing_manifest_file(tmp_path):
    package = build_daisy_package("Libro", ["Un párrafo."], [b"audio"], tmp_path / "invalid")
    (package / "audio/chapter-001/chapter-001.mp3").unlink()
    try:
        validate_daisy_package(package)
    except ValueError as error:
        assert "chapter-001.mp3" in str(error)
    else:
        raise AssertionError("The validator should reject a missing referenced audio file")


def test_alignment_is_grouped_as_phrases_for_reader_highlighting():
    chars = list("Hola mundo. Sigo aquí!")
    starts = [index * 0.1 for index in range(len(chars))]
    ends = [(index + 1) * 0.1 for index in range(len(chars))]
    phrases = api._phrases_from_alignment(chars, starts, ends, 0, 0)
    assert [phrase["text"].strip() for phrase in phrases] == ["Hola mundo.", "Sigo aquí!"]
    assert phrases[0]["start"] == 0
    assert phrases[0]["end"] > phrases[0]["start"]


def test_transcript_comparison_reports_possible_words_without_claiming_certainty():
    source = "La casa roja está aquí"
    issues = api.compare_transcripts(source, "La casa está aquí")
    assert len(issues) == 1
    issue = issues[0]
    assert issue["kind"] == "delete"
    assert issue["expected"] == "roja"
    assert issue["recognized"] == ""
    assert issue["category"] == "likely_mismatch"
    # Offsets let the interface mark the missing word in place.
    assert source[issue["expected_start"]:issue["expected_end"]] == "roja"


def test_production_sentence_quality_accepts_spanish_number_pronunciation():
    assert api._sentence_quality("147", "ciento cuarenta y siete") == ("verified", [], 1.0)
    assert api._sentence_quality("18 km", "dieciocho kilómetros")[0] == "verified"
    assert api._sentence_quality("147", "ciento cuarenta y ocho")[0] == "likely_error"


def test_preview_returns_extracted_text_without_tts_credentials(monkeypatch):
    monkeypatch.setattr(api.settings, "tts_provider", "fishaudio")
    monkeypatch.setattr(api.settings, "fish_audio_api_key", "")
    monkeypatch.setattr(api.settings, "fish_audio_voice_id", "")
    response = TestClient(api.app).post(
        "/api/books/preview",
        files={"file": ("libro.txt", io.BytesIO("Capitulo uno. Este libro contiene suficiente texto.".encode()), "text/plain")},
    )

    assert response.status_code == 200
    result = response.json()
    assert "Capitulo uno" in result["transcript"]
    assert result["audio_base64"] == ""
    assert result["audio_error"]


def test_legacy_text_extractor_uses_same_canonical_chapter_parser():
    source = b"<book><title>Book</title><chapter><title>Part one</title><p>One paragraph.</p></chapter></book>"
    structure = extract_book_structure("book.xml", source, "")

    assert api.extract_text("book.xml", source) == structure.as_text()


def test_package_api_creates_full_multichapter_zip_without_external_tts(monkeypatch):
    # Checks the package's structure and text, not its audio processing: the
    # plain byte join keeps each paragraph's tag findable in the chapter audio.
    monkeypatch.setattr(api.filesystem, "which", lambda _name: None)
    async def fake_synthesize(text, voice_speed=1.0, voice_id="", **_kwargs):
        return providers.mock_audio(text)

    monkeypatch.setattr(api, "synthesize", fake_synthesize)
    # The endpoint validates credentials and the voice before parsing the book;
    # the offline mock provider satisfies both without an account.
    monkeypatch.setattr(api.settings, "tts_provider", "mock")
    monkeypatch.setattr(api.settings, "tts_voice_id", "mock-es-es-female")
    monkeypatch.setattr(api.settings, "once_intro_template", "{title}. {author}. {once_identification_statement}")
    monkeypatch.setattr(api.settings, "once_bibliographic_template", "{title}. {author}. {edition}. {publication_year}. {publisher}. {legal_statement}")
    monkeypatch.setattr(api.settings, "once_identification_statement", "ONCE approved identification")
    monkeypatch.setattr(api.settings, "once_legal_statement", "ONCE approved legal text")
    monkeypatch.setattr(api.settings, "once_intro_filename", "intro-confirmed.mp3")
    monkeypatch.setattr(api.settings, "once_bibliographic_filename", "bibliography-confirmed.mp3")
    source = b"""<book><title>Libro integrado</title><creator>Autora</creator><edition>3</edition><date>2026</date><publisher>Editorial</publisher><chapter><title>Capitulo 1</title><p>Texto uno.</p><p>Texto dos.</p></chapter><chapter><title>Capitulo 2</title><p>Texto tres.</p></chapter></book>"""

    response = TestClient(api.app).post(
        "/api/books/package",
        files={"file": ("libro.xml", io.BytesIO(source), "application/xml")},
        data={"corrections_json": '{"0:1":"Texto dos corregido."}'},
    )

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        # Same layout as the reviewed delivery: everything inside "DAISY 3/".
        names = {name.removeprefix("DAISY 3/") for name in archive.namelist()}
        assert all(name.startswith("DAISY 3/") for name in archive.namelist())
        base = "Libro_integrado"
        assert {f"{base}.xml", f"{base}.ncx", f"{base}.opf", f"{base}.res", f"{base}.mp3"} <= names
        assert {"audio/chapter-001/chapter-001.mp3", "audio/chapter-002/chapter-002.mp3",
                "audio/intro-confirmed.mp3", "audio/bibliography-confirmed.mp3"} <= names
        assert not any("segment-" in name for name in names)
        assert {"smil/chapter-0001.smil", "smil/chapter-0002.smil"} <= names
        book_text = archive.read(f"DAISY 3/{base}.xml").decode("utf-8")
        assert all(text in book_text for text in ("Texto uno.", "Texto dos corregido.", "Texto tres.", "ONCE approved identification", "ONCE approved legal text"))
        assert "Texto dos." not in book_text
        # The corrected paragraph's audio is part of its chapter file.
        # The corrected paragraph is in the chapter audio: its text travels in
        # the mock provider's tag, which a plain byte join preserves.
        assert b"Texto dos corregido." in archive.read("DAISY 3/audio/chapter-001/chapter-001.mp3")


def test_one_voices_clips_are_joined_by_copying_and_anything_else_is_re_encoded(monkeypatch):
    import subprocess
    from pathlib import Path

    from app import daisy_export

    commands = []

    def fake_ffmpeg(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"joined")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(daisy_export.shutil, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(daisy_export.subprocess, "run", fake_ffmpeg)

    def codec() -> str:
        return commands[-1][commands[-1].index("-c:a") + 1]

    assert daisy_export.join_mp3([_mp3(10), b"ID3\x04\x00\x00\x00\x00\x00\x00" + _mp3(20)]) == b"joined"
    # Same stream format: frames are copied, nothing is re-encoded or lost.
    assert codec() == "copy"
    daisy_export.join_mp3([_mp3(10), b"not an mp3"])
    assert codec() == "libmp3lame"
    # Without ffmpeg the files are concatenated as before.
    assert daisy_export.join_mp3([b"a", b"b"], use_ffmpeg=False) == b"ab"
