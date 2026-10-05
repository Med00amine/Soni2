from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.backend.api import create_app
from app.backend.application import BackendApplication
from app.backend.storage import BookStorage


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(BackendApplication(BookStorage(tmp_path))))


def test_upload_returns_book_metadata_and_chapters(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    with _client(tmp_path) as client:
        response = client.post(
            "/api/books",
            files={"file": ("ejemplo.xml", source.read_bytes(), "application/xml")},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["title"]
        assert body["chapter_count"] > 0
        assert client.get(f"/api/books/{body['id']}/chapters").status_code == 200


def test_upload_rejects_unsupported_files(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/books", files={"file": ("book.pdf", b"not a dtbook")})

    assert response.status_code == 415
    assert "traceback" not in response.text.lower()


def test_audio_path_traversal_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/books/unknown/audio/..%2Fbook.xml")

    assert response.status_code == 404


def test_mock_generation_job_completes_and_validates(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    with _client(tmp_path) as client:
        upload = client.post(
            "/api/books",
            files={"file": ("ejemplo.xml", source.read_bytes(), "application/xml")},
        )
        book_id = upload.json()["id"]
        queued = client.post(
            f"/api/books/{book_id}/generate",
            json={"engine": "mock", "voice_id": "ES"},
        )
        assert queued.status_code == 202
        job_id = queued.json()["id"]
        for _ in range(500):
            status = client.get(f"/api/jobs/{job_id}").json()
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert status["status"] == "completed"
        assert status["stage"] == "Completed"
        assert client.get(f"/api/audiobooks/{book_id}/daisy").status_code == 200


def test_generation_failure_is_terminal_and_safe(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    service = BackendApplication(BookStorage(tmp_path))
    book = service.upload(source.read_bytes(), "ejemplo.xml")
    job = service.start_generation(book.id, "invalid", "ES")
    for _ in range(500):
        status = service.job(job.id)
        if status.status in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert status.status == "failed"
    assert status.error == "Unable to generate the audiobook."
    service.shutdown()
