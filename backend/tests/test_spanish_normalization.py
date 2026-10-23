from app.spanish_normalization import compare_transcripts, canonical_tokens


def _keys(source, spoken):
    left, right = canonical_tokens(source, spoken)
    return [item.value for item in left], [item.value for item in right]


def test_spanish_cardinal_and_compound_numbers_match():
    for source, spoken in [
        ("147", "ciento cuarenta y siete"),
        ("3 500", "tres mil quinientos"),
        ("12", "doce"),
        ("960", "novecientos sesenta"),
        ("3.500", "tres mil quinientos"),
    ]:
        assert compare_transcripts(source, spoken) == []
    assert compare_transcripts("3.er edicion", "tercero edicion") == []


def test_decimal_uses_span_not_concatenated_integer():
    assert compare_transcripts("2,5", "dos coma cinco") == []
    assert compare_transcripts("2,5", "25")


def test_units_match_only_when_quantity_and_unit_match():
    assert compare_transcripts("18 km", "dieciocho kil\u00f3metros") == []
    assert compare_transcripts("22 millas", "veintid\u00f3s millas") == []
    assert compare_transcripts("9 m", "nueve metros") == []
    assert compare_transcripts("9 metros", "nueve metros y medio")
    assert compare_transcripts("9 km", "nueve metros")


def test_spanish_clock_times_match_without_merging_whitespace_numbers():
    assert compare_transcripts("6:45", "seis cuarenta y cinco") == []
    assert compare_transcripts("23:10", "veintitr\u00e9s diez") == []
    assert compare_transcripts("3:20", "tres veinte") == []
    assert _keys("3 20", "tres veinte") == (["num:3", "num:20"], ["num:3", "num:20"])
    assert compare_transcripts("3 20", "trescientos veinte")


def test_date_percent_and_currency_expressions_match_when_explicit():
    assert compare_transcripts("12/03/2024", "doce de marzo de dos mil veinticuatro") == []
    assert compare_transcripts("12%", "doce por ciento") == []
    assert compare_transcripts("20 \u20ac", "veinte euros") == []


def test_accent_punctuation_and_whitespace_are_harmless():
    assert compare_transcripts("La canci\u00f3n est\u00e1 aqu\u00ed.", "La cancion esta aqui") == []


def test_wrong_missing_and_added_content_stays_visible_with_original_spelling():
    issue = compare_transcripts("147", "ciento cuarenta y ocho")
    assert len(issue) == 1
    assert issue[0]["expected"] == "147"
    assert issue[0]["recognized"] == "ciento cuarenta y ocho"
    assert issue[0]["category"] == "likely_mismatch"
    assert compare_transcripts("Hab\u00eda 12 cajas", "Hab\u00eda cajas")
    assert compare_transcripts("El camino mide 9 m", "El camino mide nueve metros") == []
    assert compare_transcripts("El camino est\u00e1 abierto", "El camino est\u00e1 completamente abierto")


def test_different_quantities_remain_detected():
    assert compare_transcripts("18 km", "diecinueve kil\u00f3metros")
    assert compare_transcripts("960", "novecientos cincuenta")
