"""Reading a word the way Spanish does, and respelling it so a voice agrees."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app import main, pronunciation, providers
from app.sentences import build_synthesis_text


# --------------------------------------------------------------------------- #
# Syllables and stress: the rules Spanish actually uses
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("word,parts", [
    ("casa", ["ca", "sa"]),
    ("discapacidad", ["dis", "ca", "pa", "ci", "dad"]),
    ("general", ["ge", "ne", "ral"]),
    ("transporte", ["trans", "por", "te"]),
    ("obstáculo", ["obs", "tá", "cu", "lo"]),
    ("aire", ["ai", "re"]),
    ("ciudad", ["ciu", "dad"]),
    ("abril", ["a", "bril"]),
])
def test_words_are_split_into_the_right_syllables(word, parts) -> None:
    # A trailing consonant closes the last syllable; it is not one of its own.
    assert pronunciation.syllables(word) == parts


@pytest.mark.parametrize("word,stressed", [
    ("casa", "ca"),            # ends in a vowel: next to last
    ("general", "ral"),        # ends in a consonant: last
    ("discapacidad", "dad"),
    ("obstáculo", "tá"),       # the written accent wins
    ("árbol", "ár"),
    ("corazón", "zón"),
    ("examen", "xa"),
])
def test_the_stress_falls_where_spanish_puts_it(word, stressed) -> None:
    parts = pronunciation.syllables(word)
    assert parts[pronunciation.stressed_index(word)] == stressed


# --------------------------------------------------------------------------- #
# IPA, for showing a reviewer why a word sounded wrong
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("word,sound", [
    ("casa", "ˈkasa"),
    ("zapato", "θaˈpato"),      # Castilian: z is θ
    ("cielo", "ˈθielo"),        # c before e is θ
    ("general", "xeneˈral"),    # g before e is x
    ("guerra", "ˈɡera"),        # gu before e is a plain g; rr is a trill
    ("llave", "ˈʎabe"),         # ll, and v sounds like b
    ("pingüino", "pinˈɡwino"),  # ü is pronounced
    ("hola", "ˈola"),           # h is silent
    ("niño", "ˈniɲo"),
])
def test_the_ipa_follows_castilian_spanish(word, sound) -> None:
    assert pronunciation.ipa(word) == sound


def test_a_word_with_no_vowel_has_no_pronunciation() -> None:
    assert pronunciation.ipa("") == ""
    assert pronunciation.ipa("xyz") == ""


# --------------------------------------------------------------------------- #
# Respelling: the repair
# --------------------------------------------------------------------------- #

def test_a_spanish_word_needs_no_respelling() -> None:
    analysis = pronunciation.analyse("casa")
    assert analysis.looks_spanish
    assert not any(item.label == "Adaptado al español" for item in analysis.suggestions)


def test_a_foreign_word_is_recognised_and_adapted() -> None:
    analysis = pronunciation.analyse("Washington")
    assert not analysis.looks_spanish
    adapted = [item for item in analysis.suggestions if item.label == "Adaptado al español"]
    assert adapted, analysis.suggestions
    # The suggestion must be readable by Spanish rules, which is the point.
    assert pronunciation.looks_spanish(adapted[0].respelling), adapted[0].respelling


def test_an_initialism_can_be_spelled_out_or_read_as_a_word() -> None:
    analysis = pronunciation.analyse("ONCE")
    labels = {item.label for item in analysis.suggestions}
    assert "Letra por letra" in labels
    assert "Como una palabra" in labels
    letters = next(i for i in analysis.suggestions if i.label == "Letra por letra")
    assert letters.respelling == "o ene ce e"
    word = next(i for i in analysis.suggestions if i.label == "Como una palabra")
    assert word.respelling == "once"


def test_every_suggestion_explains_itself() -> None:
    for word in ("Washington", "ONCE", "discapacidad"):
        for item in pronunciation.analyse(word).suggestions:
            assert item.respelling and item.label and item.reason


# --------------------------------------------------------------------------- #
# The endpoints a reviewer uses
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "database_path", tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={
        "tts_provider": "mock", "asr_provider": "mock", "tts_voice_id": "mock-es-es-female",
    }))
    return TestClient(main.app)


def test_analyse_reports_the_sound_and_the_options(client) -> None:
    response = client.post("/api/pronunciation/analyse", json={"word": "Washington"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["word"] == "Washington"
    assert body["syllables"]
    assert body["looks_spanish"] is False
    assert body["suggestions"]
    assert body["saved_respelling"] == ""


def test_analyse_refuses_nonsense_input(client) -> None:
    assert client.post("/api/pronunciation/analyse", json={"word": "   "}).status_code == 422
    assert client.post("/api/pronunciation/analyse", json={"word": "x" * 200}).status_code == 422


def test_a_candidate_can_be_heard_before_it_is_saved(client) -> None:
    response = client.post("/api/pronunciation/preview", json={
        "word": "Washington", "respelling": "guáshinton",
        "sentence": "Vivía en Washington desde niño.",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    # The word is heard inside its sentence, with the respelling in its place.
    assert body["spoken_text"] == "Vivía en guáshinton desde niño."
    assert body["audio_base64"]
    assert providers.mock_transcript(base64.b64decode(body["audio_base64"])) == body["spoken_text"]


def test_a_candidate_can_be_heard_on_its_own(client) -> None:
    response = client.post("/api/pronunciation/preview",
                           json={"word": "ONCE", "respelling": "o ene ce e"})
    assert response.status_code == 200, response.text
    assert response.json()["spoken_text"] == "o ene ce e"


def test_saving_one_pronunciation_keeps_the_others(client) -> None:
    client.post("/api/pronunciation-lexicon/entries",
                json={"term": "ONCE", "pronunciation": "once"})
    response = client.post("/api/pronunciation-lexicon/entries",
                           json={"term": "Washington", "pronunciation": "guáshinton"})
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert entries["ONCE"] == "once"
    assert entries["Washington"] == "guáshinton"


def test_a_saved_pronunciation_is_reported_back_for_the_word(client) -> None:
    client.post("/api/pronunciation-lexicon/entries",
                json={"term": "Washington", "pronunciation": "guáshinton"})
    body = client.post("/api/pronunciation/analyse", json={"word": "Washington"}).json()
    assert body["saved_respelling"] == "guáshinton"


def test_a_saved_pronunciation_changes_what_the_voice_is_given(client) -> None:
    client.post("/api/pronunciation-lexicon/entries",
                json={"term": "Washington", "pronunciation": "guáshinton"})
    lexicon = client.get("/api/pronunciation-lexicon").json()["entries"]
    narration = build_synthesis_text("Vivía en Washington.", lexicon)
    # This is the actual repair: the voice is handed the respelling.
    assert narration == "Vivía en guáshinton."


def test_a_pronunciation_can_be_removed(client) -> None:
    client.post("/api/pronunciation-lexicon/entries",
                json={"term": "ONCE", "pronunciation": "once"})
    assert client.delete("/api/pronunciation-lexicon/entries/ONCE").status_code == 200
    assert client.get("/api/pronunciation-lexicon").json()["entries"] == {}
    assert client.delete("/api/pronunciation-lexicon/entries/ONCE").status_code == 404


def test_an_empty_pronunciation_is_refused(client) -> None:
    response = client.post("/api/pronunciation-lexicon/entries",
                           json={"term": "ONCE", "pronunciation": "  "})
    assert response.status_code == 422
