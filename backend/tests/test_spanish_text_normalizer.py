"""The spoken form of written Peninsular Spanish, with every change accounted for."""

from __future__ import annotations

import pytest

from app.spanish_text_normalizer import cardinal, normalize_for_speech, noun_gender, ordinal


def spoken(text: str) -> str:
    return normalize_for_speech(text).spoken


@pytest.mark.parametrize("text,expected", [
    # Dates are day/month/year in Spain, and "1" is "uno", not "primero".
    ("El 12/03/2024 firmaron.", "El doce de marzo de dos mil veinticuatro firmaron."),
    ("Nació el 1/2/1990.", "Nació el uno de febrero de mil novecientos noventa."),
    ("El 2024-01-05 llovió.", "El cinco de enero de dos mil veinticuatro llovió."),
    # Clock times, read as a Peninsular narrator says them.
    ("Salió a las 6:45.", "Salió a las seis y cuarenta y cinco."),
    ("Volvió a las 7:15 y a las 9:30.", "Volvió a las siete y cuarto y a las nueve y media."),
    ("Abre a las 9:00.", "Abre a las nueve en punto."),
    ("Cierra a la 1:05.", "Cierra a la una y cinco."),
    ("Empieza a las 18:30 h.", "Empieza a las dieciocho treinta horas."),
    ("Fue a las 23:00.", "Fue a las veintitrés horas."),
    # Money: gender and apocope agree with the currency.
    ("Costó 3,50 €.", "Costó tres coma cincuenta euros."),
    ("Pagó 1 € y luego 21 €.", "Pagó un euro y luego veintiún euros."),
    ("Cobró $100.", "Cobró cien dólares."),
    ("Debía £200.", "Debía doscientas libras."),
    ("Ganó 1.000.000 €.", "Ganó un millón de euros."),
    # Percentages and units.
    ("Subió un 12 % y luego un 2,5%.", "Subió un doce por ciento y luego un dos coma cinco por ciento."),
    ("Mide 9 m y pesa 21 t.", "Mide nueve metros y pesa veintiuna toneladas."),
    ("Iba a 100 km/h.", "Iba a cien kilómetros por hora."),
    ("Hacía -5 °C.", "Hacía menos cinco grados."),
    ("Compró 4 ha.", "Compró cuatro hectáreas."),
    # Grouped thousands and decimals, agreeing with the noun that follows.
    ("Vinieron 3.500 personas.", "Vinieron tres mil quinientas personas."),
    ("Eran 1.200.000 personas.", "Eran un millón doscientas mil personas."),
    ("Pesaba 3,14 kilos.", "Pesaba tres coma catorce kilos."),
    ("Midió 2,05 metros.", "Midió dos coma cero cinco metros."),
    # Ordinals.
    ("Quedó 1.º y ella 2.ª en el 3.er intento.", "Quedó primero y ella segunda en el tercer intento."),
    ("La n.º 5 y el núm. 7.", "La número 5 y el número 7."),
    # Roman numerals where they are unambiguous.
    ("Capítulo XIV", "Capítulo catorce"),
    ("en el s. XVI", "en el siglo dieciséis"),
    ("Felipe II e Isabel II", "Felipe segundo e Isabel segunda"),
    ("Alfonso XIII", "Alfonso trece"),
    ("II", "dos"),
    # Abbreviations.
    ("El Sr. García y la Dra. López.", "El señor García y la doctora López."),
    ("Vivía en la Avda. de la Paz, pág. 23.", "Vivía en la avenida de la Paz, página 23."),
    ("Trajo pan, vino, etc. Luego se fue.", "Trajo pan, vino, etcétera. Luego se fue."),
    ("Viajó a EE. UU. en 1990.", "Viajó a Estados Unidos en 1990."),
    ("Murió en el 44 a. C.", "Murió en el 44 antes de Cristo."),
    ("Ud. y Uds. lo saben.", "Usted y ustedes lo saben."),
    ("—Sr. Arrieta, ¿viene?", "—Señor Arrieta, ¿viene?"),
    ("Vive en c/ Mayor.", "Vive en calle Mayor."),
    # Symbols between what they join.
    ("Romeo & Julieta", "Romeo y Julieta"),
    ("2 + 2 = 4", "2 más 2 igual a 4"),
])
def test_written_forms_become_what_a_peninsular_narrator_says(text, expected) -> None:
    assert spoken(text) == expected


@pytest.mark.parametrize("text", [
    "La casa tenía 147 ventanas.",       # plain integers: the voice reads them in context
    "En 1990 ha sido largo.",            # "ha" is the verb
    "Versión 1.2.3 del programa.",       # not a Spanish number
    "Vitamina D y la letra X.",          # letters, not numerals
    "Escribió a ana@example.com.",       # an address is left alone
])
def test_text_that_is_safe_as_written_is_left_alone(text) -> None:
    assert spoken(text) == text


@pytest.mark.parametrize("text,code", [
    ("Llegó D. Pedro.", "ambiguous_abbreviation"),       # don, or an initial?
    ("Comió 3/4 de la tarta.", "fraction_or_date"),     # a fraction, or 3 April?
    ("Su código es 28013.", "long_number"),             # a quantity, a postcode, a phone?
    ("Tenía 4 ha de terreno.", "ambiguous_unit"),       # hectares, or "ha de" (must)?
    ("Lo dijo el 31/02/2020.", "invalid_date"),         # no such day: left alone
    ("Véase ed. citada.", "ambiguous_abbreviation"),
])
def test_ambiguous_expressions_are_flagged_and_left_as_written(text, code) -> None:
    result = normalize_for_speech(text)
    assert code in {flag.code for flag in result.flags}
    flagged = next(flag for flag in result.flags if flag.code == code)
    assert flagged.text in result.spoken  # not rewritten
    assert flagged.severity == "review"


def test_names_a_voice_may_say_the_english_way_are_put_on_the_listening_list() -> None:
    result = normalize_for_speech("Salió con Smith hacia Washington y la OTAN.")
    watch = {flag.text for flag in result.flags if flag.severity == "watch"}
    assert {"Smith", "Washington"} <= watch
    assert "OTAN" not in watch  # read as a word, like ONU
    assert not normalize_for_speech("Desde Huesca llegó Héctor.").flags  # Spanish names are not foreign


def test_a_title_in_capitals_is_not_a_list_of_acronyms() -> None:
    assert not normalize_for_speech("LOS OJOS VERDES").flags


def test_every_change_records_the_original_text_and_where_it_was() -> None:
    source = "El Sr. Ruiz pagó 3,50 € el 12/03/2024."
    result = normalize_for_speech(source)
    assert result.source == source
    assert [change.rule for change in result.changes] == ["abbreviation", "currency", "date"]
    for change in result.changes:
        assert source[change.start:change.end] == change.original
    assert result.as_dict()["changes"][1] == {"rule": "currency", "original": "3,50 €",
                                              "spoken": "tres coma cincuenta euros", "start": 17, "end": 23}


@pytest.mark.parametrize("text", [
    "El 12/03/2024, a las 18:30 h, el Sr. García pagó 3,50 € por 245 km a 120 km/h.",
    "Vinieron 3.500 personas; subió un 2,5 %; Felipe II; el s. XVI; la pág. 23; etc.",
    "Quedó 1.º, la 2.ª, el 3.er; 1.000.000 €; -5 °C; c/ Mayor; EE. UU.; 44 a. C.",
])
def test_normalizing_the_spoken_form_again_changes_nothing(text) -> None:
    once = spoken(text)
    assert spoken(once) == once


def test_spanish_numbers_in_words() -> None:
    assert cardinal(21, "m") == "veintiún" and cardinal(21, "f") == "veintiuna" and cardinal(21) == "veintiuno"
    assert cardinal(101, "m") == "ciento un" and cardinal(100) == "cien"
    assert cardinal(500, "f") == "quinientas" and cardinal(1000) == "mil"
    assert cardinal(2_500_000) == "dos millones quinientos mil"
    assert ordinal(13) == "decimotercero" and ordinal(21, "f") == "vigésima primera"
    assert ordinal(3, apocope=True) == "tercer" and ordinal(150) is None
    assert noun_gender("personas") == "f" and noun_gender("días") == "m" and noun_gender("canciones") == "f"
