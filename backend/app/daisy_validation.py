"""Structural checks for links and files in generated DAISY 3 packages."""

from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"


def validate_daisy_package(package: Path) -> None:
    """Raise ValueError if generated package files or cross-references are broken."""
    # The package files carry the book's name, so they are found through the
    # one OPF at the package root rather than by fixed filenames.
    opf_files = sorted(package.glob("*.opf"))
    if len(opf_files) != 1:
        raise ValueError("El paquete DAISY debe contener exactamente un archivo OPF en su raíz.")
    opf_path = opf_files[0]
    opf = ET.parse(opf_path).getroot()
    by_id = {item.get("id", ""): item.get("href", "") for item in opf.findall(f"{OPF}manifest/{OPF}item")}
    names = {key: by_id.get(key, "") for key in ("dtbook", "ncx", "resource", "full-audiobook")}
    missing = [key for key, href in names.items() if not href or not (package / href).is_file()]
    if missing:
        raise ValueError(f"Faltan archivos requeridos del paquete DAISY: {', '.join(missing)}")
    for path in package.rglob("*.mp3"):
        if path.stat().st_size == 0:
            raise ValueError(f"El archivo de audio está vacío: {path.relative_to(package)}")
    # Each chapter directory must hold exactly one final chapter audio file.
    chapter_dirs = sorted(path for path in (package / "audio").glob("chapter-*") if path.is_dir())
    for chapter_dir in chapter_dirs:
        expected = chapter_dir / f"{chapter_dir.name}.mp3"
        if not expected.is_file():
            raise ValueError(f"Falta el audio final del capítulo: {expected.relative_to(package)}")

    dtbook_name = names["dtbook"]
    dtbook = ET.parse(package / dtbook_name).getroot()
    smil_ids: dict[str, set[str]] = {}
    for path in (package / "smil").glob("*.smil"):
        root = ET.parse(path).getroot()
        smil_ids[path.relative_to(package).as_posix()] = {
            element.get("id", "") for element in root.iter() if element.get("id")
        }
        for audio in root.findall(f".//{SMIL}audio"):
            _check_reference(package, path, audio.get("src", ""), must_exist=True)
            _check_clip(audio)
        for text in root.findall(f".//{SMIL}text"):
            _check_reference(package, path, text.get("src", ""), must_exist=True, dtbook_name=dtbook_name)

    paragraphs = dtbook.findall(f".//{DTBOOK}p")
    if not paragraphs:
        raise ValueError("El documento DTBook no contiene párrafos narrables.")
    for paragraph in paragraphs:
        # Every paragraph with text is read, so it must be synchronized. An
        # empty one only completes the structure after a lone heading.
        if not paragraph.get("smilref") and not "".join(paragraph.itertext()).strip():
            continue
        path, fragment = _split_ref(paragraph.get("smilref", ""))
        if path not in smil_ids or fragment not in smil_ids[path]:
            raise ValueError(f"Referencia smilref no resuelta: {paragraph.get('smilref', '')}")
    for element in dtbook.iter():
        # Headings and page markers are synchronized too: their references must resolve.
        reference = element.get("smilref")
        if reference:
            path, fragment = _split_ref(reference)
            if path not in smil_ids or fragment not in smil_ids[path]:
                raise ValueError(f"Referencia smilref no resuelta: {reference}")

    ncx = ET.parse(package / names["ncx"]).getroot()
    points = ncx.findall(f".//{NCX}navMap/{NCX}navPoint")
    if not points:
        raise ValueError("La navegación NCX no contiene capítulos.")
    for target in ncx.findall(f".//{NCX}content"):
        path, fragment = _split_ref(target.get("src", ""))
        if not path or path not in smil_ids or fragment not in smil_ids[path]:
            raise ValueError(f"Destino de navegación NCX no resuelto: {target.get('src', '')}")

    items = opf.findall(f"{OPF}manifest/{OPF}item")
    ids = [item.get("id", "") for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("El manifiesto OPF contiene identificadores duplicados.")
    for item in items:
        href = item.get("href", "")
        _safe_file(package, href)
    for ref in opf.findall(f"{OPF}spine/{OPF}itemref"):
        if ref.get("idref") not in ids:
            raise ValueError(f"Referencia de spine OPF no resuelta: {ref.get('idref', '')}")


def _split_ref(reference: str) -> tuple[str, str]:
    path, separator, fragment = reference.partition("#")
    return path, fragment if separator else ""


def _safe_file(package: Path, href: str) -> Path:
    relative = PurePosixPath(href)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Ruta de paquete no válida: {href}")
    target = package.joinpath(*relative.parts)
    if not target.is_file():
        raise ValueError(f"Falta un archivo referenciado en el manifiesto: {href}")
    return target


def _seconds(clock: str) -> float:
    """Seconds in a SMIL full clock value such as 0:01:02.345."""
    hours, minutes, seconds = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _check_clip(audio: ET.Element) -> None:
    begin, end = audio.get("clipBegin"), audio.get("clipEnd")
    if begin is None and end is None:
        return
    try:
        if begin is None or end is None or _seconds(begin) >= _seconds(end):
            raise ValueError
    except ValueError:
        raise ValueError(f"Fragmento de audio SMIL no válido: {audio.get('src', '')} {begin}-{end}") from None


def _check_reference(package: Path, parent: Path, reference: str, *, must_exist: bool,
                     dtbook_name: str = "") -> None:
    path, fragment = _split_ref(reference)
    if not path:
        raise ValueError(f"Referencia vacía o no válida: {reference}")
    relative = PurePosixPath(path)
    if relative.is_absolute():
        raise ValueError(f"Ruta de referencia no válida: {reference}")
    target = (parent.parent / Path(*relative.parts)).resolve()
    if package.resolve() not in target.parents and target != package.resolve():
        raise ValueError(f"Referencia fuera del paquete: {reference}")
    if must_exist and not target.is_file():
        raise ValueError(f"Falta un archivo referenciado: {reference}")
    if fragment and dtbook_name and target.name == dtbook_name:
        root = ET.parse(target).getroot()
        if not any(element.get("id") == fragment for element in root.iter()):
            raise ValueError(f"Fragmento DTBook no encontrado: {reference}")
