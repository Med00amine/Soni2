import io
import json
import zipfile

from fastapi.testclient import TestClient

from app import main as api


def test_preview_and_package_use_the_same_selected_narration_plan(monkeypatch):
    source = "<book><title>Prueba</title><chapter><title>Indice</title><p>Capitulo 1 .......... 2</p></chapter><chapter><title>Capitulo 1</title><p>La historia comienza aquí, con suficiente texto para una muestra legible.</p></chapter></book>".encode("utf-8")
    client = TestClient(api.app)
    structure = client.post("/api/books/structure", files={"file": ("prueba.xml", io.BytesIO(source), "application/xml")})
    assert structure.status_code == 200, structure.text
    selected_ids = [section["id"] for section in structure.json()["sections"]
                    if section["category"] == "main" and section["included"]]
    form_data = {"narration_plan_json": json.dumps({"selected_ids": selected_ids})}
    monkeypatch.setattr(api.settings, "fish_audio_api_key", "")
    monkeypatch.setattr(api.settings, "fish_audio_voice_id", "")
    preview = client.post("/api/books/preview", files={"file": ("prueba.xml", io.BytesIO(source), "application/xml")}, data=form_data)
    assert preview.status_code == 200, preview.text
    # The preview is the book's first paragraphs, exactly what the download says.
    assert preview.json()["transcript"].startswith("La historia comienza aquí")
    assert "Indice" not in preview.json()["transcript"]

    async def fake_synthesize(text, voice_speed=1.0, voice_id="", **_kwargs):
        return f"audio:{text}".encode()

    async def fake_preview_synthesis(text, voice_speed=1.0, voice_id=""):
        return b"preview-audio", None

    monkeypatch.setattr(api, "synthesize", fake_synthesize)
    monkeypatch.setattr(api, "synthesize_with_alignment", fake_preview_synthesis)
    # The package endpoint checks credentials and the voice up front; the
    # offline mock provider satisfies both without an account.
    monkeypatch.setattr(api.settings, "tts_provider", "mock")
    monkeypatch.setattr(api.settings, "tts_voice_id", "mock-es-es-female")
    package = client.post("/api/books/package", files={"file": ("prueba.xml", io.BytesIO(source), "application/xml")}, data=form_data)
    assert package.status_code == 200, package.text
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        opf = next(name for name in archive.namelist() if name.count("/") == 1 and name.endswith(".opf"))
        spoken = archive.read(opf.removesuffix(".opf") + ".xml").decode("utf-8")
        assert "La historia comienza aquí" in spoken
        assert "Capitulo 1 .......... 2" not in spoken
