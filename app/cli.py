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
from .text.normalizer import TextNormalizer
from .text.segmenter import SentenceSegmenter
from .tts.mock import MockTTS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daisy-audiobook")
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert = subparsers.add_parser("convert", help="Build a validated DAISY package with MockTTS.")
    convert.add_argument("--input", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("validate", help="Validate a DAISY package.").add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        report = DaisyValidator().validate(args.input)
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        return 0 if report.valid else 1

    logger.info("[1/8] Parsing book...")
    book = DTBookParser().parse(args.input)
    normalizer = TextNormalizer()
    segmenter = SentenceSegmenter()
    tts = MockTTS()
    sentences = list(_book_sentences(book))
    logger.info("[2/8] Normalizing %d sentences...", len(sentences))
    logger.info("[3/8] Segmenting...")
    for sentence in sentences:
        sentence.tts_text = normalizer.normalize(sentence.original_text or sentence.text)
        segments = segmenter.segment(sentence.tts_text)
        sentence.tts_text = " ".join(segments)

    staging_audio = args.output / ".audio"
    staging_audio.mkdir(parents=True, exist_ok=True)
    logger.info("[4/8] Generating mock audio...")
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
    if staging_audio.exists():
        for path in staging_audio.glob("*"):
            path.unlink()
        staging_audio.rmdir()
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0 if report.valid else 1


def _book_sentences(book):
    for chapter in book.chapters:
        yield from _chapter_sentences(chapter)


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
