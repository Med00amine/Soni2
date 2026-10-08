from app.text.lexicon import PronunciationLexicon
from app.text.normalizer import TextNormalizer
from app.text.segmenter import SentenceSegmenter


def test_normalizer_is_non_destructive_and_uses_lexicon() -> None:
    source = "  Dr.\u0301   usa GPU  "
    assert TextNormalizer(PronunciationLexicon({"Dr.́": "doctor", "GPU": "G P U"})).normalize(source) == "doctor usa G P U"
    assert source == "  Dr.\u0301   usa GPU  "


def test_segmenter_preserves_punctuation() -> None:
    assert SentenceSegmenter().segment("Primera frase. ¿Segunda frase? Tercera.") == [
        "Primera frase.",
        "¿Segunda frase?",
        "Tercera.",
    ]

