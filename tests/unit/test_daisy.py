from pathlib import Path

from app.daisy.builder import DaisyBuilder
from app.daisy.validator import DaisyValidator
from app.ingestion.dtbook import DTBookParser
from app.audio.processor import AudioProcessor
from app.synchronization.synchronizer import SynchronizationEngine
from app.tts.mock import MockTTS


def test_built_package_validates_and_references_resources(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ejemplo_inicial" / "ejemplo.xml"
    book = DTBookParser().parse(source)
    sentences = list(_sentences(book))
    audio = {}
    for sentence in sentences:
        path = tmp_path / "staging" / f"{sentence.id}.wav"
        MockTTS().synthesize(sentence.text, path)
        audio[sentence.id] = AudioProcessor().inspect(path)
    points = SynchronizationEngine().synchronize(sentences, audio)
    package = DaisyBuilder().build(book, points, audio, tmp_path / "package")

    report = DaisyValidator().validate(package)
    assert report.valid
    assert len(points) == len(sentences)
    assert (package / "content" / "book.xml").exists()
    assert len(list((package / "audio").glob("*.wav"))) == len(sentences)
    assert len(list((package / "smil").glob("*.smil"))) == len(book.chapters)


def _sentences(book):
    for chapter in book.chapters:
        for paragraph in chapter.paragraphs:
            yield from paragraph.sentences
        for section in chapter.sections:
            yield from _section_sentences(section)


def _section_sentences(section):
    for paragraph in section.paragraphs:
        yield from paragraph.sentences
    for nested in section.sections:
        yield from _section_sentences(nested)
