"""Photo inserts: pages of photographs a book carries without page numbers.

Many books gather their photographs in an insert ("encarte", "láminas") printed
on unnumbered pages in the middle of the book. ONCE reads such an insert at
the end of the audiobook, with its photographs numbered. This module finds the
inserts and their photographs; it never describes a photograph (only the
book's own caption is read) and never moves an ordinary illustration.

Evidence decides how sure a find is:

* **strong** (``confirmed`` by the evidence itself): in a PDF, a run of pages
  that are photographs with no printed page number, between two pages whose
  printed numbers follow each other (64, the insert, 65); in an EPUB, a
  document made of images that the book itself calls an insert (its name,
  ``id``, ``class`` or ``epub:type``: encarte, láminas, plates, fotos…);
* **candidate**: image pages or documents without that evidence. They are
  reported and moved only if the planilla confirms them (``photo_insert_pages``
  for PDF page ranges, ``photo_insert_documents`` for EPUB documents).

The photographs' image files are read from the source again when the package
is built (``load_images``), so they are preserved exactly as the book has them.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import asdict, dataclass, field

# Words a book uses for a photo insert, in a file name, id, class or type.
_INSERT_WORDS = re.compile(r"(?:^|[\W_])(encartes?|l[aá]minas?|plates?|inserts?|fotos|fotograf[ií]as|photos|"
                           r"photographs|gallery|galer[ií]a|illustrations?-insert)(?:$|[\W_])", re.IGNORECASE)
# Smaller than this, an image is an ornament or a logo, not a photograph.
MIN_PHOTO_PIXELS = 120
_MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}


@dataclass
class Photo:
    number: int
    # Where the image file is in the source: {"kind": "pdf", "page", "xref"} or
    # {"kind": "epub", "href"}.
    source: dict
    # The book's own caption, "" when it has none. Never a description.
    caption: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PhotoInsert:
    photos: list[Photo]
    # "strong": the source itself shows it is an unpaginated insert;
    # "candidate": it may be one, and only a person can confirm it.
    evidence: str
    reason: str
    # Where it was: {"pdf_pages": [33, 36], "after_page": "64", "before_page": "65"} or {"document": "..."}.
    location: dict = field(default_factory=dict)

    @property
    def strong(self) -> bool:
        return self.evidence == "strong"

    def as_dict(self) -> dict:
        return {"photos": [photo.as_dict() for photo in self.photos], "evidence": self.evidence,
                "reason": self.reason, "location": self.location}


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def printed_page_label(text: str) -> str | None:
    """The page number printed on a page: a line with only a number, at its top or bottom."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in (lines[:1] + lines[-1:]) if lines else []:
        if re.fullmatch(r"\d{1,4}", line):
            return line
    return None


def _photos_on_page(page) -> list[int]:
    xrefs = []
    try:
        images = page.get_images(full=True)
    except Exception:
        return []
    for image in images:
        xref, width, height = image[0], image[2], image[3]
        if width >= MIN_PHOTO_PIXELS and height >= MIN_PHOTO_PIXELS and xref not in xrefs:
            xrefs.append(xref)
    return xrefs


def detect_pdf(pdf, profiles: list[dict]) -> list[PhotoInsert]:
    """Runs of photograph pages in a PDF, with the evidence for each.

    ``profiles`` are the extractor's page profiles (page number, characters,
    image ratio, status, text). A photograph page is mostly image with little
    text (a caption at most). A run is strong evidence when none of its pages
    prints a page number and the pages on either side print consecutive ones.
    """
    labels = {profile["page"]: printed_page_label(profile.get("_text", "")) for profile in profiles}
    is_photo = {profile["page"]: (profile["image_ratio"] >= 0.25 and profile["chars"] < 260
                                  and bool(_photos_on_page(pdf[profile["page"] - 1])))
                for profile in profiles}
    pages = [profile["page"] for profile in profiles]
    runs: list[list[int]] = []
    for number in pages:
        if is_photo[number]:
            if runs and runs[-1][-1] == number - 1:
                runs[-1].append(number)
            else:
                runs.append([number])
    inserts: list[PhotoInsert] = []
    counter = 0
    for run in runs:
        before = next((labels[number] for number in reversed(pages) if number < run[0] and labels.get(number)), None)
        after = next((labels[number] for number in pages if number > run[-1] and labels.get(number)), None)
        unnumbered = not any(labels.get(number) for number in run)
        consecutive = bool(before and after and int(after) == int(before) + 1)
        photos = []
        for number in run:
            profile = next(item for item in profiles if item["page"] == number)
            caption = " ".join((profile.get("_text") or "").split())
            xrefs = _photos_on_page(pdf[number - 1])
            for position, xref in enumerate(xrefs):
                counter += 1
                # A caption belongs to its photograph only when the page has one.
                photos.append(Photo(counter, {"kind": "pdf", "page": number, "xref": xref},
                                    caption if len(xrefs) == 1 else ""))
            if len(xrefs) > 1 and caption:
                photos[-1].caption = caption
        evidence = "strong" if unnumbered and consecutive else "candidate"
        reason = (f"páginas {run[0]}–{run[-1]} del PDF: fotografías sin número de página entre las páginas "
                  f"impresas {before} y {after}" if evidence == "strong" else
                  f"páginas {run[0]}–{run[-1]} del PDF: sobre todo imagen; no hay pruebas de que sea un encarte "
                  "sin paginar")
        inserts.append(PhotoInsert(photos, evidence, reason,
                                   {"pdf_pages": [run[0], run[-1]], "after_page": before or "",
                                    "before_page": after or ""}))
    return inserts


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #

def epub_document_insert(name: str, soup, report_words: int = 40) -> PhotoInsert | None:
    """An EPUB spine document that is a photo insert, or None.

    Strong when the document is made of images and the book names it as an
    insert; a candidate when it is made of images but says nothing about it.
    """
    body = soup.body or soup
    images = [image for image in body.find_all("img") if image.get("src")]
    if not images:
        return None
    captions = [" ".join(node.get_text(" ").split()) for node in body.find_all(["figcaption", "caption"])]
    words = len(" ".join(body.get_text(" ").split()).split())
    caption_words = len(" ".join(captions).split())
    if words - caption_words > report_words * max(1, len(images)) // 2 and words > report_words:
        return None  # a chapter with illustrations, not an insert
    hints = " ".join([name, body.get("class") and " ".join(body.get("class")) or "", body.get("id") or "",
                      body.get("epub:type") or ""] + [" ".join(node.get("class") or []) + " " + (node.get("id") or "")
                                                     + " " + (node.get("epub:type") or "")
                                                     for node in body.find_all(["section", "div", "figure"])])
    strong = bool(_INSERT_WORDS.search(hints))
    photos = []
    for number, image in enumerate(images, start=1):
        figure = image.find_parent("figure")
        caption = " ".join(figure.find("figcaption").get_text(" ").split()) if figure and figure.find("figcaption") else ""
        href = posixpath.normpath(posixpath.join(posixpath.dirname(name), image.get("src")))
        photos.append(Photo(number, {"kind": "epub", "href": href}, caption))
    return PhotoInsert(photos, "strong" if strong else "candidate",
                       f"documento {name}: {len(images)} imágenes" + (" y el libro lo llama encarte o láminas"
                                                                     if strong else ", sin nada que diga que es un encarte"),
                       {"document": name})


# --------------------------------------------------------------------------- #
# Which inserts the audiobook uses, and their images
# --------------------------------------------------------------------------- #

def _page_ranges(value) -> list[tuple[int, int]]:
    ranges = []
    for item in value or []:
        match = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*", str(item))
        if match:
            first = int(match.group(1))
            ranges.append((first, int(match.group(2) or first)))
    return ranges


def selected(inserts: list[PhotoInsert], mode: str, pages: list | None = None,
             documents: list | None = None) -> tuple[list[PhotoInsert], list[PhotoInsert]]:
    """(used, left out). ``mode``: "auto" uses strong inserts and confirmed candidates; "off" none."""
    if mode == "off":
        return [], list(inserts)
    ranges = _page_ranges(pages)
    names = {str(name) for name in documents or []}
    used, left = [], []
    for insert in inserts:
        span = insert.location.get("pdf_pages")
        confirmed = (span and any(first <= span[0] and span[-1] <= last for first, last in ranges)) or \
            insert.location.get("document") in names
        (used if insert.strong or confirmed else left).append(insert)
    return used, left


def renumber(inserts: list[PhotoInsert]) -> list[Photo]:
    """The photographs of the inserts used, numbered 1, 2, 3… in reading order."""
    photos = []
    for insert in inserts:
        for photo in insert.photos:
            photos.append(Photo(len(photos) + 1, dict(photo.source), photo.caption))
    return photos


def load_images(filename: str, content: bytes, photos: list[dict]) -> dict[int, tuple[bytes, str, str]]:
    """The image file of each photograph: number -> (bytes, extension, media type).

    Read from the source as it is; an image in a format a DAISY reader may
    not show (JPEG 2000, JBIG2…) is converted to PNG. A photograph whose
    image cannot be read is left out of the result, and the caller says so.
    """
    images: dict[int, tuple[bytes, str, str]] = {}
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix == "pdf":
        import fitz

        with fitz.open(stream=content, filetype="pdf") as pdf:
            for photo in photos:
                source = photo.get("source") or {}
                try:
                    extracted = pdf.extract_image(int(source["xref"]))
                except Exception:
                    continue
                extension = (extracted.get("ext") or "").lower()
                data = extracted.get("image") or b""
                if extension not in {"jpeg", "jpg", "png"}:
                    try:
                        pixmap = fitz.Pixmap(pdf, int(source["xref"]))
                        if pixmap.n - pixmap.alpha >= 4:
                            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                        data, extension = pixmap.tobytes("png"), "png"
                    except Exception:
                        continue
                extension = "jpg" if extension == "jpeg" else extension
                images[int(photo["number"])] = (data, extension, _MEDIA_TYPES[extension])
    elif suffix == "epub":
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = {name.split("/", 1)[-1] if "/" in name else name: name for name in archive.namelist()}
            for photo in photos:
                href = (photo.get("source") or {}).get("href", "")
                stored = next((full for short, full in names.items()
                               if full.endswith(href) or short.endswith(href)), None)
                extension = href.rsplit(".", 1)[-1].lower()
                if stored is None or extension not in _MEDIA_TYPES:
                    continue
                extension = "jpg" if extension == "jpeg" else extension
                images[int(photo["number"])] = (archive.read(stored), extension, _MEDIA_TYPES[extension])
    return images
