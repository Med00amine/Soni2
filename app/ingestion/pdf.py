"""Conservative text-only PDF ingestion; OCR is intentionally unsupported."""

from pathlib import Path

from .models import Chapter
from .parsers import BookParseError, make_book, make_paragraph


class PDFParser:
    def parse(self, source: str | Path):
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(source))
            if not reader.pages:
                raise BookParseError("The PDF contains no pages.")
            chapters = []
            for order, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                paragraphs = [
                    make_paragraph(block, f"page-{order}-paragraph-{index}", f"page-{order}", index)
                    for index, block in enumerate(text.split("\n\n"), 1) if block.strip()
                ]
                if paragraphs:
                    chapters.append(Chapter(id=f"page-{order}", title=f"Page {order}",
                                            paragraphs=paragraphs, order=order))
            return make_book(Path(source), title=Path(source).stem, author=None, language=None,
                             chapters=chapters, source_format="pdf")
        except BookParseError:
            raise
        except Exception as exc:
            raise BookParseError(f"Unable to extract readable text from PDF '{source}'.") from exc
