"""EPUB 3 reader using the package manifest and spine order."""

from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from lxml import etree, html

from .models import Chapter
from .parsers import BookParseError, make_book, make_paragraph


class EPUBParser:
    def parse(self, source: str | Path):
        path = Path(source)
        try:
            with ZipFile(path) as archive:
                if "mimetype" not in archive.namelist() or archive.read("mimetype") != b"application/epub+zip":
                    raise BookParseError("The file is not a valid EPUB.")
                for name in archive.namelist():
                    member = PurePosixPath(name)
                    if member.is_absolute() or ".." in member.parts:
                        raise BookParseError("The EPUB contains an unsafe archive path.")
                container = etree.fromstring(archive.read("META-INF/container.xml"))
                rootfile = container.xpath("//*[local-name()='rootfile']/@full-path")
                if not rootfile:
                    raise BookParseError("EPUB package metadata is missing.")
                opf_path = rootfile[0]
                opf_dir = Path(opf_path).parent
                opf = etree.fromstring(archive.read(opf_path))
                metadata = {etree.QName(node).localname: " ".join("".join(node.itertext()).split())
                            for node in opf.xpath("//*[local-name()='metadata']/*") if "".join(node.itertext()).strip()}
                manifest = {
                    item.get("id"): item.get("href")
                    for item in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']")
                }
                chapters = []
                for order, item in enumerate(opf.xpath("//*[local-name()='spine']/*[local-name()='itemref']"), 1):
                    href = manifest.get(item.get("idref"))
                    if not href:
                        continue
                    archive_path = PurePosixPath(opf_dir.as_posix()) / href.split("#", 1)[0]
                    document = html.fromstring(archive.read(archive_path.as_posix()))
                    for node in document.xpath("//script|//style|//nav|//*[@hidden]"):
                        node.drop_tree()
                    chapter_id = f"chapter-{order}"
                    paragraphs = [
                        make_paragraph(" ".join(node.itertext()), f"{chapter_id}-paragraph-{index}", chapter_id, index)
                        for index, node in enumerate(document.xpath("//p"), 1)
                        if " ".join(node.itertext()).strip()
                    ]
                    if paragraphs:
                        heading = " ".join(document.xpath("string(//h1|//h2|//title)").split()) or f"Chapter {order}"
                        chapters.append(Chapter(id=chapter_id, title=heading, paragraphs=paragraphs, order=order))
                return make_book(path, title=metadata.get("title"), author=metadata.get("creator"),
                                 language=metadata.get("language"), chapters=chapters, source_format="epub",
                                 metadata=metadata)
        except (OSError, BadZipFile, KeyError, etree.XMLSyntaxError) as exc:
            raise BookParseError(f"Unable to parse EPUB '{path}'.") from exc
