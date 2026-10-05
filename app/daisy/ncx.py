"""DAISY NCX navigation generation."""

from lxml import etree

from app.ingestion.models import Book

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"


def build_ncx(book: Book, chapter_files: list[tuple[str, str]]) -> bytes:
    root = etree.Element(f"{{{NCX_NS}}}ncx", nsmap={None: NCX_NS}, version="2005-1")
    head = etree.SubElement(root, f"{{{NCX_NS}}}head")
    etree.SubElement(head, f"{{{NCX_NS}}}meta", name="dtb:uid", content=book.id)
    doc_title = etree.SubElement(root, f"{{{NCX_NS}}}docTitle")
    etree.SubElement(doc_title, f"{{{NCX_NS}}}text").text = book.title or book.id
    nav_map = etree.SubElement(root, f"{{{NCX_NS}}}navMap")
    for order, (chapter_id, smil_path) in enumerate(chapter_files, start=1):
        point = etree.SubElement(nav_map, f"{{{NCX_NS}}}navPoint", id=f"nav-{chapter_id}", playOrder=str(order))
        label = etree.SubElement(point, f"{{{NCX_NS}}}navLabel")
        etree.SubElement(label, f"{{{NCX_NS}}}text").text = chapter_id
        etree.SubElement(point, f"{{{NCX_NS}}}content", src=f"{smil_path}#seq-{chapter_id}")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
