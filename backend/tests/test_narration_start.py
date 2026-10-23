"""Narration must begin at the narrative, not at the contents or title page.

The reported defect was books whose audio opened with the table of contents,
a synopsis or page furniture. PDF ingestion is the hard case: it produces one
pseudo-chapter per page titled "Página N", a label that says nothing, so the
page's own content has to decide.
"""

from __future__ import annotations

from app.narration_plan import apply_narration_selection, build_narration_plan


def narrated(plan) -> str:
    return "\n".join(text for _, body in apply_narration_selection(plan, None) for text in body)


def categories(plan) -> list[str]:
    return [section.category for section in plan]


# A realistic PDF: title page, copyright page, contents page, then the novel.
PDF_PAGES = [
    ("Página 1", ["La casa junto al río", "Marta Olivares", "Editorial Candil"]),
    ("Página 2", [
        "© 2021 Marta Olivares",
        "© 2021 Editorial Candil",
        "Primera edición: marzo de 2021",
        "ISBN: 978-84-1234-567-8",
        "Depósito legal: M-1234-2021",
        "Reservados todos los derechos. Queda prohibida la reproducción total o parcial.",
    ]),
    ("Página 3", [
        "Capítulo 1. La llegada .......... 9",
        "Capítulo 2. El reencuentro .......... 27",
        "Capítulo 3. La tormenta .......... 54",
        "Epílogo .......... 188",
    ]),
    ("Página 4", [
        "Amanecía despacio sobre el río cuando Marta abrió la ventana de la buhardilla.",
        "El aire olía a tierra mojada y a los jazmines que su madre había plantado años atrás.",
    ]),
]


def test_pdf_title_and_copyright_pages_are_not_narrated() -> None:
    plan = build_narration_plan(PDF_PAGES)
    text = narrated(plan)
    assert "Marta Olivares" not in text
    assert "ISBN" not in text
    assert "Depósito legal" not in text
    assert "Reservados todos los derechos" not in text


def test_pdf_contents_page_is_not_narrated_even_without_an_index_heading() -> None:
    plan = build_narration_plan(PDF_PAGES)
    text = narrated(plan)
    assert "Capítulo 1. La llegada .......... 9" not in text
    assert ".........." not in text
    assert "toc" in categories(plan)


def test_pdf_narration_starts_at_the_first_real_prose_page() -> None:
    plan = build_narration_plan(PDF_PAGES)
    included = apply_narration_selection(plan, None)
    assert included, "the novel's text must still be narrated"
    first_line = included[0][1][0]
    assert first_line.startswith("Amanecía despacio sobre el río")


def test_pdf_page_labels_are_never_spoken() -> None:
    plan = build_narration_plan(PDF_PAGES)
    titles = [title for title, _body in apply_narration_selection(plan, None)]
    assert all(not title.lower().startswith("página") for title in titles)


def test_excluded_front_matter_is_still_offered_for_the_user_to_include() -> None:
    plan = build_narration_plan(PDF_PAGES)
    excluded = [section for section in plan if not section.included]
    assert excluded, "front matter must be listed, not silently discarded"
    for section in excluded:
        assert section.reason, "every exclusion must explain itself"
        assert section.paragraphs or section.category == "toc"
    # The user can override the default and include the contents page.
    everything = {section.id for section in plan}
    assert "ISBN: 978-84-1234-567-8" in "\n".join(
        text for _, body in apply_narration_selection(plan, everything) for text in body
    )


def test_a_short_page_in_the_middle_of_a_book_is_not_treated_as_a_title_page() -> None:
    pages = [
        ("Página 1", ["Capítulo 1", "Empieza la historia en un pueblo pequeño junto al mar."]),
        ("Página 2", ["Siguió caminando durante horas sin encontrar a nadie en el camino."]),
        ("Página 3", ["—Nunca", "—dijo ella"]),
        ("Página 4", ["Después de aquello, el silencio se volvió costumbre en la casa."]),
    ]
    plan = build_narration_plan(pages)
    text = narrated(plan)
    assert "—Nunca" in text, "short dialogue pages are content, not front matter"


def test_a_synopsis_page_is_not_narrated_as_if_it_were_the_book() -> None:
    plan = build_narration_plan([
        ("Sinopsis", ["Una novela sobre el regreso a un pueblo olvidado."]),
        ("Capítulo 1", ["Amaneció sobre el río."]),
    ])
    assert plan[0].category == "synopsis" and not plan[0].included
    assert "Una novela sobre el regreso" not in narrated(plan)
    assert "Amaneció sobre el río." in narrated(plan)


def test_prologue_and_epilogue_are_kept_but_bibliography_is_optional() -> None:
    plan = build_narration_plan([
        ("Prólogo", ["Una advertencia necesaria para el lector."]),
        ("Capítulo 1", ["Comienza la novela."]),
        ("Epílogo", ["Años después."]),
        ("Bibliografía", ["Olivares, M. (2019)."]),
    ])
    assert [title for title, _ in apply_narration_selection(plan, None)] == [
        "Prólogo", "Capítulo 1", "Epílogo",
    ]
    assert plan[-1].category == "back" and not plan[-1].included


def test_an_ambiguous_opening_is_narrated_and_flagged_rather_than_dropped() -> None:
    plan = build_narration_plan([
        ("", ["El tren llegó al pueblo al amanecer y nadie bajó de él aquella mañana de invierno."]),
    ])
    assert plan[0].included and plan[0].needs_review
    assert "El tren llegó al pueblo" in narrated(plan)


def test_a_book_of_only_front_matter_still_produces_something_to_narrate() -> None:
    # Policy must never leave a plan with nothing included; the longest prose
    # section is kept and flagged for review instead.
    plan = build_narration_plan([
        ("Página 1", ["Título del documento", "Autor desconocido"]),
        ("Página 2", [
            "ISBN: 978-84-0000-000-0",
            "Primera edición: 2020",
            "Reservados todos los derechos de esta obra para cualquier soporte conocido. "
            "Queda prohibida la reproducción total o parcial sin la autorización escrita del editor, "
            "y se recuerda al lector que esta página no constituye contenido narrativo alguno.",
        ]),
    ])
    assert any(section.included for section in plan)
    flagged = [section for section in plan if section.included]
    assert all(section.needs_review for section in flagged)


def test_a_parser_invented_prologue_heading_cannot_narrate_a_copyright_page() -> None:
    # Plain-text ingestion labels everything before chapter one "Prólogo".
    # That heading must not turn a credits page into narrative content.
    plan = build_narration_plan([
        ("Prólogo", [
            "La casa junto al río",
            "Marta Olivares",
            "© 2021 Marta Olivares",
            "ISBN: 978-84-1234-567-8",
            "Depósito legal: M-1234-2021",
            "Primera edición: marzo de 2021",
            "Reservados todos los derechos.",
        ]),
        ("Capítulo 1", ["Amanecía despacio sobre el río cuando Marta abrió la ventana."]),
    ])
    text = narrated(plan)
    assert "ISBN" not in text
    assert "Depósito legal" not in text
    assert "Amanecía despacio sobre el río" in text


def test_a_real_prologue_is_still_narrated() -> None:
    # The strict detectors must not swallow genuine front-matter prose.
    plan = build_narration_plan([
        ("Prólogo", ["Esta novela nació de una conversación en un tren nocturno, "
                     "cuando todavía creía que los pueblos no desaparecen del todo."]),
        ("Capítulo 1", ["Amanecía despacio sobre el río."]),
    ])
    assert "Esta novela nació" in narrated(plan)
