"""The production sheet ("planilla") ONCE prepares for every audiobook.

``MODELO ONCE.docx``, the model the supervisor supplied in October 2026, shows
what a planilla contains and the structure every ONCE audiobook starts with:

* the **ABSYS number** that identifies the audiobook (the model has none);
* **audio file 1**: the title, then after a comma the author ("…, De …"), then
  ONCE's legal notice, word for word. File 1 always ends on page 1;
* **audio file 2**: the bibliographic data (title, author, edition and
  publication details, publisher, and the production line), on the page the
  planilla gives (page 2 in the model);
* **audio file 3**: the index, "Índice.", one entry per heading with its page,
  and "Fin de índice.";
* the chapters, one audio file each, subchapters at level 2;
* at the end of the last chapter, "Aquí finaliza la grabación de …, De …".

Notes are read "Nota 1." … "Fin de nota 1." (the supervisor's instruction).

Everything that changes from book to book is a field here, so a new book is
produced from its own planilla without touching the code. The model's own
values (its invented title, author, publisher and dates) are never used as
defaults: what a book does not supply is left out or reported as missing.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import re
import string
from dataclasses import asdict, dataclass, field, fields

# The legal notice exactly as MODELO ONCE.docx gives it (paragraph 8), including
# the closing quotation mark the model ends it with. It is not paraphrased or
# corrected; a planilla can replace it with its own text.
ONCE_LEGAL_TEXT = (
    "Esta adaptación es propiedad de la Organización Nacional de Ciegos Españoles (ONCE) y ha sido realizada en "
    "sistema Daisy exclusivamente para su reproducción por personas afiliadas a esta Organización; para un uso "
    "privado, sin finalidad comercial, conforme a lo dispuesto en la Ley española de Propiedad Intelectual. En "
    "consecuencia, estas no podrán distribuirla, comunicarla públicamente ni transformarla en todo o en parte, y "
    "tampoco utilizarla con fines distintos de los contemplados en la ley mencionada y en este aviso. También "
    "queda rigurosamente prohibida la utilización, reproducción, distribución, transformación, comunicación "
    "pública o explotación de esta adaptación por otras personas distintas. Todo esto, sin perjuicio de la "
    "facultad de la ONCE para intercambiar ejemplares de obras en formato accesible con entidades autorizadas, "
    "al amparo de la normativa y los tratados internacionales aplicables en cada momento. Servicio Bibliográfico "
    "ONCE 2026”"
)
# The production line of file 2, as in the model, before its date.
PRODUCTION_LINE = ("Obra grabada en formato digital para el Servicio Bibliográfico de la ONCE, "
                   "Sonidos S-2 Productions, S.L.")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")

# What each text may refer to. {title_and_author} is "Título, De Autor", or
# the title alone for a book without an author.
_PLACEHOLDERS = {
    "title_line": {"title", "author", "title_and_author"},
    "closing": {"title", "author", "title_and_author"},
    "index_self_entry": {"page"},
    "index_entry": {"heading", "page"},
    "note_open": {"n"},
    "note_close": {"n"},
}
_PAGE_LABEL = re.compile(r"^[0-9]{1,5}$|^[ivxlcdm]{1,8}$", re.I)
_TEXT_LIMITS = {"legal_text": 6000, "closing": 600}


@dataclass
class Planilla:
    absys: str = ""
    # None: taken from the book's own metadata.
    title: str | None = None
    author: str | None = None
    title_line: str = "{title_and_author}"
    legal_text: str = ONCE_LEGAL_TEXT
    bibliographic_heading: str = "DATOS BIBLIOGRÁFICOS"
    # None: composed from the book's metadata; "" leaves the line out.
    edition_line: str | None = None
    publisher_line: str | None = None
    # Only a planilla that asks for it carries an ISBN (the model has none).
    isbn: str = ""
    production_line: str = PRODUCTION_LINE
    # None: the month the book is produced, "octubre de 2026".
    production_date: str | None = None
    file1_page: str = "1"
    # The model puts file 2 on page 2 and the index on page 3 "in this case":
    # each book's planilla says where; unset means no page marker.
    file2_page: str = ""
    index_page: str = ""
    index_heading: str = "Índice."
    index_self_entry: str = "Índice página {page}."
    index_entry: str = "{heading}. Página {page}."
    index_end: str = "Fin de índice."
    # 1: chapters only (as the model's index); 2: chapters and subchapters.
    index_depth: int = 2
    closing: str = "Aquí finaliza la grabación de {title_and_author}."
    note_open: str = "Nota {n}."
    note_close: str = "Fin de nota {n}."
    # Heading -> page, from a planilla's own index. Used only where the book
    # itself carries no page numbers.
    chapter_pages: dict[str, str] = field(default_factory=dict)
    # The files a planilla lists after the index, to compare with the book.
    expected_files: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def defaults() -> dict:
    return Planilla().as_dict()


def from_json(raw: str | None) -> Planilla | None:
    """Parse the ``planilla_json`` a request carries; None means no ONCE structure."""
    if raw is None or not str(raw).strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("La planilla no tiene un formato válido.") from exc
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("La planilla debe ser un objeto con sus datos.")
    if payload.get("enabled") is False:
        return None
    return from_dict(payload)


def from_dict(payload: dict) -> Planilla:
    known = {item.name: item for item in fields(Planilla)}
    values: dict = {}
    for key, value in payload.items():
        if key not in known or key == "enabled":
            continue
        if key in {"title", "author", "edition_line", "publisher_line", "production_date"}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"El dato «{key}» de la planilla debe ser texto.")
            values[key] = value.strip() if isinstance(value, str) else None
        elif key == "index_depth":
            if value not in (1, 2):
                raise ValueError("La profundidad del índice debe ser 1 (capítulos) o 2 (capítulos y subcapítulos).")
            values[key] = value
        elif key == "chapter_pages":
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, (str, int))
                                                  for k, v in value.items()) or len(value) > 2000:
                raise ValueError("Las páginas de los capítulos de la planilla no son válidas.")
            values[key] = {k.strip(): str(v).strip() for k, v in value.items() if k.strip() and str(v).strip()}
        elif key == "expected_files":
            if not isinstance(value, list) or len(value) > 2000 or any(
                    not isinstance(item, dict) or not isinstance(item.get("heading", ""), str) for item in value):
                raise ValueError("La lista de archivos de la planilla no es válida.")
            values[key] = [{"number": int(item.get("number") or 0), "heading": item.get("heading", "").strip()}
                           for item in value]
        else:
            if not isinstance(value, str):
                raise ValueError(f"El dato «{key}» de la planilla debe ser texto.")
            values[key] = value.strip()
    planilla = Planilla(**values)
    validate(planilla)
    return planilla


def validate(planilla: Planilla) -> None:
    for name in ("file1_page", "file2_page", "index_page"):
        value = getattr(planilla, name)
        if value and not _PAGE_LABEL.match(value):
            raise ValueError(f"La página «{value}» de la planilla no es un número de página válido.")
    for name, allowed in _PLACEHOLDERS.items():
        template = getattr(planilla, name)
        used = {field_name for _text, field_name, _spec, _conv in string.Formatter().parse(template) if field_name}
        if used - allowed:
            raise ValueError(f"El texto «{template}» de la planilla usa datos que no existen: "
                             f"{', '.join(sorted(used - allowed))}.")
    if not planilla.legal_text.strip():
        raise ValueError("El texto legal de la ONCE no puede quedar vacío.")
    for name, limit in _TEXT_LIMITS.items():
        if len(getattr(planilla, name)) > limit:
            raise ValueError(f"El texto «{name}» de la planilla supera los {limit} caracteres.")
    for item in fields(Planilla):
        value = getattr(planilla, item.name)
        if isinstance(value, str) and len(value) > 6000:
            raise ValueError(f"El dato «{item.name}» de la planilla es demasiado largo.")


def title_and_author(title: str, author: str) -> str:
    """ "El faro de la isla Almendra, De Antonio Pérez": the model's form."""
    title, author = title.strip(), author.strip()
    return f"{title}, De {author}" if author else title


def render(template: str, **values: str | int) -> str:
    return template.format(**values).strip()


def production_month(today: _dt.date | None = None) -> str:
    """ "octubre de 2026": the form of the date in the model's production line."""
    day = today or _dt.date.today()
    return f"{MONTHS[day.month - 1]} de {day.year}"


@dataclass
class Resolved:
    """The planilla with every value decided for one book."""

    planilla: Planilla
    title: str
    author: str
    edition_line: str
    publisher_line: str
    production_date: str
    missing: list[str]

    def as_dict(self) -> dict:
        return {**self.planilla.as_dict(), "title": self.title, "author": self.author,
                "edition_line": self.edition_line, "publisher_line": self.publisher_line,
                "production_date": self.production_date, "missing": self.missing}


def resolve(planilla: Planilla, metadata: dict, *, today: _dt.date | None = None) -> Resolved:
    """Fill what the planilla leaves to the book, and say what is still missing."""
    title = planilla.title if planilla.title else (metadata.get("title") or "").strip()
    author = planilla.author if planilla.author is not None else (metadata.get("author") or "").strip()
    if planilla.edition_line is None:
        edition_line = ", ".join(part for part in ((metadata.get("edition") or "").strip(),
                                                   (metadata.get("publication_year") or "").strip()) if part)
    else:
        edition_line = planilla.edition_line
    publisher_line = (planilla.publisher_line if planilla.publisher_line is not None
                      else (metadata.get("publisher") or "").strip())
    production_date = planilla.production_date or production_month(today)
    missing = []
    if not title:
        missing.append("título")
    if not author:
        missing.append("autor")
    if not edition_line:
        missing.append("edición y datos de publicación")
    if not publisher_line:
        missing.append("editorial")
    if not planilla.file2_page:
        missing.append("página de los datos bibliográficos")
    if not planilla.index_page:
        missing.append("página del índice")
    return Resolved(planilla, title, author, edition_line, publisher_line, production_date, missing)


# --------------------------------------------------------------------------- #
# Reading a planilla written like MODELO ONCE.docx
# --------------------------------------------------------------------------- #
#
# The model writes the content in black and its instructions in red ("(SIEMPRE
# SE PONE EL TÍTULO)", "ARCHIVO 2 DE AUDIO.", "EN ESTE CASO PONERLA EN LA
# PÁGINA 2"). That convention is what is read here. Nothing is guessed: what
# cannot be identified is reported, and the result is shown to a person to
# confirm before anything is narrated.

_FILE_MARK = re.compile(r"ARCHIVO\s+(\d+)\s+DE\s+AUDIO", re.I)
_PAGE_RULE = re.compile(r"P[ÁA]GINA\s+([0-9]+|[IVXLCDM]+)\b", re.I)
_INDEX_SELF = re.compile(r"^[ÍI]ndice\.?\s+p[áa]gina\s+([0-9ivxlcdm]+)\.?$", re.I)
_INDEX_ENTRY = re.compile(r"^(.*?)\.?\s+P[áa]gina\s+([0-9ivxlcdm]+)\.?$", re.I)
_INDEX_END = re.compile(r"^fin\s+de(?:l)?\s+[íi]ndice\.?$", re.I)
_DATE_TAIL = re.compile(rf"^(.*?)\s*\b((?:{'|'.join(MONTHS)})\s+de\s+\d{{4}})\.?$", re.I)


def _is_instruction(run) -> bool:
    color = run.font.color
    try:
        rgb = color.rgb if color is not None and color.type is not None else None
    except (AttributeError, ValueError):
        rgb = None
    if rgb is None:
        return False
    red, green, blue = int(str(rgb)[0:2], 16), int(str(rgb)[2:4], 16), int(str(rgb)[4:6], 16)
    return red >= 0x99 and green <= 0x66 and blue <= 0x66


def _clean_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_docx(content: bytes) -> tuple[dict, list[str]]:
    """The planilla a document like MODELO ONCE.docx describes, and what could not be read."""
    from docx import Document

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:  # not a Word document
        raise ValueError("No se ha podido abrir la planilla: comprueba que sea un documento de Word (.docx).") from exc
    rows: list[tuple[str, str]] = []
    for paragraph in document.paragraphs:
        content_parts, instruction_parts = [], []
        for run in paragraph.runs:
            (instruction_parts if _is_instruction(run) else content_parts).append(run.text)
        rows.append((_clean_line("".join(content_parts)), _clean_line("".join(instruction_parts))))

    result: dict = {}
    warnings: list[str] = []
    blocks: dict[int, list[tuple[str, str]]] = {}
    current: int | None = None
    closing_next = False
    for text, instruction in rows:
        mark = _FILE_MARK.search(instruction) or _FILE_MARK.search(text)
        if mark:
            current = int(mark.group(1))
            blocks.setdefault(current, [])
            continue
        if text.lower().startswith("número absys") or text.lower().startswith("numero absys"):
            result["absys"] = re.sub(r"^n[úu]mero absys\s*[.:]?\s*", "", text, flags=re.I).strip()
            continue
        if re.search(r"DEBE\s+DECIR", instruction, re.I):
            closing_next = True
            if current is not None:
                blocks[current].append(("", instruction))
            continue
        if closing_next and text:
            result["_closing_literal"] = text
            closing_next = False
            continue
        if current is not None:
            blocks[current].append((text, instruction))

    def lines(number: int) -> list[str]:
        return [text for text, _instruction in blocks.get(number, []) if text]

    def page_rule(number: int) -> str:
        for _text, instruction in blocks.get(number, []):
            match = _PAGE_RULE.search(instruction)
            if match:
                return match.group(1)
        return ""

    # File 1: "Título," / "De Autor" / the legal text.
    first = lines(1)
    if len(first) >= 3:
        result["title"] = first[0].rstrip(", ").strip()
        result["author"] = re.sub(r"^de\s+", "", first[1], flags=re.I).strip()
        result["legal_text"] = " ".join(first[2:]).strip()
    else:
        warnings.append("No se encontraron el título, el autor y el texto legal del archivo 1.")
    if page_rule(1):
        result["file1_page"] = page_rule(1)

    # File 2: the bibliographic data.
    second = lines(2)
    if second:
        result["bibliographic_heading"] = second[0]
        rest = second[1:]
        if rest and result.get("title") and rest[0].rstrip(", ").strip() == result["title"]:
            rest = rest[1:]
        if rest and re.match(r"^de\s+", rest[0], flags=re.I):
            rest = rest[1:]
        production = next((line for line in rest if line.lower().startswith("obra grabada")), "")
        isbn = next((line for line in rest if line.upper().startswith("ISBN")), "")
        others = [line for line in rest if line not in {production, isbn}]
        if production:
            match = _DATE_TAIL.match(production)
            if match:
                result["production_line"], result["production_date"] = match.group(1).strip(), match.group(2)
            else:
                result["production_line"] = production
                warnings.append("No se reconoció la fecha de la línea de producción del archivo 2.")
        else:
            warnings.append("No se encontró la línea «Obra grabada…» del archivo 2.")
        if isbn:
            result["isbn"] = re.sub(r"^ISBN[:\s]*", "", isbn, flags=re.I).strip()
        publisher = next((line for line in others if line.lower().startswith("editorial")), "")
        edition = [line for line in others if line != publisher]
        if not publisher and len(edition) > 1:
            publisher = edition.pop()
        result["edition_line"] = " ".join(edition).strip()
        result["publisher_line"] = publisher
        if not edition:
            warnings.append("El archivo 2 no indica la edición ni los datos de publicación.")
        if not publisher:
            warnings.append("El archivo 2 no indica la editorial.")
    else:
        warnings.append("No se encontró el archivo 2 (datos bibliográficos).")
    if page_rule(2):
        result["file2_page"] = page_rule(2)
    else:
        warnings.append("La planilla no dice en qué página va el archivo 2.")

    # File 3: the index.
    third = lines(3)
    entries: dict[str, str] = {}
    if third:
        result["index_heading"] = third[0]
        for line in third[1:]:
            if _INDEX_END.match(line):
                result["index_end"] = line
            elif match := _INDEX_SELF.match(line):
                result["index_page"] = match.group(1)
            elif match := _INDEX_ENTRY.match(line):
                entries[match.group(1).strip().rstrip(".")] = match.group(2)
        result["chapter_pages"] = entries
        if "index_page" not in result:
            warnings.append("El índice de la planilla no dice en qué página está.")
    else:
        warnings.append("No se encontró el archivo 3 (índice).")

    # Files 4 onwards: the headings the book is expected to have.
    expected = []
    for number in sorted(key for key in blocks if key >= 4):
        heading = next((line for line in lines(number) if not line.startswith("(")), "")
        if heading:
            expected.append({"number": number, "heading": heading.rstrip(".").strip()})
    result["expected_files"] = expected
    if expected and entries:
        def same(left: str, right: str) -> bool:
            return _normal(left) == _normal(right)
        indexed = [item for item in expected if any(same(item["heading"], heading) for heading in entries)]
        result["index_depth"] = 1 if len(indexed) < len(expected) else 2

    literal = result.pop("_closing_literal", "")
    if literal:
        title, author = result.get("title", ""), result.get("author", "")
        pair = title_and_author(title, author) if title else ""
        result["closing"] = literal.replace(pair, "{title_and_author}") if pair and pair in literal else literal
    else:
        warnings.append("No se encontró la frase final de la grabación.")
    if "absys" in result and not result["absys"]:
        warnings.append("La planilla no trae número ABSYS.")
    return result, warnings


def _normal(text: str) -> str:
    import unicodedata

    folded = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in folded if not unicodedata.combining(c))).strip()


normal_heading = _normal
