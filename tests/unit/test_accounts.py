from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.backend.api import create_app
from app.backend.application import BackendApplication
from app.backend.storage import BookStorage


def test_authentication_library_favorites_and_progress_are_user_scoped(tmp_path: Path) -> None:
    service = BackendApplication(BookStorage(tmp_path))
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    with TestClient(create_app(service)) as client:
        assert client.get("/api/library").status_code == 401
        assert client.get("/api/library", headers={"Authorization": "Bearer invalid"}).status_code == 401
        first = client.post("/api/auth/register", json={"email": "A@EXAMPLE.COM", "password": "password-a", "display_name": "A"})
        second = client.post("/api/auth/register", json={"email": "b@example.com", "password": "password-b", "display_name": "B"})
        assert first.status_code == 201
        assert client.post("/api/auth/register", json={"email": "a@example.com", "password": "password-a"}).status_code == 400
        assert client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong"}).status_code == 401
        token_a = first.json()["access_token"]
        token_b = second.json()["access_token"]
        upload = client.post("/api/books", files={"file": ("book.xml", source.read_bytes(), "application/xml")})
        book_id = upload.json()["id"]
        job = client.post(f"/api/books/{book_id}/generate", json={"engine": "mock", "voice_id": "ES"}).json()
        for _ in range(500):
            status = client.get(f"/api/jobs/{job['id']}").json()
            if status["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        audiobook_id = status["audiobook_id"]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        assert client.post(f"/api/library/{audiobook_id}", headers=headers_a).status_code == 204
        assert client.post(f"/api/favorites/{audiobook_id}", headers=headers_a).status_code == 204
        assert len(client.get("/api/library", headers=headers_a).json()) == 1
        assert client.get("/api/library", headers=headers_b).json() == []
        assert client.get("/api/favorites", headers=headers_b).json() == []
        progress = client.put(
            f"/api/audiobooks/{audiobook_id}/progress",
            headers=headers_a,
            json={"current_sentence_id": None, "position_seconds": 1.5},
        )
        assert progress.status_code == 200
        assert client.put(
            f"/api/audiobooks/{audiobook_id}/progress",
            headers=headers_a,
            json={"current_sentence_id": "unknown", "position_seconds": 1.5},
        ).status_code == 400
        assert client.get(f"/api/audiobooks/{audiobook_id}/progress", headers=headers_b).json() is None
        assert client.get("/api/auth/me", headers=headers_a).json()["email"] == "a@example.com"
    service.shutdown()
