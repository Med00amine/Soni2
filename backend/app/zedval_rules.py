"""The Z39.86-2005 conformance rules of ZedVal, checked on a built DAISY 3 package.

ZedVal is the DAISY Consortium's conformance validator for Z39.86 digital
talking books: the one DAISY Pipeline runs as "Z3986 DTB Validator". A package
that breaks one of its requirements comes back from a library such as ONCE
with errors, so the same rules run here on every package before it is released.

The rules are ZedVal's own, taken from its test catalogue
(``zedval.jar``, ``org/daisy/zedval/zedsuite/v2005/maps``) and, where the
catalogue only names a rule, from its code, with its arithmetic:

* **Times.** A SMIL file lasts the sum of its clips, ``clipEnd - clipBegin``,
  in whole milliseconds. ``dtb:totalElapsedTime`` must equal the sum of the
  files before it and the OPF's ``dtb:totalTime`` the sum of all of them,
  exactly: ZedVal's default tolerance is 0 ms.
* **End of an MP3.** ZedVal reads the first frame. With a Xing header it takes
  the frame count; without one it assumes a constant bitrate and takes the file
  size in bytes over that first frame's bitrate in bytes per second, truncated
  to the millisecond. No ``clipEnd`` may pass it (``smil_clipEndBeforeEOF``,
  ``ncx_clipEndBeforeEOF``). A variable-bitrate file without a Xing header is
  therefore misread: that is what made the clips of the first deliveries
  "end after the audio".
* **Members of the book.** SMIL, NCX and resource files belong to the book by
  themselves; any other file in the manifest must be referred to by one of
  them or by the DTBook (audio, images, the style sheet its ``xml-stylesheet``
  names). A file the manifest lists and nothing refers to fails
  ``opf_mnfDTBmembers``.
* **Skippable structures.** Every ``smilCustomTest`` of the NCX needs an entry in
  the resource file's NCX scope (``resource_skipStructResEntry``).

This is not ZedVal and does not replace running it. Each finding is reported
under ZedVal's test id, so it can be matched with a ZedVal report.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from app.daisy_export import first_frame_estimate

DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"
RES = "{http://www.daisy.org/z3986/2005/resource/}"
DC = "{http://purl.org/dc/elements/1.1/}"
NCX_NSURI = "http://www.daisy.org/z3986/2005/ncx/"

MULTIMEDIA_TYPES = {"audioOnly", "audioNCX", "audioPartText", "audioFullText", "textPartAudio", "textNCX"}
MULTIMEDIA_CONTENT = {"audio", "text", "image"}
# x-metadata names Z39.86-2005 defines (opf_xMetaDtbPrefixRestriction).
OPF_DTB_NAMES = {"sourceDate", "sourceEdition", "sourcePublisher", "sourceRights", "sourceTitle", "multimediaType",
                 "multimediaContent", "narrator", "producer", "producedDate", "revision", "revisionDate",
                 "revisionDescription", "totalTime", "audioFormat"}
OPF_SINGLE = ("sourceDate", "sourceEdition", "sourcePublisher", "sourceRights", "sourceTitle", "producedDate",
              "revision", "revisionDate", "revisionDescription")
AUDIO_FORMATS = {"MP4-AAC", "MP3", "WAV"}
SMIL_DTB_NAMES = {"uid", "generator", "totalElapsedTime"}
NCX_DTB_NAMES = {"uid", "generator", "depth", "totalPageCount", "maxPageNumber"}
# The bookStruct each skippable structure's smilCustomTest must carry (ncx_bookStructValue).
BOOK_STRUCTS = {"pagenum": "PAGE_NUMBER", "note": "NOTE", "noteref": "NOTE_REFERENCE",
                "annotation": "ANNOTATION", "linenum": "LINE_NUMBER", "sidebar": "OPTIONAL_SIDEBAR",
                "prodnote": "OPTIONAL_PRODUCER_NOTE"}
# DTBook elements whose SMIL target must be skippable (dtbook_customTestMinimally).
SKIPPABLE_ELEMENTS = {"linenum", "note", "noteref", "annotation", "pagenum"}
AUDIO_TYPES = {"audio/mpeg", "audio/mp4", "audio/x-wav"}

_ISO8601 = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$")
_RFC3066 = re.compile(r"^[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*$")
_ROMAN = re.compile(r"^[ivxlcdm]+$", re.IGNORECASE)
_FULL_CLOCK = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d+))?$")
_PARTIAL_CLOCK = re.compile(r"^([0-5]?\d):([0-5]\d)(?:\.(\d+))?$")
_TIMECOUNT = re.compile(r"^(\d+)(?:\.(\d+))?(h|min|s|ms)?$")
_STYLESHEET = re.compile(r"<\?xml-stylesheet\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"']")


def clock_ms(value: str | None) -> int | None:
    """A SMIL clock value in whole milliseconds, as ZedVal's SmilClock reads it; None if invalid."""
    if value is None:
        return None
    text = value.strip()
    if text.startswith("npt="):
        text = text[4:]

    def fraction(digits: str | None) -> int:
        return int(((digits or "") + "000")[:3])

    match = _FULL_CLOCK.match(text)
    if match:
        return (int(match.group(1)) * 3_600_000 + int(match.group(2)) * 60_000 + int(match.group(3)) * 1000
                + fraction(match.group(4)))
    match = _PARTIAL_CLOCK.match(text)
    if match:
        return int(match.group(1)) * 60_000 + int(match.group(2)) * 1000 + fraction(match.group(3))
    match = _TIMECOUNT.match(text)
    if match:
        whole, digits, unit = match.group(1), match.group(2) or "", match.group(3) or "s"
        scale = {"h": 3_600_000, "min": 60_000, "s": 1000, "ms": 1}[unit]
        return int(int(whole) * scale + (int(digits) * scale // 10 ** len(digits) if digits else 0))
    return None


def mp3_end_ms(path: Path) -> tuple[int | None, bool]:
    """Where ZedVal (javazoom) puts the end of an MP3 file, in whole ms, and whether it calls it VBR.

    The first frame after an ID3v2 tag decides (``first_frame_estimate``),
    which is wrong for a variable-bitrate file without a Xing header: ZedVal
    then reads its whole size at the first frame's bitrate.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(256 * 1024)
    except OSError:
        return None, False
    seconds, vbr = first_frame_estimate(head, size)
    # SmilClock(double): seconds times 1000, truncated.
    return (int(seconds * 1000.0) if seconds is not None else None), vbr


def _check(check_id: str, label: str, problems: list[dict], detail_ok: str, **evidence) -> dict:
    failed = [problem for problem in problems if problem.get("level", "requirement") == "requirement"]
    status = "failed" if failed else "warning" if problems else "passed"
    detail = detail_ok if not problems else (
        f"{len(failed)} incumplimientos de requisitos" if failed else f"{len(problems)} recomendaciones sin seguir")
    if problems:
        tests = sorted({problem["test"] for problem in problems})
        detail += ": " + ", ".join(tests[:6]) + ("…" if len(tests) > 6 else "")
    return {"id": check_id, "label": label, "status": status, "detail": detail, "problems": problems[:40], **evidence}


def _problem(test: str, detail: str, file: str = "", level: str = "requirement") -> dict:
    return {"test": test, "file": file, "detail": detail, "level": level}


class _Book:
    """The package as ZedVal sees it: OPF, manifest, spine, and every document parsed once."""

    def __init__(self, package: Path) -> None:
        self.root = package.resolve()
        self.opf_path = next(package.glob("*.opf")).resolve()
        self.opf_text = self.opf_path.read_text(encoding="utf-8")
        self.opf = ET.fromstring(self.opf_text)
        self.items = [(item.get("id") or "", item.get("href") or "", item.get("media-type") or "")
                      for item in self.opf.iter(f"{OPF}item")]
        self.by_id = {item_id: href for item_id, href, _media in self.items}
        self.media = {self.key(href): media for _id, href, media in self.items}
        self.spine = [self.by_id.get(ref.get("idref") or "", "") for ref in self.opf.iter(f"{OPF}itemref")]
        self.uid = next((element.text or "" for element in self.opf.iter(f"{DC}Identifier")
                         if element.get("id") == self.opf.get("unique-identifier")), "").strip()
        self._documents: dict[str, ET.Element | None] = {}

    def key(self, href: str, base: Path | None = None) -> str:
        """A reference as a path relative to the package root, the way the manifest names files."""
        path = PurePosixPath(href.partition("#")[0])
        start = (base or self.root)
        resolved = (start / Path(*path.parts)).resolve() if path.parts else start
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def document(self, key: str) -> ET.Element | None:
        if key not in self._documents:
            try:
                self._documents[key] = ET.parse(self.root / key).getroot()
            except (ET.ParseError, OSError):
                self._documents[key] = None
        return self._documents[key]

    def files_of(self, media_types: set[str]) -> list[str]:
        return [self.key(href) for _id, href, media in self.items if media in media_types]


def _timing(book: _Book) -> dict:
    problems: list[dict] = []
    ends: dict[str, tuple[int | None, bool]] = {}

    def end_of(key: str) -> tuple[int | None, bool]:
        if key not in ends:
            ends[key] = mp3_end_ms(book.root / key) if key.lower().endswith(".mp3") else (None, False)
        return ends[key]

    def clip(test_prefix: str, owner: str, audio: ET.Element, base: Path) -> int | None:
        begin, end = clock_ms(audio.get("clipBegin")), clock_ms(audio.get("clipEnd"))
        if begin is None or end is None:
            problems.append(_problem(f"{test_prefix}_clipBeginValue", f"clipBegin/clipEnd no válidos: "
                                     f"{audio.get('clipBegin')} – {audio.get('clipEnd')}", owner))
            return None
        if end <= begin:
            problems.append(_problem(f"{test_prefix}_clipEndAfterclipBegin",
                                     f"clipEnd {audio.get('clipEnd')} no es posterior a clipBegin", owner))
        key = book.key(audio.get("src") or "", base)
        eof, _vbr = end_of(key)
        if eof is not None and end > eof:
            problems.append(_problem(f"{test_prefix}_clipEndBeforeEOF",
                                     f"clipEnd {audio.get('clipEnd')} pasa del final de {key} ({eof} ms según "
                                     "su primera trama)", owner))
        return end - begin

    elapsed = 0
    for href in book.spine:
        key = book.key(href)
        root = book.document(key)
        if root is None:
            continue
        own = 0
        for audio in root.iter(f"{SMIL}audio"):
            length = clip("smil", key, audio, (book.root / key).parent)
            own += length or 0
        declared = next((meta.get("content") for meta in root.iter(f"{SMIL}meta")
                         if meta.get("name") == "dtb:totalElapsedTime"), None)
        value = clock_ms(declared)
        if declared is None:
            problems.append(_problem("smil_metaDtbTotElaPresence", "falta dtb:totalElapsedTime", key))
        elif value is None:
            problems.append(_problem("smil_metaDtbTotElaSyntax", f"dtb:totalElapsedTime no válido: {declared}", key))
        elif value != elapsed:
            problems.append(_problem("smil_metaAudioDtbTotElaAccurate",
                                     f"dtb:totalElapsedTime {declared}; los SMIL anteriores suman {elapsed} ms", key))
        elapsed += own
    totals = [meta.get("content") for meta in book.opf.iter(f"{OPF}meta") if meta.get("name") == "dtb:totalTime"]
    if len(totals) == 1 and clock_ms(totals[0]) is not None and clock_ms(totals[0]) != elapsed:
        problems.append(_problem("opf_xMetaTotTimeAccurate",
                                 f"dtb:totalTime {totals[0]}; los SMIL del spine suman {elapsed} ms", book.opf_path.name))
    ncx_key = book.key(book.by_id.get("ncx", ""))
    ncx = book.document(ncx_key) if book.by_id.get("ncx") else None
    if ncx is not None:
        for audio in ncx.iter(f"{NCX}audio"):
            clip("ncx", ncx_key, audio, (book.root / ncx_key).parent)
    for key, (_end, vbr) in sorted(ends.items()):
        if vbr:
            problems.append(_problem("audio_mp3vbr", "MP3 de velocidad variable (VBR)", key, "recommendation"))
    return _check("zedval_timing", "Tiempos y fragmentos (reglas de ZedVal)", problems,
                  f"{len(book.spine)} archivos SMIL, {elapsed} ms: cada tiempo declarado coincide al milisegundo "
                  "y ningún fragmento pasa del final de su audio", total_ms=elapsed, audio_files=len(ends))


def _metadata(book: _Book) -> dict:
    problems: list[dict] = []
    opf = book.opf_path.name
    dc: dict[str, list[ET.Element]] = {}
    for element in book.opf.iter():
        if isinstance(element.tag, str) and element.tag.startswith(DC):
            dc.setdefault(element.tag[len(DC):], []).append(element)

    def values(name: str) -> list[str]:
        return [(element.text or "").strip() for element in dc.get(name, [])]

    for name, present, filled in (("Title", "opf_DcTitlePresent", "opf_DcTitleValue"),
                                  ("Identifier", "opf_DcIdentifierPresent", "opf_DcIdentifierValue"),
                                  ("Publisher", "opf_DcPubPresent", "opf_DcPubValue")):
        if not values(name):
            problems.append(_problem(present, f"falta dc:{name}", opf))
        elif not all(values(name)):
            problems.append(_problem(filled, f"dc:{name} vacío", opf))
    if not values("Date"):
        problems.append(_problem("opf_DcDatePresent", "falta dc:Date", opf))
    elif not all(_ISO8601.match(value) for value in values("Date")):
        problems.append(_problem("opf_DcDateValue", f"dc:Date no es ISO 8601: {values('Date')}", opf))
    if not values("Format"):
        problems.append(_problem("opf_DcFormatPresent", "falta dc:Format", opf))
    elif any(value != "ANSI/NISO Z39.86-2005" for value in values("Format")):
        problems.append(_problem("opf_DcFormatValue", f"dc:Format {values('Format')}", opf))
    if not values("Language"):
        problems.append(_problem("opf_DcLangPresent", "falta dc:Language", opf))
    elif not all(_RFC3066.match(value) for value in values("Language")):
        problems.append(_problem("opf_DcLangValue", f"dc:Language no es RFC 3066: {values('Language')}", opf))
    if not any(element.get("id") == book.opf.get("unique-identifier") for element in dc.get("Identifier", [])):
        problems.append(_problem("opf_DcIdUniqIdref", "ningún dc:Identifier tiene el id de unique-identifier", opf))
    if 'xmlns:dc="http://purl.org/dc/elements/1.1/"' not in book.opf_text \
            or 'xmlns:oebpackage="http://openebook.org/namespaces/oeb-package/1.0/"' not in book.opf_text:
        problems.append(_problem("opf_dc-metadata-xmlns", "dc-metadata sin xmlns:dc y xmlns:oebpackage", opf))
    meta: dict[str, list[str]] = {}
    for element in book.opf.iter(f"{OPF}meta"):
        meta.setdefault(element.get("name") or "", []).append((element.get("content") or "").strip())
    for name in meta:
        if name.startswith("dtb:") and name[4:] not in OPF_DTB_NAMES:
            problems.append(_problem("opf_xMetaDtbPrefixRestriction", f"{name} no es un nombre dtb: de Z39.86", opf))
    kinds = meta.get("dtb:multimediaType", [])
    if len(kinds) != 1:
        problems.append(_problem("opf_xMetaMmTypePresence", f"dtb:multimediaType aparece {len(kinds)} veces", opf))
    elif kinds[0] not in MULTIMEDIA_TYPES:
        problems.append(_problem("opf_xMetaMmTypeValue", f"dtb:multimediaType «{kinds[0]}»", opf))
    contents = meta.get("dtb:multimediaContent", [])
    if len(contents) != 1:
        problems.append(_problem("opf_xMetaMmContentPresence", f"dtb:multimediaContent aparece {len(contents)} veces",
                                 opf))
    elif not set(part.strip() for part in contents[0].split(",")) <= MULTIMEDIA_CONTENT:
        problems.append(_problem("opf_xMetaMmContentValue", f"dtb:multimediaContent «{contents[0]}»", opf))
    else:
        parts = {part.strip() for part in contents[0].split(",")}
        if "audio" in parts and not book.files_of(AUDIO_TYPES):
            problems.append(_problem("opf_xMetaMmContentValueManifestAudio", "sin audio en el manifiesto", opf))
        if "text" in parts and not book.files_of({"application/x-dtbook+xml"}):
            problems.append(_problem("opf_xMetaMmContentValueManifestText", "sin DTBook en el manifiesto", opf))
    totals = meta.get("dtb:totalTime", [])
    if len(totals) != 1:
        problems.append(_problem("opf_xMetaTotTimePresence", f"dtb:totalTime aparece {len(totals)} veces", opf))
    elif clock_ms(totals[0]) is None:
        problems.append(_problem("opf_xMetaTotTimeValue", f"dtb:totalTime «{totals[0]}»", opf))
    for name in OPF_SINGLE:
        found = meta.get(f"dtb:{name}", [])
        if len(found) > 1:
            problems.append(_problem(f"opf_xMetaDtb{name[0].upper()}{name[1:]}Presence",
                                     f"dtb:{name} aparece {len(found)} veces", opf))
    for name in ("sourceDate", "producedDate", "revisionDate"):
        for value in meta.get(f"dtb:{name}", []):
            if not _ISO8601.match(value):
                problems.append(_problem(f"opf_xMetaDtb{name[0].upper()}{name[1:]}Value",
                                         f"dtb:{name} no es ISO 8601: {value}", opf))
    for value in meta.get("dtb:revision", []):
        if not value.isdigit():
            problems.append(_problem("opf_xMetaDtbRevisionValue", f"dtb:revision «{value}»", opf))
    for value in meta.get("dtb:audioFormat", []):
        if value not in AUDIO_FORMATS:
            problems.append(_problem("opf_xMetaDtbAudioFormatValue", f"dtb:audioFormat «{value}»", opf))
    return _check("zedval_metadata", "Metadatos del paquete (reglas de ZedVal)", problems,
                  "dc:Title, dc:Identifier, dc:Publisher, dc:Date, dc:Format, dc:Language y los metadatos dtb: "
                  "obligatorios están presentes y son válidos")


def _references(book: _Book) -> tuple[set[str], list[dict]]:
    """Every file the members of the book refer to, and what is wrong with those references."""
    problems: list[dict] = []
    referred: set[str] = set()
    dtbook_keys = set(book.files_of({"application/x-dtbook+xml"}))
    for key in book.files_of({"application/smil"}):
        root = book.document(key)
        if root is None:
            problems.append(_problem("smil_isDtdValid", "no se puede leer", key))
            continue
        base = (book.root / key).parent
        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
            if name in {"text", "audio", "img"} and element.get("src"):
                target = book.key(element.get("src"), base)
                referred.add(target)
                if not (book.root / target).is_file():
                    problems.append(_problem("smil_textSrcResolves" if name == "text" else f"smil_{name}SrcValue",
                                             f"{element.get('src')} no existe", key))
                elif name == "text":
                    if target not in dtbook_keys:
                        problems.append(_problem("smil_srcDtbook", f"{element.get('src')} no es el DTBook", key))
                    fragment = (element.get("src") or "").partition("#")[2]
                    document = book.document(target)
                    if fragment and document is not None and not any(
                            node.get("id") == fragment for node in document.iter()):
                        problems.append(_problem("smil_textSrcResolves", f"{element.get('src')}: falta el elemento",
                                                 key))
                elif name == "audio" and book.media.get(target) not in AUDIO_TYPES:
                    problems.append(_problem("smil_srcAudio", f"{element.get('src')} no es un audio del manifiesto", key))
    ncx_key = book.key(book.by_id.get("ncx", "")) if book.by_id.get("ncx") else ""
    ncx = book.document(ncx_key) if ncx_key else None
    if ncx is not None:
        base = (book.root / ncx_key).parent
        for element in ncx.iter():
            name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
            if name in {"content", "audio", "img"} and element.get("src"):
                referred.add(book.key(element.get("src"), base))
    resource_key = book.key(book.by_id.get("resource", "")) if book.by_id.get("resource") else ""
    resources = book.document(resource_key) if resource_key else None
    if resources is not None:
        base = (book.root / resource_key).parent
        for element in resources.iter():
            if element.get("src"):
                referred.add(book.key(element.get("src"), base))
    for key in dtbook_keys:
        try:
            text = (book.root / key).read_text(encoding="utf-8")
        except OSError:
            continue
        prolog = text[:text.find("<dtbook")] if "<dtbook" in text else text[:2000]
        base = (book.root / key).parent
        for href in _STYLESHEET.findall(prolog):
            referred.add(book.key(href, base))
        root = book.document(key)
        for element in root.iter() if root is not None else []:
            name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
            if name == "img" and element.get("src"):
                referred.add(book.key(element.get("src"), base))
            if name == "a" and element.get("href") and element.get("external") != "true" \
                    and not element.get("href", "").startswith(("#", "http:", "https:", "mailto:")):
                referred.add(book.key(element.get("href"), base))
    return referred, problems


def _manifest(book: _Book) -> dict:
    problems: list[dict] = []
    opf = book.opf_path.name
    hrefs = [href for _id, href, _media in book.items]
    keys = [book.key(href) for href in hrefs]
    if len(set(keys)) != len(keys):
        problems.append(_problem("opf_mnfHrefUriUni", "dos elementos del manifiesto nombran el mismo archivo", opf))
    for item_id, href, media in book.items:
        if "#" in href:
            problems.append(_problem("opf_mnfHrefUriFrag", f"{href} lleva un fragmento", opf))
        if not (book.root / book.key(href)).is_file():
            problems.append(_problem("opf_mnfIntegrityExists", f"{href} no existe", opf))
    ncx = [(item_id, href) for item_id, href, media in book.items if media == "application/x-dtbncx+xml"]
    if len(ncx) != 1:
        problems.append(_problem("opf_mnfNcxCount", f"{len(ncx)} NCX en el manifiesto", opf))
    elif ncx[0][0] != "ncx":
        problems.append(_problem("opf_mnfNcxId", f"el NCX tiene id «{ncx[0][0]}», no «ncx»", opf))
    resources = [(item_id, href) for item_id, href, media in book.items if media == "application/x-dtbresource+xml"]
    if len(resources) > 1:
        problems.append(_problem("opf_mnfResCount", f"{len(resources)} archivos de recursos", opf))
    elif resources and resources[0][0] != "resource":
        problems.append(_problem("opf_mnfResId", f"el archivo de recursos tiene id «{resources[0][0]}»", opf))
    ncx_root = book.document(book.key(ncx[0][1])) if len(ncx) == 1 else None
    if ncx_root is not None and list(ncx_root.iter(f"{NCX}smilCustomTest")) and len(resources) != 1:
        problems.append(_problem("opf_mnfResCountWhenSkippable",
                                 "hay estructuras saltables y no hay un archivo de recursos", opf))
    package_items = [href for _id, href, media in book.items if book.key(href) == book.key(book.opf_path.name)]
    if len(package_items) != 1:
        problems.append(_problem("opf_opfInManifest", f"el OPF aparece {len(package_items)} veces en el manifiesto",
                                 opf))
    smil_keys = book.files_of({"application/smil"})
    spine_keys = [book.key(href) for href in book.spine]
    for key in smil_keys:
        if key not in spine_keys:
            problems.append(_problem("opf_SpineRefAllSmilInMnf", f"{key} no está en el spine", opf))
    for key in spine_keys:
        if key not in smil_keys:
            problems.append(_problem("opf_spnItemRefIdrefSmil", f"el spine apunta a {key}, que no es SMIL", opf))
    if not smil_keys:
        problems.append(_problem("opf_mnfOneSmil", "ningún SMIL en el manifiesto", opf))
    if not book.files_of({"application/x-dtbook+xml"}):
        problems.append(_problem("opf_mnfOneDtbook", "ningún DTBook en el manifiesto", opf))
    referred, reference_problems = _references(book)
    problems.extend(reference_problems)
    # SMIL, NCX and resource files belong to the book by themselves; anything
    # else is a member only when one of them, or the DTBook, refers to it.
    members = set(referred) | set(smil_keys) | {book.key(href) for _id, href, media in book.items
                                                if media in {"application/x-dtbncx+xml",
                                                             "application/x-dtbresource+xml"}}
    listed = set(keys)
    for key in sorted(referred - listed):
        if not key.startswith(("http:", "https:")):
            problems.append(_problem("opf_mnfDTBmembers", f"{key} forma parte del libro y no está en el manifiesto",
                                     opf))
    for key in sorted(listed - members - {book.key(book.opf_path.name)}):
        problems.append(_problem("opf_mnfDTBmembers",
                                 f"{key} está en el manifiesto y nada del libro lo usa", opf))
    return _check("zedval_manifest", "Manifiesto, spine y miembros del libro (reglas de ZedVal)", problems,
                  f"{len(book.items)} archivos en el manifiesto: todos existen, todos forman parte del libro y "
                  "todo lo que el libro usa está en el manifiesto")


def _custom_tests(book: _Book) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """customTest elements of each SMIL file, and the NCX's smilCustomTests, by id."""
    by_smil: dict[str, list[dict]] = {}
    for key in book.files_of({"application/smil"}):
        root = book.document(key)
        if root is not None:
            by_smil[key] = [dict(test.attrib) for test in root.iter(f"{SMIL}customTest")]
    ncx_key = book.key(book.by_id.get("ncx", "")) if book.by_id.get("ncx") else ""
    ncx = book.document(ncx_key) if ncx_key else None
    ncx_tests = {test.get("id") or "": dict(test.attrib) for test in ncx.iter(f"{NCX}smilCustomTest")} \
        if ncx is not None else {}
    return by_smil, ncx_tests


def _navigation(book: _Book) -> dict:
    problems: list[dict] = []
    ncx_key = book.key(book.by_id.get("ncx", "")) if book.by_id.get("ncx") else ""
    ncx = book.document(ncx_key) if ncx_key else None
    if ncx is None:
        return _check("zedval_navigation", "Navegación NCX (reglas de ZedVal)",
                      [_problem("ncx_isDtdValid", "falta el NCX o no se puede leer", ncx_key)], "")
    meta: dict[str, list[str]] = {}
    for element in ncx.find(f"{NCX}head") if ncx.find(f"{NCX}head") is not None else []:
        if element.tag == f"{NCX}meta":
            meta.setdefault(element.get("name") or "", []).append((element.get("content") or "").strip())
    for name in meta:
        if name.startswith("dtb:") and name[4:] not in NCX_DTB_NAMES:
            problems.append(_problem("ncx_metaDtbPrefixRestriction", f"{name} no es un nombre dtb: del NCX", ncx_key))
    if meta.get("dtb:uid", [""])[0] != book.uid:
        problems.append(_problem("ncx_UidOpfUid", "dtb:uid distinto del identificador del paquete", ncx_key))
    if "dtb:generator" not in meta:
        problems.append(_problem("ncx_metaDtbGeneratorPresence", "falta dtb:generator", ncx_key, "recommendation"))

    def depth(element: ET.Element, level: int = 0) -> int:
        children = element.findall(f"{NCX}navPoint")
        return max([depth(child, level + 1) for child in children], default=level)

    nav_map = ncx.find(f"{NCX}navMap")
    actual_depth = depth(nav_map) if nav_map is not None else 0
    declared_depth = meta.get("dtb:depth", [""])[0]
    if not declared_depth:
        problems.append(_problem("ncx_metaDtbDepthPresence", "falta dtb:depth", ncx_key))
    elif not declared_depth.isdigit() or int(declared_depth) < 1:
        problems.append(_problem("ncx_metaDtbDepthValue", f"dtb:depth «{declared_depth}»", ncx_key))
    elif int(declared_depth) != actual_depth:
        problems.append(_problem("ncx_metaDtbDepthValueCorrelated",
                                 f"dtb:depth {declared_depth}; el navMap tiene {actual_depth} niveles", ncx_key))
    pages = list(ncx.iter(f"{NCX}pageTarget"))
    count = meta.get("dtb:totalPageCount", [""])[0]
    if not count:
        problems.append(_problem("ncx_metaDtbtotPageCountPresence", "falta dtb:totalPageCount", ncx_key))
    elif not count.isdigit():
        problems.append(_problem("ncx_metaDtbtotPageCountValue", f"dtb:totalPageCount «{count}»", ncx_key))
    elif int(count) != len(pages):
        problems.append(_problem("ncx_metaDtbtotPageCountValueCorrelated",
                                 f"dtb:totalPageCount {count}; el NCX tiene {len(pages)} páginas", ncx_key))
    values = []
    pairs: list[tuple[str, str]] = []
    for page in pages:
        value = page.get("value")
        if value is None:
            problems.append(_problem("ncx_allPageTargetsHaveValueAttribute",
                                     f"la página «{page.findtext(f'{NCX}navLabel/{NCX}text')}» no tiene value",
                                     ncx_key, "recommendation"))
            continue
        if not value.isdigit() or int(value) < 1:
            problems.append(_problem("ncx_ValueAttrValue", f"value «{value}» no es un entero positivo", ncx_key))
            continue
        values.append(int(value))
        pairs.append((value, page.get("type") or ""))
    if len(set(pairs)) != len(pairs):
        repeated = sorted({pair for pair in pairs if pairs.count(pair) > 1})
        problems.append(_problem("ncx_pageTargUniqValTypeComb", f"páginas repetidas: {repeated[:5]}", ncx_key))
    highest = meta.get("dtb:maxPageNumber", [""])[0]
    if not highest:
        problems.append(_problem("ncx_metaDtbmaxPageNumPresence", "falta dtb:maxPageNumber", ncx_key))
    elif not highest.isdigit():
        problems.append(_problem("ncx_metaDtbmaxPageNumValue", f"dtb:maxPageNumber «{highest}»", ncx_key))
    elif int(highest) != max(values, default=0):
        problems.append(_problem("ncx_metaDtbmaxPageNumValueCorrelated",
                                 f"dtb:maxPageNumber {highest}; la página más alta es {max(values, default=0)}",
                                 ncx_key))
    # playOrder: positive, from 1, without gaps, one value per SMIL container.
    base = (book.root / ncx_key).parent
    targets: list[tuple[int, str]] = []
    for element in ncx.iter():
        if element.tag not in {f"{NCX}navPoint", f"{NCX}navTarget", f"{NCX}pageTarget"}:
            continue
        order = element.get("playOrder") or ""
        content = element.find(f"{NCX}content")
        source = content.get("src") if content is not None else ""
        if not order.isdigit() or int(order) < 1:
            problems.append(_problem("ncx_playOrderPosInt", f"playOrder «{order}» en {element.get('id')}", ncx_key))
            continue
        target_file, _hash, fragment = (source or "").partition("#")
        key = book.key(target_file, base)
        if not fragment:
            problems.append(_problem("ncx_contentSrcHasFragment", f"{source} sin fragmento", ncx_key))
        elif book.media.get(key) != "application/smil":
            problems.append(_problem("ncx_srcSMIL", f"{source} no apunta a un SMIL", ncx_key))
        else:
            smil = book.document(key)
            if smil is None or not any(node.get("id") == fragment for node in smil.iter()):
                problems.append(_problem("ncx_contentSrcFragmentResolves", f"{source} no existe", ncx_key))
        targets.append((int(order), f"{key}#{fragment}"))
    orders = sorted({order for order, _target in targets})
    if orders and orders[0] != 1:
        problems.append(_problem("ncx_playOrderOrigin", f"el primer playOrder es {orders[0]}", ncx_key))
    if orders and orders != list(range(orders[0], orders[0] + len(orders))):
        problems.append(_problem("ncx_playOrderNoGaps", "la secuencia de playOrder tiene huecos", ncx_key))
    by_target: dict[str, set[int]] = {}
    by_order: dict[int, set[str]] = {}
    for order, target in targets:
        by_target.setdefault(target, set()).add(order)
        by_order.setdefault(order, set()).add(target)
    if any(len(found) > 1 for found in by_target.values()):
        problems.append(_problem("ncx_playOrderMatch", "un mismo destino con dos playOrder", ncx_key))
    if any(len(found) > 1 for found in by_order.values()):
        problems.append(_problem("ncx_playOrderMatch2", "un mismo playOrder con dos destinos", ncx_key))
    # Skippable structures: the NCX and the SMIL files declare the same tests.
    by_smil, ncx_tests = _custom_tests(book)
    for test_id, attributes in ncx_tests.items():
        expected = BOOK_STRUCTS.get(test_id)
        if expected and not attributes.get("bookStruct"):
            problems.append(_problem("ncx_bookStructReq", f"smilCustomTest «{test_id}» sin bookStruct", ncx_key))
        elif expected and attributes.get("bookStruct") != expected:
            problems.append(_problem("ncx_bookStructValue",
                                     f"smilCustomTest «{test_id}» con bookStruct {attributes.get('bookStruct')}",
                                     ncx_key))
        matching = [test for tests in by_smil.values() for test in tests
                    if test.get("id") == test_id and test.get("override") == attributes.get("override")
                    and test.get("defaultState") == attributes.get("defaultState")]
        if not matching:
            problems.append(_problem("ncx_smilCustomTestMatch", f"«{test_id}» no está así en ningún SMIL", ncx_key))
    states: dict[str, set[str]] = {}
    for key, tests in by_smil.items():
        for test in tests:
            states.setdefault(test.get("id") or "", set()).add(test.get("defaultState") or "")
            declared = ncx_tests.get(test.get("id") or "")
            if declared is None or declared.get("override") != test.get("override") \
                    or declared.get("defaultState") != test.get("defaultState"):
                problems.append(_problem("smil_ncxCustomTestMatch",
                                         f"customTest «{test.get('id')}» no está igual en el NCX", key))
            if test.get("override") != "visible":
                problems.append(_problem("smil_overrideVis", f"customTest «{test.get('id')}» override "
                                         f"{test.get('override')}", key, "recommendation"))
    for test_id, found in states.items():
        if len(found) > 1:
            problems.append(_problem("ncx_defaultStateSame", f"«{test_id}» cambia de defaultState entre SMIL", ncx_key))
    return _check("zedval_navigation", "Navegación NCX: páginas, playOrder y estructuras saltables (reglas de ZedVal)",
                  problems, f"{len(targets)} destinos de navegación y {len(pages)} páginas: recuentos, valores, "
                            "playOrder y customTest coherentes", pages=len(pages))


def _documents(book: _Book) -> dict:
    """DTBook, SMIL and resource-file rules that tie the documents together."""
    problems: list[dict] = []
    by_smil, ncx_tests = _custom_tests(book)
    smil_keys = book.files_of({"application/smil"})
    for key in smil_keys:
        root = book.document(key)
        if root is None:
            continue
        names = {meta.get("name") or "": (meta.get("content") or "") for meta in root.iter(f"{SMIL}meta")}
        for name in names:
            if name.startswith("dtb:") and name[4:] not in SMIL_DTB_NAMES:
                problems.append(_problem("smil_metaDtbPrefixRestriction", f"{name} no es un nombre dtb: de SMIL", key))
        if "dtb:uid" not in names:
            problems.append(_problem("smil_metaDtbUidPresence", "falta dtb:uid", key))
        elif names["dtb:uid"] != book.uid:
            problems.append(_problem("smil_metaDtbUidValue", "dtb:uid distinto del identificador del paquete", key))
        declared = {test.get("id") for test in by_smil.get(key, [])}
        for element in root.iter():
            if element.get("customTest") and element.get("customTest") not in declared:
                problems.append(_problem("smil_customTestAttrResolvesToCustomTestElement",
                                         f"customTest «{element.get('customTest')}» no está en el head", key))
            if element.tag == f"{SMIL}par":
                kinds = [child.tag.rsplit("}", 1)[-1] for child in element]
                if any(kinds.count(kind) > 1 for kind in ("text", "audio", "img", "seq")):
                    problems.append(_problem("smil_parContent", f"par {element.get('id')} con medios repetidos", key))
    for key in book.files_of({"application/x-dtbook+xml"}):
        root = book.document(key)
        if root is None:
            problems.append(_problem("dtbook_isDtdValid", "no se puede leer", key))
            continue
        head = {meta.get("name"): meta.get("content") for meta in root.iter(f"{DTBOOK}meta")}
        if "dtb:uid" not in head:
            problems.append(_problem("dtbook_MetaUid", "falta dtb:uid", key))
        elif head["dtb:uid"] != book.uid:
            problems.append(_problem("dtbook_UidOpfUid", "dtb:uid distinto del identificador del paquete", key))
        if "dc:Title" not in head:
            problems.append(_problem("dtbook_MetaTitle", "falta dc:Title", key))
        ids = {element.get("id") for element in root.iter() if element.get("id")}
        base = (book.root / key).parent
        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
            if name == "pagenum":
                label = (element.text or "").strip()
                kind = element.get("page") or "normal"
                if kind == "normal" and not (label.isdigit() and int(label) > 0):
                    problems.append(_problem("dtbook_pageNumNormal", f"página normal «{label}»", key))
                if kind == "front" and not _ROMAN.match(label):
                    problems.append(_problem("dtbook_pageNumFront", f"página front «{label}»", key))
            if name in {"noteref", "annoref"}:
                target = (element.get("idref") or "").lstrip("#")
                if not target or target not in ids:
                    problems.append(_problem("dtbook_idrefNote" if name == "noteref" else "dtbook_idrefAnnotation",
                                             f"{name} a «{element.get('idref')}», que no existe", key))
            reference = element.get("smilref")
            if not reference:
                continue
            target_file, _hash, fragment = reference.partition("#")
            smil_key = book.key(target_file, base)
            smil = book.document(smil_key) if smil_key in smil_keys else None
            node = next((item for item in smil.iter() if item.get("id") == fragment), None) if smil is not None else None
            if node is None:
                problems.append(_problem("dtbook_SmilRefResolves", f"smilref {reference} no existe", key))
                continue
            if node.tag.rsplit("}", 1)[-1] not in {"par", "seq"}:
                problems.append(_problem("dtbook_SmilRefResolvesToTimeContainers",
                                         f"smilref {reference} no es un par ni un seq", key))
            skippable = name in SKIPPABLE_ELEMENTS or (name in {"sidebar", "prodnote"}
                                                       and element.get("render") == "optional")
            if skippable and not node.get("customTest"):
                problems.append(_problem("dtbook_customTestMinimally",
                                         f"{name} {element.get('id')}: su SMIL no es saltable (sin customTest)", key))
    resource_key = book.key(book.by_id.get("resource", "")) if book.by_id.get("resource") else ""
    resources = book.document(resource_key) if resource_key else None
    if resources is not None:
        selects = [node.get("select") or "" for scope in resources.iter(f"{RES}scope")
                   if scope.get("nsuri") == NCX_NSURI for node in scope.iter(f"{RES}nodeSet")]
        for test_id, attributes in ncx_tests.items():
            book_struct = attributes.get("bookStruct") or ""
            if not any((select.startswith("//smilCustomTest[@bookStruct=") and book_struct and book_struct in select)
                       or (select.startswith("//smilCustomTest[@id=") and test_id in select) for select in selects):
                problems.append(_problem("resource_skipStructResEntry",
                                         f"sin entrada para la estructura saltable «{test_id}» ({book_struct})",
                                         resource_key))
        nsuris = [scope.get("nsuri") for scope in resources.iter(f"{RES}scope")]
        if len(set(nsuris)) != len(nsuris):
            problems.append(_problem("resource_resNsUriAtt", "dos scope con el mismo nsuri", resource_key))
        for node in resources.iter(f"{RES}nodeSet"):
            languages = [resource.get("{http://www.w3.org/XML/1998/namespace}lang") for resource in node]
            if len(set(languages)) != len(languages):
                problems.append(_problem("resource_nodeSetLangUnique",
                                         f"nodeSet {node.get('id')} repite un idioma", resource_key))
    elif ncx_tests:
        problems.append(_problem("resource_skipStructResEntry", "hay estructuras saltables y no hay archivo de "
                                                               "recursos", book.opf_path.name))
    return _check("zedval_documents", "DTBook, SMIL y recursos (reglas de ZedVal)", problems,
                  "identificadores, smilref, páginas, notas y estructuras saltables coherentes entre documentos")


def verify(package: Path) -> list[dict]:
    """ZedVal's requirement rules on a built package, as checks for the completeness chain."""
    try:
        book = _Book(package)
    except (StopIteration, ET.ParseError, OSError) as exc:
        return [{"id": "zedval_package", "label": "Paquete legible (reglas de ZedVal)", "status": "failed",
                 "detail": f"No se pudo leer el paquete: {exc}", "problems": []}]
    return [_timing(book), _metadata(book), _manifest(book), _navigation(book), _documents(book)]


def summary(checks: list[dict]) -> dict:
    failed = [problem for check in checks for problem in check.get("problems", [])
              if problem.get("level", "requirement") == "requirement"]
    return {"status": "failed" if any(check["status"] == "failed" for check in checks) else "passed",
            "requirements_failed": sorted({problem["test"] for problem in failed}),
            "checks": {check["id"]: check["status"] for check in checks}}
