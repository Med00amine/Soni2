from app.text.pronunciation import SpanishPronunciationProcessor


def test_numbers_times_dates_and_units() -> None:
    processor = SpanishPronunciationProcessor()
    text = processor.process("15, 2026, 3.5, 15:30, 2 km, 4 kg, 20 °C, 10%, 12/10/2026.")
    assert "quince" in text
    assert "dos mil veintiséis" in text
    assert "tres coma cinco" in text
    assert "quince horas y treinta minutos" in text
    assert "dos kilómetros" in text
    assert "cuatro kilogramos" in text
    assert "veinte grados Celsius" in text
    assert "diez por ciento" in text
    assert "doce de octubre de dos mil veintiséis" in text


def test_abbreviations_symbols_and_lexicon() -> None:
    processor = SpanishPronunciationProcessor()
    text = processor.process("El Dr. y la Dra. vieron al Sr. y la Sra.; etc. IA, CPU, GPU, 20€ y 5$.")
    assert "doctor" in text
    assert "doctora" in text
    assert "señor" in text
    assert "señora" in text
    assert "etcétera" in text
    assert "inteligencia artificial" in text
    assert "ce pe u" in text
    assert "ge pe u" in text
    assert "veinte euros" in text
    assert "cinco dólares" in text


def test_unknown_names_and_normal_text_are_not_destructively_changed() -> None:
    processor = SpanishPronunciationProcessor()
    source = "García visitó Shakespeare en OpenAI."
    result = processor.process(source)
    assert "García" in result
    assert "Shakespeare" in result
    assert "OpenAI" in result
    assert processor.process("La casa es grande.") == "La casa es grande."
    assert processor.process("Dos opciones - una segura.") == "Dos opciones más una segura."


def test_custom_lexicon_is_deterministic() -> None:
    from app.text.lexicon import PronunciationLexicon

    processor = SpanishPronunciationProcessor(
        PronunciationLexicon({"OpenAI": "Open AI"}),
        lexicon_path=None,
    )
    assert processor.process("OpenAI OpenAIR") == "Open AI OpenAIR"
