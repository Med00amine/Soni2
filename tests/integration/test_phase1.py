from pathlib import Path

from app.ingestion.dtbook import DTBookParser
from app.tts.mock import MockTTS


def test_provided_ejemplo_parses_and_generates_audio(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    book = DTBookParser().parse(source)
    sentence = book.chapters[0].paragraphs[0].sentences[0]
    output = MockTTS().synthesize(sentence.text, tmp_path / "sentence.wav")

    assert sentence.id == "id_3"
    assert output.exists()
    assert output.stat().st_size > 44
