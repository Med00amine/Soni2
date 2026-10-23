from app.narration_plan import apply_narration_selection, build_narration_plan


def selected_text(plan):
    return "\n".join(text for _, body in apply_narration_selection(plan, None) for text in body)


def test_index_before_first_chapter_is_excluded_by_default():
    plan = build_narration_plan([
        ("ÍNDICE", ["Capítulo 1 .......... 3", "Capítulo 2 .......... 18"]),
        ("Capítulo 1. La llegada", ["Amanece en la isla."]),
    ])
    assert plan[0].category == "toc" and not plan[0].included
    included = apply_narration_selection(plan, None)
    assert included[0][0] == "Capítulo 1. La llegada"
    assert "ÍNDICE" not in selected_text(plan)
    assert "Amanece en la isla." in selected_text(plan)


def test_roman_numeral_dotted_leaders_are_classified_as_index_rows():
    plan = build_narration_plan([("Índice", ["I. Prólogo .......... v", "II. Capítulo primero .......... ix", "III. Capítulo segundo .......... 24"]), ("Capítulo I", ["Texto real."])])
    assert plan[0].category == "toc"
    assert not plan[0].included
    assert "Prólogo" not in selected_text(plan)
    assert "Texto real." in selected_text(plan)


def test_meaningful_prologue_is_kept_before_main_chapter():
    plan = build_narration_plan([("Prólogo", ["Una historia necesaria."]), ("Capítulo 1", ["Comienza la novela."])])
    assert [title for title, _ in apply_narration_selection(plan, None)] == ["Prólogo", "Capítulo 1"]


def test_synopsis_is_excluded_but_chapter_remains():
    plan = build_narration_plan([("Sinopsis", ["Resumen de la obra."]), ("Capítulo 1", ["Narración principal."])])
    assert plan[0].category == "synopsis" and not plan[0].included
    assert "Resumen de la obra." not in selected_text(plan)
    assert "Narración principal." in selected_text(plan)


def test_epilogue_is_retained_and_bibliography_is_optional():
    plan = build_narration_plan([("Capítulo 1", ["Final de la novela."]), ("Epílogo", ["Años después."]), ("Bibliografía", ["Referencia bibliográfica."])])
    assert [title for title, _ in apply_narration_selection(plan, None)] == ["Capítulo 1", "Epílogo"]
    assert plan[-1].category == "back" and not plan[-1].included


def test_headingless_first_page_is_preserved_and_flagged_for_review():
    plan = build_narration_plan([("", ["El tren llegó al pueblo al amanecer."])])
    assert plan[0].included and plan[0].needs_review
    assert "El tren llegó al pueblo al amanecer." in selected_text(plan)


def test_ambiguous_structure_is_included_and_flagged_not_silently_removed():
    plan = build_narration_plan([("Fragmento", ["No sabemos si esto abre la narración."])])
    assert plan[0].included and plan[0].needs_review
    assert "No sabemos si esto abre la narración." in selected_text(plan)


