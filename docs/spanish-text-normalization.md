# Spanish text normalization

## Purpose

The parser retains the reader-facing sentence in `source_text` (and the
backward-compatible `original_text` field). The pronunciation processor
creates `tts_text`; only that value is sent to MeloTTS. The DAISY DTBook is
always built from the source text.

The conversion order is:

1. Unicode and whitespace normalization.
2. Configured pronunciation lexicon.
3. Abbreviations.
4. Dates and times.
5. Measurements, percentages, and currency.
6. Decimal and integer numbers.
7. Conservative symbol replacements.
8. Final whitespace normalization.

The processor is offline and deterministic. It uses `num2words` for Spanish
cardinal numbers rather than maintaining a fragile local number implementation.

## Supported examples

| Source | TTS text |
| --- | --- |
| `15` | `quince` |
| `2026` | `dos mil veintiséis` |
| `3.5` | `tres coma cinco` |
| `15:30` | `quince horas y treinta minutos` |
| `2 km` | `dos kilómetros` |
| `10%` | `diez por ciento` |
| `Dr. García` | `doctor García` |
| `20€` | `veinte euros` |

Unknown proper names are not rewritten. Ambiguous symbols and abbreviations
are intentionally handled conservatively; specialized books may need explicit
lexicon entries.

## Pronunciation lexicon

Project-specific entries live in
[`app/text/pronunciation_lexicon.json`](../app/text/pronunciation_lexicon.json).
Add a source-to-pronunciation mapping there, for example:

```json
{
  "entries": {
    "OpenAI": "Open AI",
    "GPU": "ge pe u"
  }
}
```

Entries are boundary-aware, so `OpenAI` does not rewrite `OpenAIR`. A custom
`PronunciationLexicon` can also be injected in Python tests or worker code.

## Inspection and debugging

Inspect source/TTS pairs without generating audio:

```powershell
python -m app.cli inspect-text `
  --input ejemplo_inicial/ejemplo.xml
```

The command emits one JSON object per sentence. The processor's debug mode
logs `SOURCE` and `TTS` pairs for focused application-level diagnostics; the
CLI does not log whole books by default.

## Limitations

Dates currently support the common `DD/MM/YYYY` notation. Number conversion
is deliberately limited to contexts that are unambiguous for speech. Unknown
proper names, technical terms, hyphenated expressions, and context-dependent
currency or dash usage may require a project-specific lexicon entry.
