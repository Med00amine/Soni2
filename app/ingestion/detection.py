"""Safe document format detection and parser selection."""

from pathlib import Path

from .dtbook import DTBookParser
from .epub import EPUBParser
from .html import HTMLParser
from .parsers import BookParseError, BookParser
from .pdf import PDFParser

SUPPORTED_FORMATS = {"xml": "dtbook", "dtbook": "dtbook", "epub": "epub", "html": "html", "htm": "html", "pdf": "pdf"}


def detect_format(source: Path) -> str:
    suffix = source.suffix.lower().lstrip(".")
    detected = SUPPORTED_FORMATS.get(suffix)
    if detected is None:
        raise BookParseError("Unsupported document format.")
    if detected == "pdf" and source.read_bytes()[:5] != b"%PDF-":
        raise BookParseError("The uploaded file is not a valid PDF.")
    if detected == "epub":
        import zipfile
        if not zipfile.is_zipfile(source):
            raise BookParseError("The uploaded file is not a valid EPUB.")
    return detected


def parser_for(source: Path) -> BookParser:
    return {"dtbook": DTBookParser, "epub": EPUBParser, "html": HTMLParser, "pdf": PDFParser}[detect_format(source)]()
