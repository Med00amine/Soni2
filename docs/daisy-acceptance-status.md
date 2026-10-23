# Soni2 pipeline acceptance status

Updated 2026-10-20. This reflects the current implementation and the tests that
were actually run. A synthetic test does not establish ONCE acceptance or audio
quality. See [pipeline-and-output.md](pipeline-and-output.md) for how the
pipeline is wired and [audit-2026-10-20.md](audit-2026-10-20.md) for the state
it was in before this upgrade.

## Verified against a live provider

- **Deepgram narration and transcription work with the project key.** One short
  live check synthesized `aura-2-diana-es` (Spain Spanish), got a valid MP3 back,
  transcribed it with `nova-3`, received 13 word-level timings, and the Spanish
  comparison returned `verified` with confidence 1.00 and zero differences.
- **A 15-case numeric sweep was narrated and transcribed through the live
  provider.** It started at 7/15 verified and now reports **14/15**, with the
  one remaining flag being nova-3 returning the non-word "hecto passpans" for
  "hectopascales" — a genuine recognizer failure that correctly goes to a
  person. Two separate causes were found and fixed separately:
  - **The voice really does misread Spanish number formats.** `3.500` is
    narrated "tres punto cinco", `2,5` as "veinticinco", `1.200` as "uno punto
    dos". The same values spelled out are narrated correctly, so those two
    formats are now spelled out in the narration copy only. These had been
    reported as QA errors, and the QA was right.
  - **The comparison did not know the forms a real recognizer returns.** nova-3
    writes a correctly narrated `6:45` as "6 45", `12/03/2024` as "12 de marzo
    de 2024", `12%` as "12 por 100", `147` as "1 4 7", and `960 hPa` as
    "960 hectopascales". All of these were false positives and now match.
  - Three pre-existing comparison bugs surfaced while fixing this. A bare number
    of **four or more digits** was never canonicalized at all, so `3.500` could
    never match `3500`. The unit regex had drifted from the unit table, so
    `kilogramos` failed while `kg` worked; it is now generated from the table.
    And the tokenizer used a single forward-only watermark, so an expression
    late in a sentence blocked an earlier one from being recognized at all — a
    percentage at the end of a sentence stopped `18 km` being read as a
    measurement, which is what produced report lines showing the **same text on
    both sides**. It now tracks claimed character ranges, so pass order sets
    priority and position does not.
  - An article swap (la for una) was reported as a wrong quantity, because
    un and una are also the numeral one. A single short function word
    swapped for another is now ambiguous rather than a quantity error.
- A quality retake now forces a fresh synthesis rather than returning the cached
  audio, and stops as soon as a retake transcribes identically, so a systematic
  recognizer quirk cannot consume the retake budget.
- **Not verified live:** full-book reliability, cost at book scale, pronunciation
  quality judged by a listener, Deepgram quota limits, and any Fish Audio call —
  the Fish balance is empty, so its paths are covered only by mocks.

## Working and covered by offline tests

- **Narration and speech review are separate providers.** `TTS_PROVIDER` and
  `ASR_PROVIDER` are configured independently; `ASR_PROVIDER=auto` falls back to
  whichever account has credentials, so review survives one balance running out.
- **An offline `mock` provider runs the whole pipeline with no account and no
  spending**, and the UI and `/api/capabilities` state plainly that the audio is
  simulated.
- **Spain Spanish is the default and is enforced.** Each provider's default voice
  is es-ES, and a Latin American or unknown voice is rejected before any
  generation rather than quietly substituted.
- **Provider failures are classified and explained in Spanish** — exhausted
  credit, rejected key, unsupported voice, rate limit, oversized payload,
  timeout, network, server — with exhausted credit treated as permanent so
  retries are not wasted on it.
- **Narration audio is cached per sentence and voice.** Re-running an unchanged
  book makes no provider calls; correcting one sentence makes exactly one;
  changing the voice correctly invalidates; previews are cached; duplicate
  submissions join the running job; interrupted jobs resume from the cache.
- **Spanish QA distinguishes three outcomes** — correct after normalization,
  ambiguous (to a person, no further paid retake), and likely error. Wrong
  quantities are never ambiguous.
- **Narration starts at the narrative.** Contents pages, title and copyright
  pages and synopses are excluded by default — including PDF pages whose only
  label is `Página N` and plain-text front matter a parser labelled `Prólogo` —
  while prologues, introductions and epilogues are kept. Nothing is dropped
  silently: every exclusion carries a reason and is overridable.
- **`job.log` is readable timestamped text** with job, book, chapter and sentence
  scope, provider, attempt, cache reuse, QA verdict and confidence, retakes and
  validation outcome. `job-events.jsonl` keeps the structured stream beside it.
  Credentials are redacted if they ever reach a field.
- **The quality report is a self-contained HTML document** with a summary,
  filters, search, per-chapter sentence detail, expected versus recognized text,
  difference explanations, human decisions, cache reuse and the validation
  result. Unmeasured metrics are reported as unmeasured.
- **Output has one copy of each audio artifact**, and one corrected sentence
  rebuilds only its own chapter before the book and package are reassembled.
  Artifacts are written atomically.
- **Jobs are durable.** SQLite-backed state, stable sentence identifiers,
  recovery after restart, up to 1 + 5 technical attempts with 2/4/8/16/32 second
  backoff for transient faults only, up to 3 quality retakes, then a human
  review queue.

## Test results (2026-10-20, this machine)

| Suite | Result |
|---|---|
| Backend `pytest`, with ffmpeg | **172 passed** |
| Backend `pytest`, without ffmpeg | 170 passed, 2 skipped (the two ffmpeg tests) |
| Frontend `vitest` | **24 passed** (3 files) |
| Frontend `tsc -b && vite build` | **passed** |
| Live Deepgram TTS + ASR check | **passed**; numeric sweep **14/15 verified** |

Before this session the backend suite could not run at all: no virtualenv
existed and `fastapi` was not importable. The baseline, once an environment was
created, was 39 passed / 2 skipped.

Not run: Docker (Docker Desktop was not running on this machine), the official
DAISY Pipeline validator, and any browser test against a live provider.

## Implemented but not verified against ONCE devices or a real listener

- Validation is **Soni2's own structural check** — required files, `smilref`
  targets, NCX destinations, OPF manifest and spine, one final audio file per
  chapter directory, no empty MP3s. It is recorded in the job log, the report and
  `daisy-validation.json`, always with `official_daisy_pipeline: not_run`.
  Producing a ZIP is not evidence the package is correct.
- Playback and text/audio synchronization have not been checked in a DAISY
  player or on a target device.
- The ffmpeg duration, silence and clipping heuristics catch some defects. They
  are not a validated speech-quality or truncation benchmark. Under the `mock`
  provider they are reported as `skipped`, because mock audio is silent.
- If ffmpeg is absent, chapter and book audio are byte-concatenated instead of
  re-encoded. The job log and `daisy-validation.json` record which path ran
  (`audio_reencoded`), so a degraded build is visible rather than silent.
- The supervisor's metrics (99% real-error detection, ≤5 false alarms/hour, ≤10
  review minutes/hour, 90% automatic correction) are **not measured**; the report
  leaves them `null`.

## Pending ONCE acceptance

- Obtain the official ONCE example and confirm filenames, package profile,
  navigation requirements, and the required legal and identification wording.
  The introductory and bibliographic templates are optional and blank by
  default until that wording is confirmed.
- This exporter produces a provisional DAISY 3 package with NCX. It does **not**
  produce DAISY 2.02 `.ncc`. Confirm the target format before claiming
  compliance.
- Run the official DAISY Pipeline validator, then compare a complete real
  multi-chapter output against the ONCE sample on the target player.
- Verify headings, page navigation, search and synchronization with
  representative EPUB, PDF, DOCX and XML books. Scanned-PDF OCR is not
  implemented.
- Have a Spanish listener judge `aura-2-diana-es` (and the alternative es-ES
  voices) for pronunciation, prosody and suitability for a full book.
- Measure the proposed QA metrics against an independently annotated corpus
  before reporting any value for them.

## Runtime notes

The worker is an in-process FastAPI background task with SQLite persistence.
Run a single backend worker for now: the database prevents state loss on
restart but is not a distributed queue or lock.

Keep `JOB_STORAGE_DIR` (`/data`) on persistent storage. It holds the jobs, the
database **and the narration cache** — the cache is what prevents paying twice
for unchanged text.

## Reproduction

From the repository root in PowerShell:

```powershell
docker compose up --build
```

Open `http://localhost:5173`, upload a book, review the detected structure,
generate a preview, then export. Sentences the automatic review cannot confirm
appear in the production panel to accept, regenerate or correct.

Backend tests (tests must be mounted because the image copies only `app/`):

```powershell
docker compose run --rm -v D:/Amine/github/Soni2/backend/tests:/app/tests:ro backend python -m pytest -q -p no:cacheprovider /app/tests
```

Frontend tests and build:

```powershell
docker compose run --rm --no-deps --entrypoint npm frontend test
docker compose run --rm --no-deps frontend npm run build
```

Without Docker, from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd backend; ..\.venv\Scripts\python.exe -m pytest -q
```

To develop with no provider account and no spending, set `TTS_PROVIDER=mock`
and `ASR_PROVIDER=mock` in `.env`.
