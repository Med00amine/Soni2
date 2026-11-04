"""The reader's changes to a book before it is narrated: the text that is produced.

Step 2 of the interface lets a person correct a phrase, add one, delete one or
delete a whole chapter. Those changes are *the* text of the audiobook: every
path that narrates (the reviewed production, the direct download, the
editor's outline) applies them here, the same way, so none of them can fall
back to the text as it was extracted.

A change is attached to a **slot**, a place in the book that does not move
when other places change:

* ``"<section id>:<n>"``: paragraph ``n`` of a section of the narration plan
  (``section-7:12``), as the book was read;
* ``"<section id>:<n>+"``: new text the reader inserted right after that
  paragraph (``n`` is ``-1`` for text before the first paragraph).

Section ids come from the narration plan, so a slot keeps meaning the same
paragraph when the reader includes or leaves out other sections.

A slot's value is either a list of narration units or a plain string:

* a **list** is the paragraph exactly as it will be narrated, one entry per
  narration unit. A phrase the reader did not touch stays byte for byte what
  it was, so its audio is still the one already paid for; an empty list
  deletes the paragraph;
* a **string** replaces the paragraph's text, which is then split into units
  as usual (a note is edited this way; it is also what the older
  ``corrections_json`` meant).

A section's title can be corrected (``titles``) or taken out of the
narration (``deleted_titles``): the section then keeps a navigation label,
but it is not read. A section whose title and paragraphs are all deleted is
not in the audiobook at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.narration_plan import SelectedChapter, is_generated

MAX_SLOTS = 20000
MAX_UNIT_CHARACTERS = 20000
_SLOT = re.compile(r"(?P<section>[A-Za-z0-9_.-]+):(?P<paragraph>-1|\d+)(?P<insert>\+?)")


class NothingLeft(ValueError):
    """The edits delete everything selected: there would be no book to narrate."""


@dataclass
class BookEdits:
    slots: dict[str, list[str] | str] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    deleted_titles: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.slots or self.titles or self.deleted_titles)

    def as_dict(self) -> dict:
        return {"slots": self.slots, "titles": self.titles, "deleted_titles": sorted(self.deleted_titles)}

    def fingerprint(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


def normalize_unit(text: str) -> str:
    """A unit as the narration splits it: one line, single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def parse(edits_json: str = "", *, corrections_json: str = "", chapters: list | None = None) -> BookEdits:
    """Read and validate the reader's changes.

    ``corrections_json`` is the older form, ``{"chapter:paragraph": text}``
    by position among the selected ``chapters``; it becomes string slots.
    """
    edits = BookEdits()
    if edits_json and edits_json.strip():
        try:
            payload = json.loads(edits_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Los cambios del libro no tienen un formato válido.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Los cambios del libro no son válidos.")
        slots = payload.get("slots") or {}
        titles = payload.get("deleted_titles") or []
        renamed = payload.get("titles") or {}
        if (not isinstance(slots, dict) or not isinstance(titles, list) or not isinstance(renamed, dict)
                or len(slots) > MAX_SLOTS):
            raise ValueError("Los cambios del libro no son válidos.")
        for key, value in renamed.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("Cada título corregido debe tener entre 1 y 500 caracteres.")
            edits.titles[key] = normalize_unit(value)
        for key, value in slots.items():
            if not isinstance(key, str) or not _SLOT.fullmatch(key):
                raise ValueError("Un cambio del libro no indica un lugar válido.")
            match = _SLOT.fullmatch(key)
            if match["paragraph"] == "-1" and not match["insert"]:
                raise ValueError("Un cambio del libro no indica un lugar válido.")
            if isinstance(value, str):
                if match["insert"]:
                    value = [value] if value.strip() else []
                elif len(value) > MAX_UNIT_CHARACTERS:
                    raise ValueError("Cada párrafo corregido debe contener como máximo 20 000 caracteres.")
            if isinstance(value, list):
                if any(not isinstance(unit, str) or len(unit) > MAX_UNIT_CHARACTERS for unit in value):
                    raise ValueError("Cada frase debe ser un texto de como máximo 20 000 caracteres.")
                value = [normalize_unit(unit) for unit in value if unit.strip()]
            elif not isinstance(value, str):
                raise ValueError("Un cambio del libro no es válido.")
            edits.slots[key] = value
        if any(not isinstance(item, str) or not item for item in titles):
            raise ValueError("Los títulos eliminados no son válidos.")
        edits.deleted_titles = set(titles)
    if corrections_json and corrections_json.strip() not in {"", "{}"}:
        _legacy_corrections(edits, corrections_json, chapters or [])
    return edits


def _legacy_corrections(edits: BookEdits, corrections_json: str, chapters: list) -> None:
    try:
        corrections = json.loads(corrections_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Las correcciones del libro no tienen un formato valido.") from exc
    if not isinstance(corrections, dict) or len(corrections) > 5000:
        raise ValueError("La lista de correcciones no es valida.")
    for key, value in corrections.items():
        match = re.fullmatch(r"(\d+):(\d+)", str(key))
        if not match or not isinstance(value, str):
            raise ValueError("Una correccion del libro no es valida.")
        chapter_index, paragraph_index = (int(part) for part in match.groups())
        if chapter_index >= len(chapters) or paragraph_index >= len(chapters[chapter_index][1]):
            raise ValueError("Una correccion hace referencia a un parrafo que no existe.")
        replacement = value.strip()
        if not replacement or len(replacement) > 20000:
            raise ValueError("Cada parrafo corregido debe contener entre 1 y 20 000 caracteres.")
        section = getattr(chapters[chapter_index], "section_id", "") or f"chapter{chapter_index}"
        edits.slots.setdefault(f"{section}:{paragraph_index}", replacement)


def validate_against(edits: BookEdits, sections: dict[str, int]) -> None:
    """Refuse changes made to another version of the book.

    ``sections`` maps every section id of the book's narration plan to its
    number of paragraphs. A change to a section the reader left out is kept
    (it applies again if the section comes back); a change to a paragraph or
    a section the book does not have means the book was read differently
    (another file, another page range), and silently dropping the change
    would narrate text the reader had corrected.
    """
    for key in edits.slots:
        match = _SLOT.fullmatch(key)
        section, paragraph = match["section"], int(match["paragraph"])
        if section not in sections:
            raise ValueError("Los cambios corresponden a otra versión del libro. Vuelve a abrir el libro.")
        if paragraph >= sections[section]:
            raise ValueError("Un cambio hace referencia a un párrafo que no existe en esta versión del libro.")
    unknown = (edits.deleted_titles | set(edits.titles)) - set(sections)
    if unknown:
        raise ValueError("Los cambios corresponden a otra versión del libro. Vuelve a abrir el libro.")


def _text(value: list[str] | str) -> str:
    # Units are kept apart by a blank line, which is where the narration
    # splits a paragraph (daisy_export.make_paragraphs): each unit stays the
    # unit it was.
    return "\n\n".join(value).strip() if isinstance(value, list) else value.strip()


def apply(chapters: list, edits: BookEdits, provenance: list[tuple[dict, list[dict]]] | None = None):
    """The selected chapters with the reader's changes, and where each paragraph comes from.

    Returns ``(chapters, provenance)``. Every returned chapter carries
    ``section_id`` and ``slots`` (the slot of each of its paragraphs), and
    every provenance record of a paragraph its ``slot``, so whatever is built
    from them can say which place of the book a unit belongs to.
    """
    result: list[SelectedChapter] = []
    result_provenance: list[tuple[dict, list[dict]]] = []
    for index, chapter in enumerate(chapters):
        paragraphs = list(chapter[1])
        section = getattr(chapter, "section_id", "") or f"chapter{index}"
        title = edits.titles.get(section, chapter[0])
        title_origin, origins = (provenance[index] if provenance is not None else ({}, []))
        origins = list(origins) + [{}] * (len(paragraphs) - len(origins))
        texts: list[str] = []
        slots: list[str] = []
        records: list[dict] = []

        def place(slot: str, text: str, origin: dict) -> None:
            if text:
                texts.append(text)
                slots.append(slot)
                records.append({**origin, "slot": slot})

        inserted = edits.slots.get(f"{section}:-1+")
        if inserted:
            place(f"{section}:-1+", _text(inserted), {})
        for number, paragraph in enumerate(paragraphs):
            slot = f"{section}:{number}"
            place(slot, _text(edits.slots[slot]) if slot in edits.slots else paragraph, origins[number])
            inserted = edits.slots.get(f"{slot}+")
            if inserted:
                # New text is plain text of the section it was added to.
                place(f"{slot}+", _text(inserted), {})
        generated = is_generated(chapter) or section in edits.deleted_titles
        if not texts and (generated or not title.strip()):
            continue
        selected = SelectedChapter(title, texts, generated)
        selected.section_id = section
        selected.slots = slots
        result.append(selected)
        result_provenance.append(({**title_origin, "section_id": section, "title_deleted":
                                   section in edits.deleted_titles}, records))
    if chapters and not result:
        raise NothingLeft("Has eliminado todo el contenido del libro: no queda nada que narrar.")
    return result, (result_provenance if provenance is not None else None)
