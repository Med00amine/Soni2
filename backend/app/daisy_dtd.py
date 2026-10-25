"""Validate a DAISY 3 package against the official ANSI/NISO Z39.86-2005 DTDs.

The DTDs in ``schemas/z3986-2005`` are the published standard files, as
redistributed by the DAISY Consortium (see that folder's SOURCE.md). They are
read locally: nothing is fetched from the network, so validation works offline
and cannot be redirected.

This checks every XML file of the package against its document type. It is
stronger evidence than Soni2's own reference checks, but it is still not the
DAISY Pipeline validator, which also checks rules a DTD cannot express (for
example that dtb:totalTime matches the audio).
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

SCHEMAS = Path(__file__).resolve().parent / "schemas" / "z3986-2005"
DOCUMENT_TYPES = {
    ".xml": "dtbook-2005-3.dtd",
    ".ncx": "ncx-2005-1.dtd",
    ".res": "resource-2005-1.dtd",
    ".smil": "dtbsmil-2005-2.dtd",
    ".opf": "oebpkg12.dtd",
}
_dtd_cache: dict[str, etree.DTD] = {}


def _dtd(name: str) -> etree.DTD:
    if name not in _dtd_cache:
        _dtd_cache[name] = etree.DTD(str(SCHEMAS / name))
    return _dtd_cache[name]


def validate_package(package: Path) -> dict:
    """``{"status": "passed"|"failed", "files": [...], "errors": [...]}``."""
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
    files: list[dict] = []
    errors: list[str] = []
    for path in sorted(package.rglob("*")):
        dtd_name = DOCUMENT_TYPES.get(path.suffix.lower())
        if not dtd_name or not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        try:
            document = etree.parse(str(path), parser)
        except etree.XMLSyntaxError as exc:
            files.append({"file": relative, "dtd": dtd_name, "valid": False})
            errors.append(f"{relative}: XML mal formado: {exc}")
            continue
        dtd = _dtd(dtd_name)
        valid = dtd.validate(document)
        files.append({"file": relative, "dtd": dtd_name, "valid": bool(valid)})
        if not valid:
            for entry in list(dtd.error_log.filter_from_errors())[:10]:
                errors.append(f"{relative}:{entry.line}: {entry.message}")
    return {
        "status": "passed" if files and not errors else "failed",
        "method": "dtd_z3986_2005",
        "files": files,
        "errors": errors[:50],
    }
