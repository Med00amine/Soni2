"""DTBook XML ingestion."""

from collections.abc import Iterator
from pathlib import Path
from lxml import etree

from .models import Book, Chapter, Metadata, Paragraph, Section, Sentence

DTBOOK_NS = "http://www.daisy.org/z3986/2005/dtbook/"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"d": DTBOOK_NS}


class DTBookParseError(ValueError):
    """Raised when a DTBook cannot be safely converted to the internal model."""


class DTBookParser:
    """Parse DTBook 2005 XML while preserving reading order and source identifiers."""

    def parse(self, source: str | Path) -> Book:
        path = Path(source)
        try:
            tree = etree.parse(
                str(path),
                etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True),
            )
        except (OSError, etree.XMLSyntaxError) as exc:
            raise DTBookParseError(f"Unable to parse DTBook XML '{path}': {exc}") from exc

        root = tree.getroot()
        if _local_name(root) != "dtbook":
            raise DTBookParseError("Root element must be dtbook.")

        metadata = self._parse_metadata(root.find("d:head", NS))
        book_node = root.find("d:book", NS)
        if book_node is None:
            raise DTBookParseError("DTBook is missing the required book element.")

        chapters = [
            self._parse_chapter(node, index)
            for index, node in enumerate(self._chapter_nodes(book_node), start=1)
        ]
        if not chapters:
            raise DTBookParseError("DTBook contains no readable chapter or section content.")

        book_id = root.get("id") or metadata.values.get("dtb:uid") or path.stem
        language = metadata.language or root.get(f"{{{XML_NS}}}lang")
        return Book(
            id=book_id,
            title=metadata.title,
            language=language,
            author=metadata.author,
            metadata=metadata,
            chapters=chapters,
        )

    def _parse_metadata(self, head: etree._Element | None) -> Metadata:
        values: dict[str, str] = {}
        if head is not None:
            for meta in head.findall("d:meta", NS):
                name = meta.get("name")
                content = meta.get("content")
                if name and content is not None:
                    values[name] = content
        return Metadata(
            title=values.get("dc:Title"),
            language=values.get("dc:Language"),
            author=values.get("dc:Creator"),
            values=values,
        )

    def _chapter_nodes(self, book_node: etree._Element) -> Iterator[etree._Element]:
        for container in book_node:
            if _local_name(container) in {"frontmatter", "bodymatter", "rearmatter"}:
                yield from (child for child in container if _local_name(child) == "level1")
        if not any(_local_name(child) in {"frontmatter", "bodymatter", "rearmatter"} for child in book_node):
            yield from (child for child in book_node if _local_name(child) == "level1")

    def _parse_chapter(self, node: etree._Element, order: int) -> Chapter:
        chapter_id = self._stable_id(node, "chapter", order)
        title = self._heading_text(node)
        paragraphs: list[Paragraph] = []
        sections: list[Section] = []
        paragraph_order = 1
        section_order = 1
        for child in node:
            name = _local_name(child)
            if name in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
                paragraph = self._parse_paragraph(child, chapter_id, paragraph_order)
                paragraphs.append(paragraph)
                paragraph_order += 1
            elif name in {"level2", "level3", "level4", "level5", "level6"}:
                sections.append(self._parse_section(child, chapter_id, section_order))
                section_order += 1
        return Chapter(id=chapter_id, title=title, paragraphs=paragraphs, sections=sections, order=order)

    def _parse_section(self, node: etree._Element, chapter_id: str, order: int) -> Section:
        section_id = self._stable_id(node, "section", order)
        paragraphs: list[Paragraph] = []
        sections: list[Section] = []
        paragraph_order = 1
        section_order = 1
        for child in node:
            name = _local_name(child)
            if name in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
                paragraphs.append(self._parse_paragraph(child, chapter_id, paragraph_order))
                paragraph_order += 1
            elif name.startswith("level") and name[5:].isdigit():
                sections.append(self._parse_section(child, chapter_id, section_order))
                section_order += 1
        return Section(
            id=section_id,
            title=self._heading_text(node),
            paragraphs=paragraphs,
            order=order,
            sections=sections,
        )

    def _parse_paragraph(self, node: etree._Element, chapter_id: str, order: int) -> Paragraph:
        paragraph_id = self._stable_id(node, "paragraph", order)
        text = _text_content(node)
        if not text:
            raise DTBookParseError(f"Readable element '{paragraph_id}' contains no text.")
        sentence_nodes = [child for child in node if _local_name(child) == "sent"]
        if sentence_nodes:
            sentences = [
                Sentence(
                    id=self._stable_id(sentence, "sentence", sentence_order),
                    text=_text_content(sentence),
                    original_text=_text_content(sentence),
                    paragraph_id=paragraph_id,
                    chapter_id=chapter_id,
                    order=sentence_order,
                )
                for sentence_order, sentence in enumerate(sentence_nodes, start=1)
            ]
        else:
            sentences = [
                Sentence(
                    id=f"{paragraph_id}-sentence-1",
                    text=text,
                    original_text=text,
                    paragraph_id=paragraph_id,
                    chapter_id=chapter_id,
                    order=1,
                )
            ]
        return Paragraph(id=paragraph_id, text=text, sentences=sentences, order=order)

    @staticmethod
    def _stable_id(node: etree._Element, prefix: str, order: int) -> str:
        return node.get("id") or f"{prefix}-{order}"

    @staticmethod
    def _heading_text(node: etree._Element) -> str | None:
        for child in node:
            if _local_name(child) in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                return _text_content(child)
        return None


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def _text_content(element: etree._Element) -> str:
    return " ".join("".join(element.itertext()).split())
