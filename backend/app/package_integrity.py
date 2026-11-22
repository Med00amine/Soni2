"""Read a delivered ZIP back and prove that the package inside it holds together.

A ZIP that downloads is not yet an audiobook. This opens the archive as a
reader would, independently of the code that wrote it, and checks:

* **zip_readable**: the archive opens, every entry's CRC matches, no entry
  name escapes the archive and none appears twice.
* **zip_layout**: one DAISY package in ``DAISY 3/``: its OPF, and the NCX,
  DTBook and resource file the OPF names; the complete audiobook, when there
  is one, beside that folder (it is not a file of the DAISY book).
* **zip_xml**: every XML document of the package is well formed.
* **zip_manifest**: every file the OPF lists is in the archive, and every file
  of the package is listed: nothing obsolete or stray travels with the book.
* **zip_references**: every SMIL ``text`` and ``audio`` reference, every DTBook
  ``smilref`` and every NCX ``content`` resolves to a file in the archive and,
  where it names one, to an element of it.
* **zip_audio**: every audio file of the package is MPEG audio and is played
  by some SMIL clip: no orphaned audio. The complete audiobook beside the
  package is MPEG audio too.
* **zip_order**: the spine, the navigation and the chapter files follow one
  reading order.
* **zip_metadata**: title and identifier agree across OPF, DTBook, NCX and SMIL.
* **zip_chapters** (given ``expected_titles``): the navigation lists exactly
  the book's sections, in order: a deleted chapter cannot linger and an added
  one cannot be missing.

Each check is ``passed`` or ``failed`` with its evidence. ``readable`` is False
only when the archive itself cannot be read: such a file must never be
delivered.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = "DAISY 3/"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
XML_SUFFIXES = {".xml", ".opf", ".ncx", ".res", ".smil"}
_CHAPTER_AUDIO = re.compile(r"chapter-(\d+)\.mp3$")


def _check(check_id: str, label: str, problems: list, detail_ok: str, **evidence) -> dict:
    # The same missing file named by every clip that plays it is one problem.
    seen: set[str] = set()
    problems = [item for item in problems
                if not (str(item.get("problem", item)) in seen or seen.add(str(item.get("problem", item))))]
    status = "failed" if problems else "passed"
    detail = detail_ok if not problems else "; ".join(str(item.get("problem", item)) for item in problems[:3]) + (
        f" (y {len(problems) - 3} más)" if len(problems) > 3 else "")
    return {"id": check_id, "label": label, "status": status, "detail": detail, "problems": problems[:30],
            **evidence}


def _resolve(base: str, reference: str) -> tuple[str, str]:
    """``(package path, fragment)`` of a reference made from the file ``base``."""
    target, _, fragment = reference.partition("#")
    if not target:
        return base, fragment
    folder = posixpath.dirname(base)
    return posixpath.normpath(posixpath.join(folder, target)) if folder else posixpath.normpath(target), fragment


def _is_mpeg(head: bytes) -> bool:
    offset = 0
    if head[:3] == b"ID3" and len(head) >= 10:
        offset = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | head[9] & 0x7F)
    for position in range(offset, max(offset, len(head) - 4)):
        if head[position] == 0xFF and head[position + 1] & 0xE0 == 0xE0 and (head[position + 2] >> 4) not in (0, 15):
            return True
    return False


def audit_zip(path: Path, *, expected_titles: list[str] | None = None) -> dict:
    """Every check above on the ZIP at ``path``."""
    checks: list[dict] = []
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        problem = {"problem": f"no se puede abrir el ZIP: {exc}"}
        return {"status": "failed", "readable": False, "files": 0, "detail": problem["problem"],
                "checks": [_check("zip_readable", "ZIP legible", [problem], "")], "package": {}}
    with archive:
        names = archive.namelist()
        problems: list[dict] = []
        try:
            broken = archive.testzip()
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            broken = f"({exc})"
        if broken:
            problems.append({"problem": f"la entrada {broken} está dañada (CRC)"})
        for name in names:
            parts = PurePosixPath(name).parts
            if name.startswith("/") or "\\" in name or ".." in parts or re.match(r"^[A-Za-z]:", name):
                problems.append({"problem": f"la entrada «{name}» sale de la carpeta del ZIP"})
        repeated = [name for name, count in Counter(names).items() if count > 1]
        problems += [{"problem": f"la entrada «{name}» aparece dos veces"} for name in repeated]
        checks.append(_check("zip_readable", "ZIP legible", problems,
                             f"{len(names)} entradas, todas legibles y con su CRC correcto"))
        if problems:
            return _result(checks, readable=False, files=len(names), package={})

        files = {name[len(ROOT):]: name for name in names if name.startswith(ROOT) and not name.endswith("/")}
        opfs = [name for name in files if name.endswith(".opf") and "/" not in name]
        if len(opfs) != 1:
            checks.append(_check("zip_layout", "Estructura del paquete",
                                 [{"problem": f"{len(opfs)} archivos OPF en «{ROOT}»; se espera uno"}], ""))
            return _result(checks, readable=True, files=len(names), package={})

        parsed: dict[str, ET.Element] = {}
        xml_problems = []
        for name in sorted(files):
            if PurePosixPath(name).suffix.lower() in XML_SUFFIXES:
                try:
                    parsed[name] = ET.fromstring(archive.read(files[name]))
                except ET.ParseError as exc:
                    xml_problems.append({"file": name, "problem": f"{name}: XML mal formado ({exc})"})
        opf_name = opfs[0]
        opf = parsed.get(opf_name)
        manifest: dict[str, tuple[str, str]] = {}
        if opf is not None:
            for item in opf.iter(f"{OPF}item"):
                href = posixpath.normpath(item.get("href") or "")
                manifest[item.get("id") or href] = (href, item.get("media-type") or "")
        by_type = {media: href for href, media in manifest.values()}
        layout_problems = []
        if opf is None:
            layout_problems.append({"problem": "el OPF no se puede leer"})
        for media, label in (("application/x-dtbncx+xml", "NCX"), ("application/x-dtbook+xml", "DTBook"),
                             ("application/x-dtbresource+xml", "recursos")):
            if media not in by_type:
                layout_problems.append({"problem": f"el OPF no declara el archivo {label}"})
        # The complete audiobook travels beside the package: nothing in a DAISY
        # book plays it, so it cannot be one of its files (ZedVal opf_mnfDTBmembers).
        book_audio = sorted(name for name in names if "/" not in name and name.lower().endswith(".mp3"))
        checks.append(_check("zip_layout", "Estructura del paquete", layout_problems,
                             f"«{ROOT}» con su OPF, NCX, DTBook y recursos"
                             + (f"; el audiolibro completo al lado ({book_audio[0]})" if book_audio else "")))
        checks.append(_check("zip_xml", "XML del paquete", xml_problems,
                             f"{len(parsed)} documentos XML bien formados"))

        listed = {href for href, _media in manifest.values()}
        manifest_problems = [{"problem": f"el OPF lista «{href}», que no está en el ZIP"}
                             for href in sorted(listed - set(files))]
        manifest_problems += [{"problem": f"«{name}» está en el paquete pero el OPF no lo lista (archivo sobrante)"}
                              for name in sorted(set(files) - listed)]
        checks.append(_check("zip_manifest", "Manifiesto y archivos", manifest_problems,
                             f"los {len(listed)} archivos del manifiesto están en el ZIP, y ninguno más"))

        dtbook_name = by_type.get("application/x-dtbook+xml", "")
        ncx_name = by_type.get("application/x-dtbncx+xml", "")
        dtbook = parsed.get(dtbook_name)
        ncx = parsed.get(ncx_name)
        ids: dict[str, set[str]] = {name: {element.get("id") for element in root.iter() if element.get("id")}
                                    for name, root in parsed.items()}
        spine = [manifest.get(ref.get("idref") or "", ("", ""))[0] for ref in opf.iter(f"{OPF}itemref")] \
            if opf is not None else []
        reference_problems: list[dict] = []
        referenced_audio: set[str] = set()
        first_audio: list[str] = []
        smil_count = 0
        for smil_name in spine:
            root = parsed.get(smil_name)
            if root is None:
                reference_problems.append({"problem": f"el spine nombra «{smil_name}», que no se puede leer"})
                continue
            smil_count += 1
            audio_here = None
            for par in root.iter(f"{SMIL}par"):
                for text in par.findall(f"{SMIL}text"):
                    target, fragment = _resolve(smil_name, text.get("src") or "")
                    if target not in files:
                        reference_problems.append({"problem": f"{smil_name}: el texto «{target}» no existe"})
                    elif fragment and fragment not in ids.get(target, set()):
                        reference_problems.append({"problem": f"{smil_name}: «{target}» no tiene #{fragment}"})
                for audio in par.findall(f"{SMIL}audio"):
                    target, _fragment = _resolve(smil_name, audio.get("src") or "")
                    if target not in files:
                        reference_problems.append({"problem": f"{smil_name}: el audio «{target}» no existe"})
                    referenced_audio.add(target)
                    audio_here = audio_here or target
            if audio_here:
                first_audio.append(audio_here)
        if dtbook is not None:
            for element in dtbook.iter():
                reference = element.get("smilref")
                if not reference:
                    continue
                target, fragment = _resolve(dtbook_name, reference)
                if target not in files or (fragment and fragment not in ids.get(target, set())):
                    reference_problems.append({"problem": f"DTBook #{element.get('id')}: «{reference}» no existe"})
        nav_labels: list[str] = []
        nav_smil: list[str] = []
        if ncx is not None:
            for point in ncx.iter():
                if point.tag not in {f"{NCX}navPoint", f"{NCX}pageTarget", f"{NCX}navTarget"}:
                    continue
                content = point.find(f"{NCX}content")
                reference = content.get("src") if content is not None else ""
                target, fragment = _resolve(ncx_name, reference or "")
                if target not in files or (fragment and fragment not in ids.get(target, set())):
                    reference_problems.append({"problem": f"NCX: «{reference}» no existe"})
                if point.tag == f"{NCX}navPoint":
                    nav_smil.append(target)
                    if point.get("class") != "frontmatter":
                        nav_labels.append((point.findtext(f"{NCX}navLabel/{NCX}text") or "").strip())
        checks.append(_check("zip_references", "Referencias internas", reference_problems,
                             f"{smil_count} archivos SMIL: cada texto, audio, smilref y entrada del NCX lleva a "
                             "algo que existe"))

        audio_files = sorted(name for name in files if name.lower().endswith(".mp3"))
        audio_problems = []
        for name in audio_files:
            with archive.open(files[name]) as stream:
                head = stream.read(65536)
            if not head or not _is_mpeg(head):
                audio_problems.append({"problem": f"«{name}» no es audio MPEG"})
            if name not in referenced_audio:
                audio_problems.append({"problem": f"«{name}» no lo reproduce ningún SMIL (audio huérfano u obsoleto)"})
        for name in book_audio:
            with archive.open(name) as stream:
                if not _is_mpeg(stream.read(65536)):
                    audio_problems.append({"problem": f"el audiolibro completo «{name}» no es audio MPEG"})
        checks.append(_check("zip_audio", "Audio del paquete", audio_problems,
                             f"{len(audio_files)} archivos MP3: cada uno se reproduce desde la navegación"
                             + ("; el audiolibro completo es audio MPEG" if book_audio else "")))

        order_problems = []
        positions = {name: index for index, name in enumerate(spine)}
        reached = [positions[name] for name in nav_smil if name in positions]
        if reached != sorted(reached):
            order_problems.append({"problem": "la navegación no sigue el orden del spine"})
        numbers = [int(match.group(1)) for match in (_CHAPTER_AUDIO.search(name) for name in first_audio) if match]
        if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
            order_problems.append({"problem": f"los archivos de capítulo no siguen el orden de lectura: {numbers[:12]}"})
        checks.append(_check("zip_order", "Orden de lectura", order_problems,
                             "spine, navegación y archivos de capítulo en el mismo orden"))

        meta_problems = []
        title_opf = (opf.findtext(f".//{DC}Title") or "").strip() if opf is not None else ""
        uid_opf = ""
        if opf is not None:
            uid_attr = opf.get("unique-identifier") or "uid"
            uid_opf = next(((element.text or "").strip() for element in opf.iter(f"{DC}Identifier")
                            if element.get("id") == uid_attr), "")
        heads = {}
        if dtbook is not None:
            heads["DTBook"] = {meta.get("name"): meta.get("content") for meta in dtbook.iter(f"{DTBOOK}meta")}
        if ncx is not None:
            heads["NCX"] = {meta.get("name"): meta.get("content") for meta in ncx.iter(f"{NCX}meta")}
            heads["NCX"]["docTitle"] = (ncx.findtext(f"{NCX}docTitle/{NCX}text") or "").strip()
        if heads.get("DTBook", {}).get("dc:Title", title_opf).strip() != title_opf:
            meta_problems.append({"problem": "el título del DTBook no coincide con el del OPF"})
        if heads.get("NCX", {}).get("docTitle", title_opf) != title_opf:
            meta_problems.append({"problem": "el título del NCX no coincide con el del OPF"})
        for label, values in heads.items():
            if values.get("dtb:uid") != uid_opf:
                meta_problems.append({"problem": f"el identificador del {label} no coincide con el del OPF"})
        for smil_name in spine:
            root = parsed.get(smil_name)
            if root is None:
                continue
            uid = next((meta.get("content") for meta in root.iter(f"{SMIL}meta") if meta.get("name") == "dtb:uid"), None)
            if uid != uid_opf:
                meta_problems.append({"problem": f"{smil_name}: dtb:uid distinto del OPF"})
        checks.append(_check("zip_metadata", "Metadatos coherentes", meta_problems,
                             f"«{title_opf}» con el mismo identificador en OPF, DTBook, NCX y SMIL"))

        if expected_titles is not None:
            expected = [title.strip() for title in expected_titles]
            chapter_problems = []
            if nav_labels != expected:
                missing = [title for title in expected if title not in nav_labels]
                extra = [title for title in nav_labels if title not in expected]
                chapter_problems += [{"problem": f"falta «{title}» en la navegación"} for title in missing]
                chapter_problems += [{"problem": f"«{title}» está en la navegación y no en el libro"} for title in extra]
                if not missing and not extra:
                    chapter_problems.append({"problem": "las secciones no están en el orden del libro"})
            checks.append(_check("zip_chapters", "Secciones del libro", chapter_problems,
                                 f"las {len(expected)} secciones del libro, en orden, y ninguna más"))

        total_time = ""
        if opf is not None:
            total_time = next((meta.get("content") or "" for meta in opf.iter(f"{OPF}meta")
                               if meta.get("name") == "dtb:totalTime"), "")
        package = {"title": title_opf, "sections": nav_labels, "audio_files": len(audio_files),
                   "chapter_files": sum(1 for name in audio_files if _CHAPTER_AUDIO.search(name)),
                   "smil_files": smil_count, "total_time": total_time,
                   "book_audio": book_audio[0] if book_audio else None,
                   "bytes": Path(path).stat().st_size}
        return _result(checks, readable=True, files=len(names), package=package)


def _result(checks: list[dict], *, readable: bool, files: int, package: dict) -> dict:
    failed = [check for check in checks if check["status"] == "failed"]
    detail = ("; ".join(f"{check['label']}: {check['detail']}" for check in failed[:3]) if failed
              else f"{files} entradas comprobadas: el paquete es coherente")
    return {"status": "failed" if failed else "passed", "readable": readable, "files": files, "detail": detail,
            "checks": checks, "package": package}
