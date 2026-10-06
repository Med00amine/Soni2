from pathlib import Path
import time

from app.backend.application import BackendApplication
from app.backend.storage import BookStorage
from app.database.repositories import generation_key
from app.ingestion.detection import parser_for


def _wait(service: BackendApplication, job_id: str):
    for _ in range(500):
        status = service.job(job_id)
        if status.status in {"completed", "failed"}:
            return status
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_generation_key_is_deterministic_and_varies_by_voice(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    book = parser_for(source).parse(source)
    assert generation_key(book, "mock", "ES") == generation_key(book, "mock", "ES")
    assert generation_key(book, "mock", "ES") != generation_key(book, "mock", "OTHER")


def test_duplicate_upload_and_generation_reuses_catalog_audiobook(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    service = BackendApplication(BookStorage(tmp_path))
    first = service.upload(source.read_bytes(), "first.xml")
    second = service.upload(source.read_bytes(), "renamed.xml")

    assert first.id == second.id
    first_job = service.start_generation(first.id, "mock", "ES")
    assert _wait(service, first_job.id).status == "completed"
    second_job = service.start_generation(second.id, "mock", "ES")

    assert second_job.id == first_job.id
    assert second_job.status == "completed"
    assert len(service.audiobooks()) == 1
    service.shutdown()


def test_different_voice_creates_distinct_audiobook_variant(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    service = BackendApplication(BookStorage(tmp_path))
    book = service.upload(source.read_bytes(), "book.xml")
    first = service.start_generation(book.id, "mock", "ES")
    assert _wait(service, first.id).status == "completed"
    second = service.start_generation(book.id, "mock", "OTHER")
    assert _wait(service, second.id).status == "completed"
    assert len(service.audiobooks()) == 2
    service.shutdown()
