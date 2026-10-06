"""Conservative semantic HTML/XHTML ingestion."""

from pathlib import Path

from lxml import html

from .models import Chapter
from .parsers import BookParseError, make_book, make_paragraph


class HTMLParser:
    def parse(self, source: str | Path):
        path = Path(source)
        try:
            root = html.parse(str(path)).getroot()
        except (OSError, html.ParserError) as exc:
            raise BookParseError(f"Unable to parse HTML '{path}'.") from exc
        for node in root.xpath("//script|//style|//noscript|//nav|//*[@hidden]"):
            node.drop_tree()
        title = _text(root.xpath("//title")) or path.stem
        chapters: list[Chapter] = []
        containers = root.xpath("//article|//section")
        if not containers:
            containers = [root]
        for chapter_index, container in enumerate(containers, 1):
            heading = _text(container.xpath("./h1|./h2|./h3")) or (
                title if chapter_index == 1 else f"Chapter {chapter_index}"
            )
            chapter_id = f"chapter-{chapter_index}"
            paragraphs = [
                make_paragraph(_text([paragraph]), f"{chapter_id}-paragraph-{index}", chapter_id, index)
                for index, paragraph in enumerate(container.xpath(".//p"), 1)
                if _text([paragraph])
            ]
            if paragraphs:
                chapters.append(Chapter(id=chapter_id, title=heading, paragraphs=paragraphs, order=chapter_index))
        return make_book(path, title=title, author=None, language=root.get("lang"), chapters=chapters, source_format="html")


def _text(nodes) -> str:
    return " ".join(" ".join(node.itertext()) for node in nodes).strip() if nodes else ""
