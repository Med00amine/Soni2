# Soni2 audiobook quality system (2026-10-25)

How Soni2 makes sure that a finished Spanish audiobook contains the whole book,
read in order, in Peninsular Spanish, synchronized with its text in a valid
DAISY 3 package, and what it can and cannot prove about that. The upgrade it
describes started from commit `eebfa0e`. Evidence files are in
[`docs/evidence/`](evidence/).

**Nothing here guarantees an error-free book.** Speech synthesis and speech
recognition are probabilistic. The system detects errors, repairs the ones it
can, escalates the rest to a person, and refuses to call a book `PASSED` while
anything required is missing, failed or unresolved. A book that passes has
passed every *automated* check; that is a measured result, not proof of
perfection.

## 1. Pipeline and where each check sits

```
upload ─► extraction ─► narration plan ─► normalization ─► units
          │ coverage report                │ change log + flags
          ▼                                ▼
     extraction-report.json         per unit: text · spoken_text · synthesis_text · provenance
                                               │
             ┌─────────────────────────────────┘
             ▼ for every unit (bounded repair loop, every take kept)
   synthesize (ElevenLabs, seed, neighbour context, jittered retries)
      → integrity (MP3 frames · decode · silence · clipping · cut-off · speaking rate)
      → recognizer 1 (Deepgram nova-3)  ─ verified ─► accept
      → on a difference: recognizer 2 (ElevenLabs Scribe v2) + word timings
      → diagnosis: synthesis | audio | normalization | source | asr
      → accept · accept & list for listening · retake · person
             ▼
   assembly (mastering) → DAISY 3 → structural check + official DTDs
      → completeness chain: source → units → audio → DTBook → SMIL → NCX/OPF
      → release gate: PENDING → GENERATING → VALIDATING → NEEDS_REVIEW / BLOCKED / PASSED → RELEASED
```

| Module | Responsibility |
|---|---|
| `book_structure.py` | Extractors per format; generated titles; paragraph provenance |
| `extraction_qa.py` | Independent word count of the source, recorded drops, damage detection |
| `ocr.py` | Tesseract (Spanish) for image-only PDF pages, with per-word confidence |
| `spanish_text_normalizer.py` | Written → spoken Peninsular Spanish, with change log and ambiguity flags |
| `providers.py` | ElevenLabs TTS (seed, metadata, limits, `Retry-After`), Deepgram and Scribe ASR, voice profiles |
| `audio_integrity.py` | Stream and signal checks of each segment |
| `qa_review.py`, `spanish_normalization.py` | Comparison against spoken and written references; phonetic equivalence |
| `qa_metrics.py` | WER/CER with exact S/D/I counts |
| `repair.py` | Error-origin diagnosis, timing anomalies, the action for each case |
| `completeness.py` | The source-to-sync chain on the built package |
| `daisy_dtd.py` + `schemas/z3986-2005/` | Validation against the official DTDs |
| `release_gate.py` | Book state, the six reported dimensions, uncertainty |

## 2. Research and decisions

Sources were read on 2026-10-25. **Measured** means observed in this project
with the real services. **Documented** means stated by the provider and not
independently tested here.

### ElevenLabs

- **Models** (live `GET /v1/models`, measured): `eleven_v4`, `eleven_v4_turbo`,
  `eleven_v3`, `eleven_multilingual_v2`, `eleven_flash_v2_5` all support
  Spanish. Per-request limits are 10,000 characters for v4 and multilingual_v2,
  5,000 for v3 and 40,000 for flash v2.5. These limits are enforced before
  sending. Flash and turbo bill 0.5× per character
  ([models](https://elevenlabs.io/docs/models)).
- **eleven_v4** launched 2026-09-28. ElevenLabs recommends it for audiobooks
  and calls multilingual_v2 "most stable on long-form generations"
  ([v4](https://elevenlabs.io/v4), [models](https://elevenlabs.io/docs/models)).
- **Voice `o0SveC0zgHFuCsEO3vHR` (Gabo)** (measured): ElevenLabs lists it as
  **Peninsular `es-ES`, verified on `eleven_v4`, `eleven_v4_turbo`,
  `eleven_multilingual_v2` and `eleven_flash_v2_5`**. Soni2 now reads this at
  start-up from `/v1/voices/{id}` or `/v1/shared-voices`, and accepts the voice
  as Spain Spanish only when the configured model is on that list. The voice's
  own "verified languages" also contain community labels (for example
  `es-latin-american`, `es-andalusian`); only `peninsular` counts. The extra
  voice Lucía (`8HiNnu…`) is verified Peninsular on multilingual_v2 **but not
  on v4**.
- **Request parameters** ([convert](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)):
  - `seed` makes takes reproducible "on a best-effort basis". Soni2 sends a
    seed per take derived from the text and take number, so a retake explores
    a different reading.
  - `previous_text`/`next_text` give continuity. They are accepted by v4
    (measured).
  - `language_code` is ignored by multilingual_v2.
  - `apply_text_normalization` defaults to `auto`.
  - Pronunciation dictionaries take at most 3 locators, and phoneme tags work
    only on `eleven_flash_v2`; other models need alias rules
    ([controls](https://elevenlabs.io/docs/best-practices/prompting/controls)).
    Soni2's own lexicon (respellings applied to the narration copy) therefore
    remains the pronunciation mechanism.
- **Request stitching by request id** is not available on v3, and the ids
  expire after two hours
  ([guide](https://elevenlabs.io/docs/developer-guides/how-to-use-request-stitching)).
  It also forces paragraphs to be generated one after another. Soni2 keeps
  text context, which works in parallel and survives resume.
- **Errors** ([errors](https://elevenlabs.io/docs/eleven-api/resources/errors.md)):
  429 is `rate_limit_exceeded`, `concurrent_limit_exceeded` or `system_busy`,
  and is retried. 400/401/402/403/404 are permanent; an exhausted balance
  arrives as 401 `quota_exceeded`. Retries use exponential backoff with jitter
  and never wait less than `Retry-After`.
- **Account** (measured): Starter plan, 40,000 credits per month, concurrency
  limit 3. Billed `character-cost` was about 0.4 credits per character on this
  plan.

### Speech recognition

- **Deepgram nova-3** supports Spanish (`es`), with improved monolingual
  models released 2026-08-04
  ([changelog](https://developers.deepgram.com/changelog/2026/8/4),
  [announcement](https://deepgram.com/learn/deepgram-expands-nova-3-with-spanish-french-and-portuguese-support)).
  It returns word timings and confidences, now kept. It is the primary
  recognizer.
- **ElevenLabs Scribe v2** works with the project key (measured; it did not
  draw on TTS credits). It is the **second recognizer**, consulted only when
  the first one hears a difference
  ([Scribe Spanish](https://elevenlabs.io/speech-to-text/spanish)). It is a
  different system from Deepgram but comes from the same vendor as the voice;
  that partial dependence is accepted and documented.
- **Measured failure modes**, all now handled and covered by regression tests:
  - Deepgram writes spoken numbers as digits ("18 30 horas", "3 coma 50",
    "2º").
  - Scribe writes the symbols back ("3,50 €", "XVI").
  - Both spell sound-alike names differently ("Ximena" as "Jimena"; "Llanes"
    as "Yanes", which is yeísmo).
  - **Scribe can smooth over a real stumble.** It wrote "echó a andar" where
    Deepgram heard "echó antana en Navandar". Scribe's own word timings gave
    "andar" 1.3 s against about 0.45 s expected at this voice's pace. Soni2 now
    treats such a stretched word as evidence that a confirming recognizer
    cannot overrule.
  - Both recognizers are non-deterministic: the same audio gave different
    transcripts on different runs.

### Extraction and OCR

- DTBook, EPUB, DOCX and text PDFs are read structurally. Image-only PDF pages
  go to **Tesseract** (`tesseract-ocr-spa`), called with TSV output so each
  word carries a confidence. No new Python dependency is needed; Tesseract is
  installed in the Docker image. "Mixed" pages (large image, little text) are
  OCR'd only to *check* whether the image holds text the text layer lacks; that
  text is never spliced in silently.
- **DAISY 3 DTDs**: the original URLs on daisy.org now return 404. The official
  files were vendored from the DAISY Consortium's own repository
  ([urakawa-sdk](https://github.com/daisy/urakawa-sdk/tree/master/csharp/dtds/Resources)),
  unmodified; see `backend/app/schemas/z3986-2005/SOURCE.md`.

## 3. TTS model benchmark (measured, real services)

Setup: Gabo, seed 12345, the six passages in `backend/tests/corpus/benchmark_es.json`
(narrative, dialogue, names and places, numbers/dates/abbreviations, difficult
consonants, a long sentence). Each take was transcribed by both recognizers.
Full data: [`evidence/2026-10-25-tts-benchmark.json`](evidence/2026-10-25-tts-benchmark.json).

**Normalized input** (what production sends):

| Model | Deepgram verified | Scribe verified | Integrity flags | Real defects | Mean latency | Cost |
|---|---|---|---|---|---|---|
| eleven_multilingual_v2 | 5/6 (WER 1.2 %) | 5/6 (0.4 %) | 0 | 1 probable stumble ("a andar", stretched 1.3 s) | 3.0 s | 1.0× |
| **eleven_v4** | 5/6 (0.4 %) | **6/6 (0.0 %)** | 0 | none found (one Deepgram slip, "borgeaba") | 4.5 s | 1.0× |
| eleven_flash_v2_5 | 6/6 | 6/6 | 1 | a 2.9 s gap inside the dialogue | 1.2 s | 0.5× |

**Raw input** (the numbers passage with no Soni2 normalization):

- **eleven_v4** read it correctly by both recognizers: Spain date order, "tres
  euros con cincuenta céntimos", "kilómetros por hora", "siglo dieciséis",
  "Felipe segundo".
- **eleven_multilingual_v2** said an English "zero" in the date, garbled "s.
  XVI", and dropped "por hora".
- **eleven_flash_v2_5** produced "3 150 euros" and nonsense for "km/h", and
  skipped "pág.".

**Decision: `eleven_v4` with Gabo.** It is the only model with no detected
content error on normalized text from either recognizer, and it is robust even
on raw text, at the same price as multilingual_v2. Soni2's normalizer stays on:
it is required for multilingual_v2 and flash, harmless for v4, and it produces
the expected spoken text that review compares against.

**Limits of this evidence**: six passages and one take per model is a small
sample. v4 is four weeks old, with less long-form track record than
multilingual_v2. Pronunciation and prosody were not judged by a human
listener.

## 4. Spanish normalization

`spanish_text_normalizer.normalize_for_speech(text)` returns:
- the spoken form;
- every change, with its rule, original text, spoken text and source offsets;
- flags for anything it would not guess.

The book's text itself is never modified. Each unit stores `text` (the book),
`spoken_text` (normalized, compared against the recognizers) and
`synthesis_text` (spoken form plus lexicon respellings, sent to the voice).

Handled, with tests (`tests/test_spanish_text_normalizer.py`, 58 cases):
- **Dates and times**: DD/MM/YYYY (Spain order) and ISO dates; "1" is read
  "uno de" and not "primero de". Clock times: "seis y cuarto", "nueve en
  punto", "dieciocho treinta horas".
- **Money, percentages, units**: currency with gender and apocope ("un euro",
  "veintiún euros", "doscientas libras", "un millón de euros"); percentages;
  units (km/h, m², °C, t, ha…).
- **Numbers**: grouped thousands and decimal commas, agreeing with the next
  noun ("tres mil quinientas personas", "tres coma catorce").
- **Ordinals**: 1.º, 2.ª, 3.er, n.º.
- **Roman numerals**: after structural words, "siglo"/"s.", known regnal
  names ("Felipe segundo", "Alfonso trece"), and a lone section number ("II"
  is read "dos").
- **Abbreviations**: Sr., Dra., Ud., Avda., pág., etc., EE. UU., a. C., c/,
  keeping a sentence-initial capital.
- **Symbols** between what they join (&, +, =, ×).

Flagged and left as written:
- "D." (don, or an initial?)
- "3/4" (a fraction, or a date without a year?)
- long undelimited numbers (a postcode or a phone number?)
- "4 ha de…" (hectares, or "ha de" = must?)
- impossible dates
- ambiguous abbreviations (ed., a. m., cf.)
- e-mail addresses and URLs

Acronyms and names with non-Spanish spelling go onto a "watch" list for
listening. Plain integers stay digits, because spelling them out would force a
gender guess. The output is idempotent; this is tested.

The **comparison** side was fixed as well:
- spoken decimals followed by more words were never folded;
- `º` was missed after NFKD folding;
- feminine hundreds were unknown;
- "18 30 horas", "3 coma 50", "2 coma 5 por 100", "siglo XVI", "Felipe II" and
  "tres euros con cincuenta céntimos" were not equivalences;
- underscores counted as words.

Same-sound spellings are equal (ll/y, x/j, b/v, silent h, c/qu/k, g/j before
e/i), while Peninsular θ/s stays distinct ("caza" ≠ "casa").

## 5. Extraction (measured baseline → now)

On `tests/corpus/el-faro-de-cabo-gris.xml` (548 words), the extractor at
`eebfa0e` **silently lost 48 words (8.8 %)**: the whole list and the whole
poem. It also narrated invented titles. Now, every format:
- reads all text-bearing blocks in order;
- records every intentional drop with a reason (page numbers, note marks,
  running heads, navigation, transcriber markers);
- counts the source's words independently. Coverage on the corpus books is
  1.000.

| Problem at the start | Now |
|---|---|
| DTBook read only `<p>` (lists, poems, `dd`, tables, sub-headings lost) | Block walker over all of DTBook; notes read at chapter end; `pagenum` gives page provenance |
| EPUB `<header>` deleted before the title search | Title kept; `div`/`dd`/`td`/`pre` text kept; `<footer>` recorded and flagged; TOC titles used for headingless documents |
| DOCX tables ignored | Read row by row |
| Image-only PDF pages silently skipped | OCR'd with confidences; without OCR, the book is **BLOCKED** (`ocr_required`) |
| Running heads narrated in page mode | Repeated first/last lines removed and recorded; a line ending a sentence is never removed |
| "Página 3", "Capítulo 2", "Prólogo" narrated | `title_generated`: navigation only, never spoken |
| `[Ilustración]`, `_italic_` read aloud or counted | Removed and recorded |
| Unaccounted words | Warning with context: the book waits (NEEDS_REVIEW) until a named person acknowledges it |

## 6. Verification, repair and the release gate

For each take, the diagnosis decides:

| Situation | Origin | Action |
|---|---|---|
| Corrupt, silent or empty file; decode failure | audio | retake |
| Text recognized but audio cut off, too fast, or with a long gap | audio | retake |
| Recognizer 1 differs, recognizer 2 hears the text, no stretched word | asr | accept, listening list |
| Recognizer 2 confirms, but its timing shows a stretched word on the difference | synthesis (critical) | retake |
| Both hear the same real difference | synthesis | retake, then a person |
| Both write the same sound-alike name | asr | accept, listening list |
| Difference sits on text extraction doubted (OCR, broken word) | source | person, no retake |
| Difference sits on a normalized expression | normalization | one retake, then a person |
| Retake transcribed identically to the previous take | — | person (stop paying) |

Retakes are capped by `QA_MAX_RETAKES` (default 2). Every take is stored in
`production_attempts`, with:
- its audio file and seed;
- request id and characters billed;
- integrity result;
- both transcripts, issues and WER;
- diagnosis and outcome.

None of this is overwritten. The review queue shows, per segment: provenance,
expected spoken text, recognized text, issue offsets, origin, severity and the
recommended action.

The **release gate** reports six dimensions separately:
- technical generation
- source completeness
- automated content verification (WER and how each unit was verified)
- audio integrity
- DAISY package (structural check, DTDs, completeness chain; DAISY Pipeline
  `not_run`)
- human review

It also lists what remains unproven. `RELEASED` requires `PASSED` plus an
explicit release by a named person. The artifact download answers 409 for
anything else; `?diagnostic=true` serves the package under a `-SIN-VALIDAR`
name. Mock audio is always `BLOCKED`. The direct "download now" path carries a
`quality-status.json` that says `UNVERIFIED`.

## 7. DAISY package fixes found by the new checks

- The DTBook was written as `<ns0:dtbook><ns0:p>…`: every SMIL file's
  `register_namespace("")` stole the default namespace. It was invalid against
  the DTD and unreadable for DTD-based readers. Fixed; tested.
- Missing per the standard: the OPF manifest entry for the package file
  itself, `dtb:uid` in the DTBook head, `dtb:uid`/`dtb:totalElapsedTime` in
  each SMIL head, and `frontmatter/doctitle`. Also an OPF 2.0-only
  `spine/@toc` and an undeclared `xml:lang` on the resource file. All fixed;
  **all five document types now validate against the official DTDs**.
- **Sync drift in the Docker image**: passage lengths were measured with
  ffprobe, which only *estimates* VBR MP3s without a Xing header, so clip
  positions drifted (0.24 s in the existing test; that test failed in Docker
  at `eebfa0e` too). Lengths are now counted from MPEG frames.
- Loudness normalization returned 48 kHz audio (loudnorm works at 192 kHz),
  so a chapter whose levelling was kept differed in format from one where it
  was not. The output is now 44.1 kHz.
- `ELEVENLABS_STABILITY=""` from Compose stopped the server at start-up. Blank
  values now mean "unset".

## 8. Measured results

| Run | Units | Result | Retakes | Billed | Time | Audio |
|---|---|---|---|---|---|---|
| DTBook corpus, host, first run | 24 | 23 verified, 1 escalated | 2 | 1,442 | 71 s | — |
| same, after the phonetic fix (cached) | 24 | **PASSED** (24/24), ZIP re-validated | 0 | 0 | 43 s | 241 s |
| *Los ojos verdes* (15.5k chars), first run | 61 | 60 verified, 1 escalated (underscore bug) | 2 | 6,482 | 226 s | — |
| same, after the fixes | 61 | **PASSED** (61/61), ZIP re-validated, WER 0.26 % | 0 | 412 | 130 s | 1,096 s |
| DTBook corpus through the **Docker stack over HTTP** | 24 | **PASSED**, ZIP re-validated, all 6 chain checks | 0 | (new volume) | 81 s | 241 s |

Billed figures are ElevenLabs characters as reported per request. Total
account use for the whole session (benchmark, all runs and probes) was 11,991
credits.

Measured synchronization (`tools/measure_sync.py`: where each paragraph's
first word is actually heard versus its SMIL `clipBegin`):
- Docker-built package: median 34 ms, maximum 133 ms (23 of 24 clips located).
- Long-form package: median 70 ms, maximum 230 ms (53 of 61 located).

Unlocated clips are a limitation of the measurement, not sync failures: a
section number spoken "uno" is written "1" by the recognizer.

Escalations on first runs were all **false alarms that cost money** (a
sound-alike name, the italic underscores). Both were fixed at the root and
pinned by regression tests. No real narration error was found in v4
production audio by either recognizer.

## 8b. Where each fault is heard

Every difference a recognizer reports is located in time:

- **In the paragraph** while it is reviewed: the recognizer's word timings
  place the difference (an omission is placed on the gap where the words are
  missing). The review card says "se oye en el segundo 12,4 del párrafo" and
  plays from just before it.
- **In the finished audiobook** once it is assembled: the paragraph's measured
  SMIL clip and each chapter's measured start in the complete MP3 turn that
  time into a time in the chapter file and in the whole book (`h:mm:ss`).
  Stretched words are included. The times appear in the Export page ("Dónde se
  oyen las diferencias"), in the quality report (each time links to play the
  chapter file or the complete book from that point) and in
  `fault-locations.json` inside the ZIP.

Verified on *Los ojos verdes* (18 min): a 3-second window of the complete
audiobook MP3 was cut at each of the 8 reported times and transcribed again;
**8 of 8 contained the word in question** (`tools/verify_locations.py`,
[evidence](evidence/2026-10-25-fault-locations-becquer.json)). For example,
"trasgo" heard as "rasgo" is at 11:39.5 of the book.

Downloads are named after the book's title («El faro de Cabo Gris.zip»; a
plain-text book has no title of its own and uses its file name). A package that
did not pass the release gate downloads as «… (SIN VALIDAR).zip».

**Without ffmpeg** (for example `tools.e2e_book` run in a terminal where
ffmpeg is not on `PATH`), chapters are joined by copying MPEG frames only, so
the stream stays clean, but the audio cannot be analysed acoustically or
mastered: the book then stops at `NEEDS_REVIEW` and says why. Install ffmpeg
(`winget install Gyan.FFmpeg`) or use Docker for a releasable book.

## 9. Running it

```powershell
# Deploy / run (Docker installs ffmpeg and Tesseract with the Spanish model)
docker compose up -d --build            # app on http://localhost, API on 127.0.0.1:8000

# Backend tests, in the image (tests are mounted; the image copies only app/)
docker compose run --rm --no-deps -v D:/Amine/github/Soni2/backend/tests:/app/tests:ro backend python -m pytest -q -p no:cacheprovider /app/tests

# Backend tests on the host
cd backend; ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider

# Frontend
cd frontend; npm test; npm run build

# Real-service tools (they call paid APIs)
cd backend
..\.venv\Scripts\python.exe -m tools.benchmark_tts --models eleven_multilingual_v2,eleven_v4,eleven_flash_v2_5 --out ..\benchmark-results
..\.venv\Scripts\python.exe -m tools.benchmark_tts --rescore ..\benchmark-results\benchmark.json   # free
..\.venv\Scripts\python.exe -m tools.e2e_book tests\corpus\el-faro-de-cabo-gris.xml --out ..\e2e-results\faro
..\.venv\Scripts\python.exe -m tools.measure_sync "..\e2e-results\faro\unpacked\DAISY 3"
..\.venv\Scripts\python.exe -m tools.verify_locations ..\e2e-results\faro\unpacked   # checks fault times in the full MP3
```

New settings (`.env.example`):
- `ASR_SECONDARY_PROVIDER` (default `auto`)
- `QA_MAX_RETAKES` (default 2)
- `ELEVENLABS_MODEL_ID=eleven_v4`
- `ELEVENLABS_VOICE_ID=o0SveC0zgHFuCsEO3vHR`
- optional `ELEVENLABS_STABILITY`, `ELEVENLABS_SIMILARITY_BOOST`,
  `ELEVENLABS_STYLE`

API additions:
- `GET /api/jobs/{id}/release`
- `POST /api/jobs/{id}/acknowledge`
- `POST /api/jobs/{id}/release`
- `GET /api/jobs/{id}/attempts`
- `GET /api/jobs/{id}/artifacts?diagnostic=true`

## 10. What still needs a person, and what is not verified

- **Listening.** Recognizer agreement proves the words are recognizable, not
  that pronunciation, stress, intonation or the Peninsular accent are right.
  Each book lists the segments a person should hear first (those accepted
  with a recognizer's doubt or a stretched word). A sampled listening check of
  every book remains necessary before ONCE delivery, and it is not recorded by
  the system yet.
- **The official DAISY Pipeline validator and a real DAISY player** have not
  been run. The DTD check validates document types, not every Z39.86 rule.
- **OCR** was tested on rendered pages (Docker) and with mocked results, not
  on a real scanned book. Reading order of complex layouts (columns,
  sidebars) is not verified.
- **Thresholds** (speaking rate 6–30 chars/s, silences, a stretched word at
  2.5× and ≥1 s) were checked on about 100 real segments from one voice. Other
  voices may need recalibration.
- **Detection rates** (real-error recall, false alarms per hour) still need an
  independently annotated corpus. They are not measured, and the report says
  so.
- The model decision rests on six passages and two books. Re-run
  `tools.benchmark_tts` when the voice, model or plan changes.
