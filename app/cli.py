"""Command-line entry points for the Phase 1 core."""

import argparse
import json
import logging
from pathlib import Path

from .audio.processor import AudioProcessor
from .daisy.builder import DaisyBuilder
from .daisy.validator import DaisyValidator
from .ingestion.dtbook import DTBookParser
from .synchronization.synchronizer import SynchronizationEngine
from .text.pronunciation import SpanishPronunciationProcessor
from .text.segmenter import SentenceSegmenter
from .tts.config import ProductionTTSConfig
from .tts.factory import create_tts_engine
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daisy-audiobook")
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert = subparsers.add_parser("convert", help="Build a validated DAISY package.")
    convert.add_argument("--input", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--tts", choices=["mock", "production"], default=settings.tts_engine)
    inspect_text = subparsers.add_parser("inspect-text", help="Inspect source and TTS text.")
    inspect_text.add_argument("--input", type=Path, required=True)
    subparsers.add_parser("validate", help="Validate a DAISY package.").add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect-text":
        return _inspect_text(args.input)
    if args.command == "validate":
        report = DaisyValidator().validate(args.input)
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        return 0 if report.valid else 1

    logger.info("[1/8] Parsing book...")
    book = DTBookParser().parse(args.input)
    pronunciation = SpanishPronunciationProcessor()
    segmenter = SentenceSegmenter()
    tts = create_tts_engine(
        args.tts,
        production_config=ProductionTTSConfig(
            model_path=settings.tts_model_path,
            device=settings.tts_device,
            language=settings.tts_language,
            voice=settings.tts_voice,
            sample_rate=settings.tts_sample_rate,
            max_retries=settings.tts_max_retries,
            retry_delay=settings.tts_retry_delay,
            timeout=settings.tts_timeout,
            cache_enabled=settings.tts_cache_enabled,
            force_regenerate=settings.tts_force_regenerate,
        ),
    )
    sentences = list(_book_sentences(book))
    logger.info("[2/8] Normalizing %d sentences...", len(sentences))
    logger.info("[3/8] Segmenting...")
    for sentence in sentences:
        source_text = sentence.source_text or sentence.original_text or sentence.text
        sentence.source_text = source_text
        sentence.original_text = source_text
        sentence.tts_text = pronunciation.process(source_text)
        segments = segmenter.segment(sentence.tts_text)
        sentence.tts_text = " ".join(segments)

    staging_audio = args.output / ".audio"
    staging_audio.mkdir(parents=True, exist_ok=True)
    logger.info("[4/8] Generating %s audio...", args.tts)
    audio_metadata = {}
    for sentence in sentences:
        path = staging_audio / f"{sentence.id}.wav"
        tts.synthesize(sentence.tts_text or sentence.text, path)
        metadata = AudioProcessor().inspect(path)
        audio_metadata[sentence.id] = metadata
    logger.info("[5/8] Analyzed %d audio files.", len(audio_metadata))
    logger.info("[6/8] Synchronizing...")
    points = SynchronizationEngine().synchronize(sentences, audio_metadata)
    logger.info("[7/8] Building DAISY package...")
    DaisyBuilder().build(book, points, audio_metadata, args.output)
    logger.info("[8/8] Validating...")
    report = DaisyValidator().validate(args.output)
    if args.tts == "mock" and staging_audio.exists():
        for path in staging_audio.glob("*"):
            path.unlink()
        staging_audio.rmdir()
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0 if report.valid else 1


def _book_sentences(book):
    for chapter in book.chapters:
        yield from _chapter_sentences(chapter)


def _inspect_text(input_path: Path) -> int:
    book = DTBookParser().parse(input_path)
    processor = SpanishPronunciationProcessor()
    for sentence in _book_sentences(book):
        source = sentence.source_text or sentence.original_text or sentence.text
        print(json.dumps({"id": sentence.id, "source_text": source, "tts_text": processor.process(source)}, ensure_ascii=False))
    return 0


def _chapter_sentences(chapter):
    for paragraph in chapter.paragraphs:
        yield from paragraph.sentences
    for section in chapter.sections:
        yield from _section_sentences(section)


def _section_sentences(section):
    for paragraph in section.paragraphs:
        yield from paragraph.sentences
    for nested in section.sections:
        yield from _section_sentences(nested)


if __name__ == "__main__":
    raise SystemExit(main())
