"""Structural checks for links and files in generated DAISY 3 packages."""

from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

DTBOOK = "{http://www.daisy.org/z3986/2005/dtbook/}"
SMIL = "{http://www.w3.org/2001/SMIL20/}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://openebook.org/namespaces/oeb-package/1.0/}"


def validate_daisy_package(package: Path) -> None:
    """Raise ValueError if generated package files or cross-references are broken."""
    required = ("book.xml", "book.ncx", "book.opf", "book.res", "book.mp3")
    missing = [name for name in required if not (package / name).is_file()]
    if missing:
        raise ValueError(f"Faltan archivos requeridos del paquete DAISY: {', '.join(missing)}")
    for path in package.rglob("*.mp3"):
        if path.stat().st_size == 0:
            raise ValueError(f"El archivo de audio está vacío: {path.relative_to(package)}")

    dtbook = ET.parse(package / "book.xml").getroot()
    smil_ids: dict[str, set[str]] = {}
    for path in (package / "smil").glob("*.smil"):
        root = ET.parse(path).getroot()
        smil_ids[path.relative_to(package).as_posix()] = {
            element.get("id", "") for element in root.iter() if element.get("id")
        }
        for audio in root.findall(f".//{SMIL}audio"):
            _check_reference(package, path, audio.get("src", ""), must_exist=True)
        for text in root.findall(f".//{SMIL}text"):
            _check_reference(package, path, text.get("src", ""), must_exist=True)

    paragraphs = dtbook.findall(f".//{DTBOOK}p")
    if not paragraphs:
        raise ValueError("El documento DTBook no contiene párrafos narrables.")
    for paragraph in paragraphs:
        path, fragment = _split_ref(paragraph.get("smilref", ""))
        if path not in smil_ids or fragment not in smil_ids[path]:
            raise ValueError(f"Referencia smilref no resuelta: {paragraph.get('smilref', '')}")

    ncx = ET.parse(package / "book.ncx").getroot()
    points = ncx.findall(f".//{NCX}navMap/{NCX}navPoint")
    if not points:
        raise ValueError("La navegación NCX no contiene capítulos.")
    for target in ncx.findall(f".//{NCX}content"):
        path, fragment = _split_ref(target.get("src", ""))
        if not path or path not in smil_ids or fragment not in smil_ids[path]:
            raise ValueError(f"Destino de navegación NCX no resuelto: {target.get('src', '')}")

    opf = ET.parse(package / "book.opf").getroot()
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


def _check_reference(package: Path, parent: Path, reference: str, *, must_exist: bool) -> None:
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
    if fragment and target.name == "book.xml":
        root = ET.parse(target).getroot()
        if not any(element.get("id") == fragment for element in root.iter()):
            raise ValueError(f"Fragmento DTBook no encontrado: {reference}")
