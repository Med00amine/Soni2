"""Check a built package against the ONCE structure it was meant to have.

The editor shows a structure, but what ONCE receives is the package, so the
package itself is read back here, independently of the code that wrote it:

* **Initial files**: files 1, 2 and 3 come first, in the front matter, with the
  title line, the legal notice word for word, the bibliographic lines and the
  index exactly as planned.
* **Hierarchy**: initial files and chapters are level 1, subchapters level 2,
  in the DTBook and in the NCX alike.
* **Pages**: every page number is a silent, skippable marker (a SMIL ``par``
  with no audio and ``customTest="pagenum"``), listed once in the NCX page list
  in reading order; file 1 is on its page and ends on it.
* **Index**: each entry names a section that exists and gives the page that
  section really starts on in this package.
* **Notes**: each note opens with "Nota N." and closes with "Fin de nota N.",
  and is a skippable ``note`` structure.
* **Closing phrase**: exactly once, as the last thing read.
* **Navigation order**: ``playOrder`` follows the reading order, one number
  per SMIL time container (Z39.86-2005 §8.4.3).

Each check is ``passed``, ``failed`` or ``warning`` with its evidence, in the
same form as the completeness chain, so the release gate treats a failure
here as it treats any broken link of the chain.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app import planilla as planilla_module
from app.completeness import DTBOOK, NCX, OPF, SMIL, _check, _package_files, _seconds  # noqa: F401
from app.daisy_export import page_kind

INITIAL_CLASSES = ["once-title", "once-bibliographic", "once-index"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _normal(text: str) -> str:
    return planilla_module.normal_heading(text)


class _Package:
    """The parts of a built package the checks read."""

    def __init__(self, package: Path) -> None:
        self.root = package
        self.opf_path, self.opf, self.dtbook_path, self.dtbook = _package_files(package)
        ncx_href = next((item.get("href") for item in self.opf.iter(f"{OPF}item") if item.get("id") == "ncx"), "")
        self.ncx = ET.parse(package / ncx_href).getroot()
        by_id = {item.get("id"): item.get("href") for item in self.opf.iter(f"{OPF}item")}
        self.spine = [by_id.get(ref.get("idref"), "") for ref in self.opf.iter(f"{OPF}itemref")]
        self.smil: dict[str, ET.Element] = {}
        for href in self.spine:
            if href and (package / href).is_file():
                self.smil[href] = ET.parse(package / href).getroot()
        # Every SMIL time container, in reading order: spine order, then document order.
        self.position: dict[str, int] = {}
        for href in self.spine:
            root = self.smil.get(href)
            if root is None:
                continue
            for element in root.iter():
                if _local(element.tag) in {"par", "seq"} and element.get("id"):
                    self.position.setdefault(f"{href}#{element.get('id')}", len(self.position))
        self.sections = self._sections()

    def container(self, reference: str) -> ET.Element | None:
        href, _, fragment = reference.partition("#")
        root = self.smil.get(href)
        if root is None:
            return None
        return next((element for element in root.iter() if element.get("id") == fragment), None)

    def _sections(self) -> list[dict]:
        """Every level of the book in reading order, with what it holds."""
        result = []
        book = self.dtbook.find(f"{DTBOOK}book")
        if book is None:
            return result
        for matter in book:
            matter_name = _local(matter.tag)
            for level in matter:
                if _local(level.tag) == "level1":
                    self._collect(level, matter_name, 1, result)
        return result

    def _collect(self, level: ET.Element, matter: str, depth: int, result: list[dict]) -> None:
        heading = next((child for child in level if _local(child.tag) in {"h1", "h2"}), None)
        entry = {"element": level, "matter": matter, "level": depth, "class": level.get("class", ""),
                 "heading": _text(heading), "heading_synchronized": bool(heading is not None and heading.get("smilref")),
                 "paragraphs": [], "pages": [], "notes": []}
        result.append(entry)
        for child in level:
            name = _local(child.tag)
            if name == "p" and child.get("smilref"):
                entry["paragraphs"].append({"text": _text(child), "class": child.get("class", ""),
                                            "smilref": child.get("smilref")})
            elif name == "pagenum":
                entry["pages"].append(child)
            elif name == "note":
                paragraphs = [_text(item) for item in child if _local(item.tag) == "p"]
                entry["notes"].append({"element": child, "text": " ".join(paragraphs)})
                for item in child:
                    if _local(item.tag) == "p" and item.get("smilref"):
                        entry["paragraphs"].append({"text": _text(item), "class": "once-note",
                                                    "smilref": item.get("smilref")})
            elif name == "level2":
                self._collect(child, matter, 2, result)


def verify(package: Path, records: list[dict], plan: dict | None = None) -> list[dict]:
    """The ONCE checks of a built package, given the planned sections.

    ``records`` are the job's sections: ``{"title", "paragraphs",
    "title_generated", "once": {...}}``; ``plan`` is the resolved planilla.
    """
    try:
        built = _Package(package)
    except (ET.ParseError, StopIteration, KeyError, OSError) as exc:
        return [_check("once_readable", "Estructura ONCE: paquete legible", "failed", f"No se pudo leer: {exc}")]
    plan = plan or {}
    planned = [record for record in records if record.get("paragraphs") or not record.get("title_generated")]
    checks = [
        _initial_files(built, planned, plan),
        _hierarchy(built, planned),
        _pages(built, plan),
        _index(built, planned, plan),
        _notes(built, plan),
        _closing(built, planned),
        _play_order(built),
    ]
    return checks


def _initial_files(built: _Package, planned: list[dict], plan: dict) -> dict:
    problems: list[str] = []
    front = [section for section in built.sections if section["matter"] == "frontmatter"]
    classes = [section["class"] for section in front]
    if classes != INITIAL_CLASSES:
        problems.append(f"los archivos iniciales son {classes or 'ninguno'}, se esperaban {INITIAL_CLASSES}")
    for number, (section, record) in enumerate(zip(front, planned[:3]), start=1):
        if section["heading"] != " ".join((record.get("title") or "").split()):
            problems.append(f"el archivo {number} se titula «{section['heading']}» y debía ser «{record.get('title')}»")
        if not section["heading_synchronized"]:
            problems.append(f"el encabezado del archivo {number} no se narra")
        expected = [" ".join(text.split()) for text in record.get("paragraphs") or []]
        actual = [paragraph["text"] for paragraph in section["paragraphs"]]
        if number == 1:
            # The legal notice is narrated in the pipeline's paragraph units; joined
            # again they must give back the planilla's text exactly.
            if " ".join(actual) != " ".join(expected):
                problems.append("el texto legal del archivo 1 no coincide palabra por palabra con la planilla")
        elif actual != expected:
            problems.append(f"el contenido del archivo {number} no coincide con el previsto")
    if front and plan.get("file1_page"):
        pages = [page.text or "" for page in front[0]["pages"]]
        if pages != [plan["file1_page"]]:
            problems.append(f"el archivo 1 debe acabar en la página {plan['file1_page']} y marca {pages or 'ninguna'}")
    if len(front) > 1 and plan.get("file2_page"):
        pages = [page.text or "" for page in front[1]["pages"]]
        if pages != [plan["file2_page"]]:
            problems.append(f"los datos bibliográficos deben ir en la página {plan['file2_page']} y marcan "
                            f"{pages or 'ninguna'}")
    if len(front) > 1:
        lines = " ".join(paragraph["text"] for paragraph in front[1]["paragraphs"])
        for required in ("Servicio Bibliográfico de la ONCE", "Sonidos S-2 Productions, S.L."):
            if required not in lines:
                problems.append(f"falta «{required}» en los datos bibliográficos")
    return _check("once_initial_files", "Estructura ONCE: archivos iniciales 1, 2 y 3",
                  "failed" if problems else "passed",
                  "; ".join(problems) or "título y autor, texto legal, datos bibliográficos e índice, en orden",
                  problems=problems)


def _hierarchy(built: _Package, planned: list[dict]) -> dict:
    problems: list[str] = []
    if len(built.sections) != len(planned):
        problems.append(f"{len(built.sections)} secciones en el paquete y {len(planned)} previstas")
    for number, (section, record) in enumerate(zip(built.sections, planned), start=1):
        once = record.get("once") or {}
        expected_level = int(once.get("level") or 1)
        if section["level"] != expected_level:
            problems.append(f"«{section['heading']}» (archivo {number}) es nivel {section['level']} y debía ser "
                            f"nivel {expected_level}")
    # The NCX nests the same way.
    def walk(points, depth, levels):
        for point in points:
            levels.append(depth)
            walk([child for child in point if _local(child.tag) == "navPoint"], depth + 1, levels)
    nav_map = built.ncx.find(f"{NCX}navMap")
    nav_levels: list[int] = []
    if nav_map is not None:
        walk([child for child in nav_map if _local(child.tag) == "navPoint"], 1, nav_levels)
    if nav_levels != [section["level"] for section in built.sections]:
        problems.append("los niveles de la navegación NCX no coinciden con los del texto")
    depth = next((meta.get("content") for meta in built.ncx.iter(f"{NCX}meta")
                  if meta.get("name") == "dtb:depth"), "")
    if depth != str(max(nav_levels, default=1)):
        problems.append(f"dtb:depth es {depth} y la navegación tiene {max(nav_levels, default=1)} niveles")
    return _check("once_hierarchy", "Estructura ONCE: niveles (archivos iniciales y capítulos 1, subcapítulos 2)",
                  "failed" if problems else "passed",
                  "; ".join(problems) or f"{len(built.sections)} archivos, "
                  f"{sum(1 for section in built.sections if section['level'] == 2)} subcapítulos en el nivel 2",
                  problems=problems)


def _pages(built: _Package, plan: dict) -> dict:
    problems: list[str] = []
    pagenums = [page for section in built.sections for page in section["pages"]]
    for page in pagenums:
        container = built.container(page.get("smilref", ""))
        label = page.text or ""
        if container is None:
            problems.append(f"la página {label} no tiene su marca en el SMIL")
            continue
        if container.find(f"{SMIL}audio") is not None:
            problems.append(f"la marca de la página {label} tiene audio: debía ser silenciosa")
        if container.get("customTest") != "pagenum":
            problems.append(f"la marca de la página {label} no es omitible (customTest)")
        text = container.find(f"{SMIL}text")
        if text is None or text.get("src", "").partition("#")[2] != page.get("id"):
            problems.append(f"la marca de la página {label} apunta a otro texto")
    targets = list(built.ncx.iter(f"{NCX}pageTarget"))
    if len(targets) != len(pagenums):
        problems.append(f"{len(pagenums)} páginas en el texto y {len(targets)} en la lista de páginas del NCX")
    for page, target in zip(pagenums, targets):
        content = target.find(f"{NCX}content")
        label = target.find(f"{NCX}navLabel/{NCX}text")
        if content is None or content.get("src") != page.get("smilref"):
            problems.append(f"la página {page.text} del NCX no apunta a su marca")
        if label is None or (label.text or "") != (page.text or ""):
            problems.append(f"la página {page.text} se llama de otro modo en el NCX")
        kind, value = page_kind(page.text or "")
        if target.get("type") != kind or (value and target.get("value") != str(value)):
            problems.append(f"la página {page.text} tiene tipo o valor incorrecto en el NCX")
    metas = {meta.get("name"): meta.get("content") for meta in built.ncx.iter(f"{NCX}meta")}
    values = [value for value in (page_kind(page.text or "")[1] for page in pagenums) if value]
    if metas.get("dtb:totalPageCount") != str(len(targets)):
        problems.append("dtb:totalPageCount no coincide con la lista de páginas")
    if metas.get("dtb:maxPageNumber") != str(max(values, default=0)):
        problems.append("dtb:maxPageNumber no coincide con la página más alta")
    if pagenums and not any(test.get("id") == "pagenum" and test.get("bookStruct") == "PAGE_NUMBER"
                            for test in built.ncx.iter(f"{NCX}smilCustomTest")):
        problems.append("el NCX no declara las páginas como estructura omitible (smilCustomTest)")
    labels = [page.text or "" for page in pagenums]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    status = "failed" if problems else ("warning" if repeated else "passed")
    detail = "; ".join(problems) or (f"páginas repetidas: {', '.join(repeated)}" if repeated else
                                     f"{len(pagenums)} marcas de página silenciosas, en orden"
                                     if pagenums else "el libro no tiene marcas de página")
    return _check("once_pages", "Estructura ONCE: marcas de página silenciosas", status, detail,
                  problems=problems, pages=labels[:200])


def _index(built: _Package, planned: list[dict], plan: dict) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    if len(built.sections) < 3:
        return _check("once_index", "Estructura ONCE: índice coherente con el libro", "failed", "no hay índice")
    index = built.sections[2]
    entries = [paragraph["text"] for paragraph in index["paragraphs"]]
    end = plan.get("index_end") or "Fin de índice."
    if not entries or entries[-1] != end:
        problems.append(f"el índice no termina con «{end}»")
    # The page in effect when each section begins, in this package.
    starts: dict[int, str] = {}
    current = ""
    position = {id(section["element"]): number for number, section in enumerate(built.sections)}
    for element in built.dtbook.iter():
        name = _local(element.tag)
        if name == "pagenum":
            current = element.text or ""
        elif name in {"level1", "level2"} and id(element) in position:
            first = next((child for child in element if _local(child.tag) not in {"pagenum"}), None)
            own = [child.text or "" for child in element if _local(child.tag) == "pagenum"
                   and first is not None and list(element).index(child) < list(element).index(first)]
            starts[position[id(element)]] = own[0] if own else current
    depth = int(plan.get("index_depth") or 2)
    template = plan.get("index_entry") or "{heading}. Página {page}."
    expected: list[str] = []
    if plan.get("index_page"):
        expected.append(planilla_module.render(plan.get("index_self_entry") or "Índice página {page}.",
                                               page=plan["index_page"]))
    for number, section in enumerate(built.sections[3:], start=3):
        if section["level"] > depth or not section["heading_synchronized"]:
            continue
        heading = section["heading"].rstrip(".").strip()
        page = starts.get(number, "")
        if page:
            expected.append(planilla_module.render(template, heading=heading, page=page))
        else:
            expected.append(f"{heading}.")
            warnings.append(heading)
    expected.append(end)
    if entries != [" ".join(line.split()) for line in expected]:
        missing = [line for line in expected if line not in entries]
        extra = [line for line in entries if line not in expected]
        problems.append("el índice no coincide con las secciones y páginas del paquete"
                        + (f"; falta: {missing[:3]}" if missing else "") + (f"; sobra: {extra[:3]}" if extra else ""))
    if plan.get("index_page"):
        pages = [page.text or "" for page in index["pages"]]
        if pages != [plan["index_page"]]:
            problems.append(f"el índice debe ir en la página {plan['index_page']} y marca {pages or 'ninguna'}")
    status = "failed" if problems else ("warning" if warnings else "passed")
    detail = "; ".join(problems) or (f"{len(warnings)} entradas sin número de página" if warnings else
                                     f"{len(entries) - 1} entradas, cada una con la página en que empieza su sección")
    return _check("once_index", "Estructura ONCE: índice coherente con el libro", status, detail,
                  problems=problems, entries=entries[:200])


def _notes(built: _Package, plan: dict) -> dict:
    problems: list[str] = []
    opening = plan.get("note_open") or "Nota {n}."
    closing = plan.get("note_close") or "Fin de nota {n}."
    count = 0
    pattern = re.compile("^" + re.escape(opening).replace(re.escape("{n}"), r"(\d+)") + r"(?:\s|$)")
    for section in built.sections:
        for note in section["notes"]:
            count += 1
            text = note["text"]
            match = pattern.match(text)
            number = int(match.group(1)) if match else None
            if number is None:
                problems.append(f"una nota no empieza con «{opening.format(n='N')}»: «{text[:60]}»")
                continue
            if not text.endswith(planilla_module.render(closing, n=number)):
                problems.append(f"la nota {number} no termina con «{planilla_module.render(closing, n=number)}»")
            paragraphs = [item for item in note["element"] if _local(item.tag) == "p"]
            for paragraph in paragraphs:
                container = built.container(paragraph.get("smilref", ""))
                parent_seq = None
                if container is not None:
                    href = paragraph.get("smilref", "").partition("#")[0]
                    root = built.smil.get(href)
                    parents = {child: parent for parent in root.iter() for child in parent} if root is not None else {}
                    parent_seq = parents.get(container)
                if parent_seq is None or parent_seq.get("customTest") != "note":
                    problems.append(f"la nota {number} no es una estructura omitible (customTest)")
    if count and not any(test.get("id") == "note" and test.get("bookStruct") == "NOTE"
                         for test in built.ncx.iter(f"{NCX}smilCustomTest")):
        problems.append("el NCX no declara las notas (smilCustomTest)")
    return _check("once_notes", "Estructura ONCE: notas «Nota N.» … «Fin de nota N.»",
                  "failed" if problems else "passed",
                  "; ".join(problems) or (f"{count} notas con su principio y su fin" if count else
                                          "el libro no tiene notas"),
                  problems=problems)


def _closing(built: _Package, planned: list[dict]) -> dict:
    problems: list[str] = []
    expected = ""
    for record in planned:
        details = (record.get("once") or {}).get("paragraphs") or []
        for detail, text in zip(details, record.get("paragraphs") or []):
            if detail.get("role") == "closing":
                expected = " ".join(text.split())
    if not expected:
        problems.append("no está prevista la frase final")
    marked = [paragraph for section in built.sections for paragraph in section["paragraphs"]
              if paragraph["class"] == "once-closing"]
    if len(marked) != 1:
        problems.append(f"la frase final aparece marcada {len(marked)} veces")
    elif marked[0]["text"] != expected:
        problems.append(f"la frase final dice «{marked[0]['text']}» y debía decir «{expected}»")
    every = [paragraph for section in built.sections for paragraph in section["paragraphs"]]
    if expected and sum(1 for paragraph in every if _normal(paragraph["text"]) == _normal(expected)) != 1:
        problems.append("la frase final se lee más de una vez")
    if every and every[-1]["class"] != "once-closing":
        problems.append("la frase final no es lo último que se lee")
    return _check("once_closing", "Estructura ONCE: frase final, una sola vez y al final",
                  "failed" if problems else "passed", "; ".join(problems) or f"«{expected}»", problems=problems)


def _play_order(built: _Package) -> dict:
    problems: list[str] = []
    targets = []
    for element in built.ncx.iter():
        if _local(element.tag) in {"navPoint", "pageTarget", "navTarget"}:
            content = element.find(f"{NCX}content")
            reference = content.get("src", "") if content is not None else ""
            targets.append((reference, element.get("playOrder", "")))
    by_reference: dict[str, set[str]] = {}
    for reference, order in targets:
        by_reference.setdefault(reference, set()).add(order)
    inconsistent = [reference for reference, orders in by_reference.items() if len(orders) > 1]
    if inconsistent:
        problems.append(f"{len(inconsistent)} destinos con más de un playOrder")
    unknown = [reference for reference in by_reference if reference not in built.position]
    if unknown:
        problems.append(f"{len(unknown)} destinos que no son contenedores SMIL del libro")
    ordered = sorted((reference for reference in by_reference if reference in built.position),
                     key=lambda reference: built.position[reference])
    numbers = []
    for reference in ordered:
        try:
            numbers.append(int(next(iter(by_reference[reference]))))
        except ValueError:
            problems.append(f"playOrder no numérico en {reference}")
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append("playOrder no sigue el orden de lectura de uno en uno")
    return _check("once_play_order", "Navegación: playOrder en orden de lectura",
                  "failed" if problems else "passed",
                  "; ".join(problems) or f"{len(ordered)} destinos numerados en orden de lectura",
                  problems=problems)
