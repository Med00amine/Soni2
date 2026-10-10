import xml.etree.ElementTree as ET
import io
import zipfile

from fastapi.testclient import TestClient

from app.daisy_export import build_daisy_package
from app.book_structure import extract_book_structure
from app.daisy_validation import validate_daisy_package
from app import main as api


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

    dtbook = ET.parse(package / "book.xml").getroot()
    searchable_text = " ".join(node.text or "" for node in dtbook.iter())
    assert all(text in searchable_text for text in ("Primer parrafo.", "Segundo parrafo.", "Tercer parrafo."))
    levels = dtbook.findall(f".//{DTBOOK}level1")
    assert [level.find(f"{DTBOOK}h1").text for level in levels] == ["Introducción", "Datos bibliográficos", "Capitulo uno", "Capitulo dos"]
    assert len(list(package.glob("smil/*.smil"))) == 4
    assert len(list(package.glob("audio/*.mp3"))) == 7
    assert (package / "audio/chapter-0001.mp3").read_bytes() == b"audio-1audio-2"
    assert (package / "audio/chapter-0002.mp3").read_bytes() == b"audio-3"

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

    ncx = ET.parse(package / "book.ncx").getroot()
    nav_points = ncx.findall(f".//{NCX}navPoint")
    assert [point.find(f"{NCX}navLabel/{NCX}text").text for point in nav_points] == ["Introducción", "Datos bibliográficos", "Capitulo uno", "Capitulo dos"]
    for point in nav_points:
        assert (package / point.find(f"{NCX}content").attrib["src"].split("#", 1)[0]).is_file()

    opf = ET.parse(package / "book.opf").getroot()
    items = {item.attrib["id"]: item.attrib["href"] for item in opf.findall(f"{OPF}manifest/{OPF}item")}
    spine_refs = [item.attrib["idref"] for item in opf.findall(f"{OPF}spine/{OPF}itemref")]
    assert len(spine_refs) == 4
    assert all((package / items[ref]).is_file() for ref in spine_refs)
    dc_ns = "{http://purl.org/dc/elements/1.1/}"
    metadata = " ".join(node.text or "" for node in opf.findall(f".//{dc_ns}*"))
    assert "Autora de prueba" in metadata and "Editorial de prueba" in metadata and "2025" in metadata
    validate_daisy_package(package)


def test_pdf_derived_page_sections_create_page_navigation(tmp_path):
    package = build_daisy_package(
        "Libro paginado", ["Texto página uno", "Texto página dos"], [b"audio1", b"audio2"],
        tmp_path / "pages", chapters=[("Página 1", ["Texto página uno"]), ("Página 2", ["Texto página dos"])],
    )
    ncx = ET.parse(package / "book.ncx").getroot()
    targets = ncx.findall(f".//{NCX}pageTarget")
    assert [target.attrib["value"] for target in targets] == ["1", "2"]
    assert [target.attrib["playOrder"] for target in targets] == ["3", "4"]
    validate_daisy_package(package)


def test_validator_rejects_missing_manifest_file(tmp_path):
    package = build_daisy_package("Libro", ["Un párrafo."], [b"audio"], tmp_path / "invalid")
    (package / "audio/segment-0001.mp3").unlink()
    try:
        validate_daisy_package(package)
    except ValueError as error:
        assert "segment-0001.mp3" in str(error)
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
    issues = api.compare_transcripts("La casa roja está aquí", "La casa está aquí")
    assert issues == [{"kind": "delete", "expected": "roja", "recognized": ""}]


def test_preview_returns_extracted_text_without_tts_credentials(monkeypatch):
    monkeypatch.setattr(api.settings, "tts_provider", "elevenlabs")
    monkeypatch.setattr(api.settings, "elevenlabs_api_key", "")
    monkeypatch.setattr(api.settings, "elevenlabs_voice_id", "")
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
    async def fake_synthesize(text, voice_speed=1.0, voice_id=""):
        return f"mp3:{text}".encode("utf-8")

    monkeypatch.setattr(api, "synthesize", fake_synthesize)
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
        names = set(archive.namelist())
        assert {"book.xml", "book.ncx", "book.opf", "book.res", "book.mp3"} <= names
        assert {"audio/chapter-0001.mp3", "audio/chapter-0002.mp3", "audio/intro-confirmed.mp3", "audio/bibliography-confirmed.mp3"} <= names
        assert {"smil/chapter-0001.smil", "smil/chapter-0002.smil"} <= names
        book_text = archive.read("book.xml").decode("utf-8")
        assert all(text in book_text for text in ("Texto uno.", "Texto dos corregido.", "Texto tres.", "ONCE approved identification", "ONCE approved legal text"))
        assert "Texto dos." not in book_text
        assert archive.read("audio/segment-0002.mp3") == b"mp3:Texto dos corregido."
