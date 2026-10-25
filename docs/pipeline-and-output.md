# Soni2 — providers, cost control, output layout, logs and reports

> **2026-10-25:** the quality system — what is verified, how errors are
> diagnosed and repaired, the release gate and the measured results — is in
> [quality-system.md](quality-system.md). Changes to this page's statements:
> the default ElevenLabs model is now `eleven_v4` (chosen by benchmark);
> review compares recognizers against the expected *spoken* form and the
> written text; a second recognizer (ElevenLabs Scribe) adjudicates
> differences; every take is kept in `production_attempts`; the delivery ZIP
> adds `quality-status.json`, `extraction-report.json` and
> `completeness.json`; and `GET /api/jobs/{id}/artifacts` serves only a book
> the release gate passed.

Reference for how the pipeline is wired after the 2026-10-20 upgrade. The
acceptance status and what remains unverified live in
[daisy-acceptance-status.md](daisy-acceptance-status.md).

## 1. Providers: narration and speech review are separate

Text to speech generates audio. Speech to text transcribes that audio so it can
be compared with the source text. They are configured independently, because a
good transcriber is not a voice and either account can run out of credit alone.

| Setting | Values | Meaning |
|---|---|---|
| `TTS_PROVIDER` | `elevenlabs`, `deepgram`, `fishaudio`, `mock` | Who generates narration |
| `TTS_VOICE_ID` | provider voice id, or blank | Blank picks that provider's default **es-ES** voice |
| `REQUIRE_PENINSULAR_VOICE` | `true` / `false` | Refuse to narrate with a non-Spain voice |
| `ASR_PROVIDER` | `auto`, `deepgram`, `fishaudio`, `mock`, `none` | Who transcribes for quality review |
| `DEEPGRAM_API_KEY`, `FISH_AUDIO_API_KEY`, `ELEVENLABS_API_KEY` | secret | Credentials, read from `.env` only |
| `ELEVENLABS_VOICE_ID`, `ELEVENLABS_EXTRA_VOICE_IDS` | voice library ids | The ElevenLabs voices on offer |
| `ELEVENLABS_VOICE_NAME`, `ELEVENLABS_VOICE_LANGUAGE` | name, `es-ES`… | What the key cannot read; see below |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | The most stable model on long narration |

`ASR_PROVIDER=auto` uses whichever provider actually has credentials, preferring
Deepgram, so quality review keeps working when one balance is empty.

### Voices known to the application

Only voices the provider documents as Spain Spanish are marked `es-ES`, and
`resolve_narration_voice()` rejects anything else before a single character is
synthesized.

| Provider | Spain (es-ES) | Latin American |
|---|---|---|
| Deepgram Aura-2 | `aura-2-diana-es`, `aura-2-carina-es`, `aura-2-alvaro-es`, `aura-2-nestor-es` | `aura-2-celeste-es` (es-CO), `aura-2-sirio-es` (es-MX) |
| Fish Audio | `3f065d66…` (Carmen) | `fb9d5855…` (Isabela, es-CO), `ec071802…` (Rodrigo, es-MX) |
| `mock` | `mock-es-es-female` | `mock-es-mx-male` |

### ElevenLabs

ElevenLabs voices come from its voice library by id, so there is no fixed
catalog: the configured ids are the voices on offer. Each request sends the
neighbouring paragraphs as `previous_text` / `next_text` (request stitching),
so the voice carries its intonation across the joins. **ElevenLabs bills only
`text`**: measured, a 20-character request with 200 characters of context cost
`character-cost: 20`. `voice_settings` is sent only for a non-normal speed,
because it replaces the settings the voice was published with.

The key in use here has only the *Text to Speech* permission, so the API will
not reveal a library voice's name or accent (`missing_permissions` for
`voices_read`). The accent is therefore **unknown, not assumed**: the voice is
marked `accent_verified: false`, the page says so, and with
`REQUIRE_PENINSULAR_VOICE=true` it is refused until the operator declares
`ELEVENLABS_VOICE_LANGUAGE=es-ES`. Declaring it does not change the cache key,
so no paid audio is lost.

Every reply carries `maximum-concurrent-requests` (5 on the account in use,
ElevenLabs' Creator limit for this model). Narration starts at the free-plan
limit of 2 and switches to one below the reported limit as soon as the first
reply arrives; `_run_limited` re-reads the limit whenever a slot frees up.

ElevenLabs answers an exhausted balance with **401**, like a bad key, so the
error body's `status` decides the classification: `quota_exceeded` is a credit
problem, `missing_permissions` a key-permission problem, and
`too_many_concurrent_requests` / `system_busy` are retried with backoff. Its own
message is kept, because it states how many credits remain.

### Offline `mock` mode

`TTS_PROVIDER=mock` with `ASR_PROVIDER=mock` runs the entire pipeline — upload,
segmentation, narration, quality review, retakes, DAISY export, validation,
reports — with no account and no spending. The mock narration is a real
ID3-prefixed silent MP3 that carries its own text, so the mock recognizer
returns exactly what was "spoken" and the comparison path is genuinely
exercised. `/api/capabilities` reports `offline_mock_mode: true` and the browser
says so in plain Spanish, so simulated audio can never be mistaken for a
finished audiobook.

### Provider errors

Every failure becomes a `ProviderError` with a Spanish message and a retry
classification, surfaced to the browser with a matching status:

| Kind | Cause | Retried? | HTTP |
|---|---|---|---|
| `credentials` | 401 / 403 | no | 503 |
| `credit` | 402, balance exhausted | no | 503 |
| `rate_limit` | 429 | yes | 429 |
| `unsupported_voice` | unknown voice, wrong accent, 404 | no | 422 |
| `bad_audio` | 400 / 413 / 422, non-MP3 payload | no | 502 |
| `timeout` / `network` | connection problems | yes | 504 / 502 |
| `server` | 5xx | yes | 502 |

An exhausted balance is deliberately **not** retried: five attempts against an
empty account only waste time and make the real cause harder to see.

## 2. Cost control

Narration is content-addressed. The cache key covers everything that can change
the audio and nothing that cannot:

```
sha256(provider ‖ voice_id ‖ voice_language ‖ model ‖ speed ‖ synthesis_text)
```

Consequences, each covered by a test in `tests/test_offline_pipeline.py`:

- **Re-running an unchanged book costs nothing.** The key contains no job id, so
  entries are shared across jobs, previews and package builds.
- **Correcting one sentence costs one sentence.** Only that sentence's text
  changes, so only its key changes.
- **A voice change does invalidate audio**, because it is genuinely different
  audio.
- **Speed is excluded for providers without a speed control** (Deepgram), so it
  cannot fragment that cache.
- **Duplicate requests are coalesced.** Concurrent callers for one key wait on a
  single synthesis, and a second "generate" click on the same book, voice and
  speed returns the job already running (`reused_existing_job: true`) instead of
  starting a second paid run.
- **Previews are cached per paragraph**, with the same keys as the download, so
  repeating a preview is free and the download never pays for it again.
- **A restart stops unfinished jobs instead of resuming them.** The page starts
  empty, so nobody is watching such a job any more; resuming it unattended
  spent credit (once with a voice the user had since changed). Verified audio
  is kept, and `POST /api/jobs/{id}/resume` continues from the cache.

Attempt limits: up to **1 + 5** technical attempts per paragraph with 2/4/8/16/32
second backoff for transient faults only, and up to **2** quality retakes. A
cache hit consumes no attempt.

The cache lives in `JOB_STORAGE_DIR/audio-cache/`. **Keep it on persistent
storage** — it is what stops you paying twice. It is not evicted automatically;
delete it only when you intend to pay to regenerate.

## 2b. Numbers: the voice misreads some Spanish formats

Measured against `aura-2-diana-es`, the voice applies English conventions to
Spanish digit groups. These are **real narration defects**, not reporting
artifacts — the same quantities spelled out are narrated correctly:

| Written | Narrated as | Spelled out, narrated as |
|---|---|---|
| `3.500 euros` | "tres punto cinco euros" | correct (3500) |
| `2,5 metros` | "veinticinco metros" | correct (2,5) |
| `1.200 pesetas` | "uno punto dos pesetas" | correct (1200) |

So `app/spanish_speech.py` spells those two formats out **in the narration copy
only** (`synthesis_text`), before the pronunciation lexicon is applied so an
operator entry always wins. The source text is never modified, and quality
review still compares the audio against what the book actually says.

Only the formats that are demonstrably misread are rewritten — a Spanish
thousands separator and a decimal comma. Plain integers, times, dates,
percentages, ordinals and unit abbreviations are narrated correctly and are left
exactly as written.

A clock time, a date and a percentage are read correctly by the voice, so the
remaining differences there were **our** comparison failing to recognize the
transcript forms; see below.

## 3. Quality review and the Spanish comparison

The recognizer's transcript is compared with the reviewed text after Spanish
normalization, so spoken and written forms of the same value match. Each
sentence ends in one of three states:

| Verdict | Meaning | Action |
|---|---|---|
| `verified` | nothing differs after normalization | accepted |
| `ambiguous` | a small divergence recognizers get wrong on their own | **accepted automatically**, recorded |
| `likely_error` | a real divergence, including every wrong quantity and every stutter | retake, then human review |

What reaches a person is what a listener would notice. `auto_accept_reason()`
accepts, and records as *Aceptada automáticamente*, an `ambiguous` result, and a
long paragraph (12+ words) that matched at least 95% with no difference longer
than two words and no wrong quantity: narration engines fail by stuttering,
dropping or garbling phrases, while scattered one-word differences are the
recognizer's own slips. Never accepted automatically: a **suspected stutter** (a
word, or the broken start of one, said twice — "el el camino", "ca camino"), an
acoustic defect, a wrong number, a gap of three words or more.

Each paragraph is transcribed **in its own language**: `recognition_language()`
picks Deepgram `es`, `en`, or `multi` (code-switching) from distinctive words. A
bilingual reader's English translations were transcribed as Spanish and every
one of them reached the review queue; measured on a Spanish clip, `en` returned
"Martabriola Ventana Des Patio" where `es` and `multi` returned the sentence.

Treated as equivalent — spoken-word forms, and the **digit and mixed forms a
real recognizer actually returns**, all measured from Deepgram nova-3:

| In the book | Transcribed as | Why it matches |
|---|---|---|
| `147` | `ciento cuarenta y siete`, `1 4 7` | words, and digit-by-digit readback |
| `3.500` | `3500`, `tres mil quinientos` | plain runs of 4+ digits are compared by value |
| `2,5 metros` | `2 coma 5 metros`, `25` is **not** accepted | digits around the spoken separator |
| `6:45` | `6 45`, `seis cuarenta y cinco` | two plain numbers form a time |
| `12/03/2024` | `12 de marzo de 2024` | day and year left in digits |
| `12%` | `12 por 100`, `12 por cien`, `doce por ciento` | all three percent spellings |
| `960 hPa` | `960 hectopascales` | unit abbreviations spelled out |
| `18 km`, `9 m`, `4 ha`, `3 t` | `18 kilómetros`, `9 metros`, … | every unit in the table |

Accents, punctuation, case and short function words are also harmless. The unit
regex is generated from the unit table, so a unit listed there is always
recognized after a number — the two had drifted, which is why `kilogramos`
previously failed while `kg` worked.

Still errors: `147`/`ciento cuarenta y ocho`, `2,5`/`25`, `3.500`/`3400`,
`18 km`/`diecinueve kilómetros`, **`9 km`/`nueve metros`** (right number, wrong
unit), `6:45`/`6 55`, `12/03/2024`/`13 de marzo`, `3 20`/`trescientos veinte`,
missing or repeated content, truncated speech. A wrong quantity or unit is never
classified as ambiguous, because numeric tokens are compared by value **and**
unit.

Two adjacent numbers only fold into a time when the source itself contains one,
and the minute must be two digits — so "contó 3 20 cajas" is never read as 3:20.

### Retakes cannot loop on a recognizer quirk

A quality retake forces a **fresh** synthesis instead of returning the cached
audio, because handing back the reading that already failed review would spend a
transcription on a guaranteed repeat. (Until 2026-10-23 it did not: the check
compared the take number with a counter the loop had already advanced, so every
"retake" got the cached failed reading back, transcribed identically, and went
to a person. That alone filled much of the review queue.) If a genuinely fresh
reading transcribes to exactly the same text, retaking stops and the paragraph
goes to a person, so a systematic recognizer quirk cannot burn the retake budget.

## 4. Where narration starts

`narration_plan.py` classifies each section before anything is synthesized.
Excluded by default but always listed and overridable: contents pages, synopsis,
title and copyright pages, bibliography, glossary and appendices. Kept:
prologue, preface, introduction, epilogue, afterword, author's note and the
chapters themselves.

PDF ingestion is the hard case: it produces one pseudo-chapter per page titled
`Página N`, a label that says nothing, so the page's own text decides. A page is
a contents page when most of its short lines are "heading …… page number" rows,
and a copyright page when it carries ISBN / depósito legal / rights wording.
Plain-text ingestion labels everything before chapter one `Prólogo`; that
invented heading cannot turn a credits page into narrative, while a genuine
prologue is still narrated.

Nothing is dropped silently. Every exclusion carries a reason, uncertain
sections are narrated **and** flagged, and if policy would exclude everything,
the longest prose section is kept and flagged rather than producing an empty
book.

## 4a. Reading the book: what is narrated

**PDF, by layout.** `get_text("text")` gave one undivided run of lines per page,
so the running head and page number were read aloud on every page, words
hyphenated at a line end were read as two broken pieces ("distribui- dores",
which sounds like a stutter), and every sentence crossing a page was cut. The
reader now uses the layout: the body size is the most common font size; blocks
in the top margin set smaller (running heads) and digit-only blocks at the foot
(page numbers) are dropped; blocks 2.5 pt or more above the body are chapter
titles ("Capítulo 1" over its title reads "Capítulo 1. El arte de hablar en
imágenes"); small blocks low on the page that start with a number are
footnotes, read at the end of their chapter as "Nota 1. …", and the bare marker
glued to the word before it is removed from the sentence. Line-end hyphens are
undone when the next line starts in lower case, and a paragraph that does not
end its sentence continues into the next page. A PDF without larger titles keeps
the page-by-page reading.

**Page range.** `pages` ("20-114", as a PDF viewer numbers pages) limits a PDF
to those pages in every endpoint; the page has a *Páginas que se narran* field.
A range is part of the job's identity, so another range of the same file is not
treated as a duplicate job.

**EPUB.** Note references (`epub:type="noteref"`) are removed — one was read as
"uno" at the end of a sentence — and footnotes/endnotes are read at the end of
the chapter as "Nota 1. …". Sub-headings (`h2`–`h6`) are now narrated in place;
they used to be dropped. A paragraph inside a quotation is read once, and inline
markup no longer splits words.

**Units.** Unit abbreviations after a number are spelled out in the narration
copy ("9 m" → "9 metros", "960 hPa" → "960 hectopascales", "km/h", "°C"…):
ElevenLabs dropped the "m" of "9 m" on every take. "ha" is deliberately not
expanded ("1990 ha sido").

**Trailing additions.** Words heard after the last word of a paragraph are a
voice trailing off into a mumble ("… llegara una heno"), never accepted as a
minor slip.

## 4b. Two ways to export

The browser offers both, side by side, and neither forces the other:

| Action | Endpoint | What happens |
|---|---|---|
| **Descargar audiolibro ahora** | `POST /api/books/package-tasks` | Starts a background task and returns its id. The page polls `GET /api/books/package-tasks/{id}` (`done`, `total`, `reused`) for a progress bar and fetches `…/{id}/download` when it is ready; choosing another book or reloading calls `…/{id}/cancel`, so an abandoned download stops spending. No quality review, no retakes, no review queue. `POST /api/books/package` still does the same in a single request for scripts. |
| **Producir con revisión de calidad** | `POST /api/books/jobs` | The durable job: per-paragraph narration, transcription, Spanish comparison, retakes, a human review queue, logs and the quality report. The package is served from `GET /api/jobs/{id}/artifacts` once every paragraph is verified or accepted. |

The preview and both exports narrate **one request per paragraph** (split by
`make_paragraphs`, at most about 800 characters) with the same synthesis text,
so they share the narration cache: nothing one of them paid for is paid again.
Narrating sentence by sentence reset the voice's intonation at every full stop,
and every clip began with 0.45–1.25 s of provider silence, which played as a
stutter; it also cost more requests. All paths apply the same narration rules:
the number formats the voice misreads are spelled out, and the pronunciation
lexicon is applied.

**Several paragraphs are narrated at once** (`TTS_CONCURRENCY`; by default 8 for
Deepgram, whose Aura REST limit is 15 concurrent requests, and 4 for Fish Audio,
whose starter tier allows 5). The provider takes time in proportion to the audio
(the logs measured about 60 s per 1,800 characters), so one request at a time
put a 40,000-character book at about 30 minutes; at 8 it is about 5. Order is
kept whatever order requests finish in. A brief rate limit is waited out with
the same 2/4/8/16/32 s backoff; a permanent failure (no credit, bad key) cancels
every request still pending instead of paying for a book that cannot be built.
The ffmpeg and ZIP work runs in a worker thread so progress requests keep being
answered.

The export actions live in the Export step only. They were previously in a
footer rendered outside the tab panels, so they appeared on every step and the
listen-and-review step looked like a second export screen.

`GET /api/jobs/{id}/artifacts` answers **409** while any sentence still needs
review. The browser fetches it and shows that reason, and points at the direct
download as the alternative; it previously clicked a plain link, so the refusal
turned into a download that silently never happened.

## 4c. Mastering: why the book sounds like one reading

A provider returns every passage as an independent clip. Pasted together the
seams show: each clip carries a different amount of dead air, levels drift
between clips, and the joins click because an MP3 frame boundary is not a zero
crossing. `app/audio_master.py` fixes all three, and each one is measured.

| Step | What it does | Measured on real narration |
|---|---|---|
| Trim | Removes silence from both ends of every passage | 11.59s chapter → 10.08s; 1.5s of dead air gone |
| Pace | Puts a chosen pause back: `AUDIO_PARAGRAPH_GAP_MS` between paragraphs, `AUDIO_CHAPTER_GAP_MS` between chapters | paragraph clips end 4.90s and the next starts 5.25s — exactly 350 ms |
| Level | One two-pass EBU R128 correction per chapter to `AUDIO_TARGET_LUFS` | two chapters at −17.8 and −18.0 LUFS against a −18 target |

Levelling matters most for a listener who cannot see a volume slider: passages
normalised separately came back within **0.10 dB** of each other, where they had
started more than 3 dB apart.

Two passes, not one: a single streaming `loudnorm` adapts as it goes, which
makes a long narration breathe up and down. Measuring first and applying one
constant correction keeps the reading's own dynamics.

### Synchronisation is measured, never estimated

This is the part a DAISY player depends on, and the part mastering could most
easily have broken. Clip positions used to be the raw passage durations scaled
to the joined length — fine for a near-lossless join, wrong once each passage
is trimmed by a different amount and padded with a pause.

So every passage is trimmed and padded **on its own**, its real length is
measured, and the resulting `(start, end)` pairs are what the SMIL files carry.
The pause belongs to the gap after a passage rather than to its speech, so the
text stops being highlighted when the reading stops. A chapter's positions are
stored next to its audio (`chapter-NNN.clips.json`) and travel with it when the
chapter is reused, because recomputing them against mastered audio would drift.
If those stored positions are missing or unusable the chapter is rebuilt rather
than guessed at.

Verified end to end against the live provider: every clip inside its chapter,
in order, the last one ending at the chapter's end.

### When it does not run

Mastering needs ffmpeg. Without it, or with `AUDIO_MASTERING=false`, the audio
is joined as before and the job log and `daisy-validation.json` say so, so a
degraded build is visible rather than silent. A passage that is quiet all the
way through would be trimmed to nothing, so it is kept whole instead and only
its pause is added — a paragraph of the book is never dropped.

## 5. Output layout

### Delivery ZIP — `{job_id}-once-delivery.zip`

```
DAISY 3/
  <Book_Name>.xml                   DTBook full text
  <Book_Name>.ncx                   navigation (chapters + page targets)
  <Book_Name>.opf                   package manifest, spine and dtb:totalTime
  <Book_Name>.res                   resource labels
  <Book_Name>.mp3                   the complete audiobook, one copy
  audio/
    chapter-001/chapter-001.mp3     final audio for chapter 1
    chapter-002/chapter-002.mp3     final audio for chapter 2
  smil/
    chapter-0001.smil …             text/audio synchronization (clipBegin/clipEnd)
quality-report.html                 the readable report
quality-report.json                 the same data, machine readable
job.log                             readable execution history
tts.log                             per-sentence attempts and reuse
review-decisions.log                human review decisions
daisy-validation.json               validation outcome
reviewed-source.json                the reviewed text and source checksum
source/<original file>              the file that was uploaded
```

`<Book_Name>` is the title with accents folded and anything but letters,
digits, dots and hyphens turned into `_` (`Spanish Short Stories for Beginners
Book 1` → `Spanish_Short_Stories_for_Beginners_Book_1`): the names appear in
SMIL and OPF references, where spaces and non-ASCII break some players. The
direct download ("Descargar audiolibro ahora") has the same `DAISY 3/` folder.

**There is no per-sentence MP3.** Each SMIL `<audio>` points at its stretch of
the chapter file with `clipBegin`/`clipEnd`. The times come from counting MP3
frames in each sentence's audio, scaled to the joined chapter's real length so
re-encoding cannot make the last sentences drift. The quality report plays
each sentence the same way, through a `#t=start,end` media fragment.

`job-events.jsonl`, the machine-readable twin of `job.log`, stays in the job
folder for tooling and is not delivered.

**Joins are one clean MP3 stream.** Every provider file starts with an ID3 tag
and an *Info* frame that states that file's own length. Glued byte by byte —
what the preview and the direct download did — a chapter carried those headers
in the middle of its audio and told players it was as long as its first
paragraph, which breaks duration and seeking and makes some players stumble at
the joins. `join_mp3()` now goes through ffmpeg's concat demuxer: clips that
share one stream format (one voice always does) are **copied frame for frame**,
which is fast and lossless; mixed formats are re-encoded. Byte joining remains
only as the fallback where ffmpeg is missing (it is installed in Docker).

**Audio entries are stored, not deflated.** MP3 does not compress: deflating a
book-sized delivery measured **269s for a 2% size saving**, against **15s**
stored — a 17x difference, and the main reason the download felt slow. Text
entries (DTBook, SMIL, OPF, NCX, the report, the log) are still deflated, where
compression actually pays.

Every audio byte has exactly one home: once per chapter, plus the full book.

### Job working directory — `JOB_STORAGE_DIR/jobs/{job_id}/`

```
<original file>                     the upload
audio/chapter-NNN/segment-NNNN.mp3  per-sentence audio, for targeted retakes
audio/chapter-NNN/originals/        the audio a correction replaced
audio/chapter-NNN/retakes/          candidates awaiting human review
chapter-audio/chapter-NNN.mp3       joined chapter audio, for incremental rebuild
delivery/chapter-state.json         per-chapter fingerprints
delivery/*.json, *.log, *.html      the reports and logs, copied for serving
{job_id}-once-delivery.zip          the deliverable
```

The staged DAISY tree is deleted once the archive holds it, so the package is
not stored twice on disk.

### Incremental rebuild

Each chapter is fingerprinted from its title and its ordered sentence audio.
Correcting one sentence therefore:

1. regenerates that one sentence (one provider call),
2. re-joins **only** that chapter — the log shows
   `assemble.chapter: reused unchanged chapter audio` for the others,
3. reassembles the full audiobook from the chapter files,
4. rebuilds and revalidates the DAISY package.

Audio and reports are written with `os.replace` after writing a temporary file
in the same directory, and the ZIP is built under a staging name and renamed, so
an interrupted job leaves either the previous artifact or the complete new one.

## 6. `job.log`

Plain timestamped text, one line per event. The structured JSON Lines stream
sits beside it in `job-events.jsonl` rather than replacing it.

```
2026-10-20T08:55:12.089Z INFO    [job=b92f7715 book=875939bcd3eb] job.queue: job queued | source_name=libro.txt chapters=3 sentences=8 provider=mock voice=mock-es-es-female voice_language=es-ES already_cached_sentences=0
2026-10-20T08:55:12.126Z INFO    [job=b92f7715 ch=001 sentence=2d608bb2d3f6] tts.synthesize: sentence audio generated | provider=deepgram voice=aura-2-diana-es attempt=1 reused=no bytes=34848
2026-10-20T08:55:12.135Z INFO    [job=b92f7715 ch=001 sentence=2d608bb2d3f6] qa.compare: quality comparison completed | recognizer=deepgram-nova-3 verdict=verified confidence=1.000 issues=0 acoustic=passed
2026-10-20T08:55:12.400Z WARNING [job=b92f7715 ch=002 sentence=806cdb82aa43] qa.ambiguous: ambiguous difference routed to human review | confidence=0.750
2026-10-20T08:55:13.750Z INFO    [job=b92f7715 ch=001] assemble.chapter: reused unchanged chapter audio | fingerprint=0366172c1b11fae2 sentences=2
2026-10-20T08:55:13.974Z INFO    [job=b92f7715] daisy.validate: structural validation passed | method=soni2_structural_validation official_daisy_pipeline=not_run audio_reencoded=yes
2026-10-20T08:55:13.991Z INFO    [job=b92f7715] job.complete: delivery assembled | output=…-once-delivery.zip reused_chapters=1 rebuilt_chapters=2 cache={"hits":7,"misses":1,…}
```

Each line carries the timestamp, level, job, book, chapter and sentence scope,
the operation, a message, and the fields that matter: provider, attempt, whether
audio was reused, the QA verdict and confidence, retry and retake counts, and
the export and validation outcome. Operations are `job.queue`, `job.start`,
`tts.cache`, `tts.synthesize`, `tts.retry`, `tts.validate`, `qa.transcribe`,
`qa.compare`, `qa.retake`, `qa.ambiguous`, `qa.skip`, `sentence.finish`,
`assemble.chapter`, `assemble.start`, `assemble.ffmpeg`, `assemble.cleanup`,
`daisy.validate`, `job.partial`, `job.review`, `job.cancel`, `job.fail`,
`job.complete`.

Credentials are never passed to the logger, and anything resembling a bearer
token, API key or long hex secret that reaches a field is redacted.

## 7. Quality report

`quality-report.html` is one self-contained file — no external stylesheet,
script or font — so it opens from the ZIP with no network. It is responsive and
follows the reader's light/dark preference. Sections:

1. **Resumen** — six status tiles plus book, job, provider, voice, language,
   chapter and sentence counts, attempts, retakes and cache reuse.
2. **Frases** — every sentence grouped by chapter, with the reviewed text beside
   the recognized transcript, each difference's type, category and explanation,
   confidence, attempt and retake counts, whether the audio came from the cache,
   the acoustic check result, and a player for the sentence audio. Filter
   buttons and a text search narrow the list.
3. **Decisiones de revisión humana** — what a person accepted, retried or
   corrected, and when.
4. **Reutilización de audio** — cache hits, misses, coalesced requests, bytes
   reused, entries on disk.
5. **Validación del paquete DAISY** — the recorded outcome, including
   `official_daisy_pipeline: not_run`.
6. **Métricas propuestas** — listed as *no medido*, never invented.
7. **Registro de ejecución** — the tail of `job.log`.
8. **Análisis acústico** — what the ffmpeg heuristics do and do not establish.

### Reviewing a flagged sentence

Each difference carries character offsets into both texts
(`expected_start`/`expected_end`, `recognized_start`/`recognized_end`), so the
browser marks the exact words that differ instead of presenting two paragraphs
to compare by eye. An omission or an addition collapses to a single point, shown
as a caret where the words belong. Each queued paragraph offers its audio, the
marked comparison, the reason, and three decisions: the reading is correct,
narrate it again, or correct the text and narrate it. Accepting is disabled when
no audio was kept, because there would be nothing to accept.

*Narrate it again* and *correct the text* take effect **inside the request**:
`POST /api/jobs/{id}/review` narrates the paragraph, checks it, and answers with
the paragraph as it now stands, so the card shows the new text and plays the new
reading at once. (They used to queue the paragraph for the job, out of sight.)
A person's request is one reading with no automatic retakes; if it still
differs, it stays in the queue with the new audio. If nothing could be narrated
(no credit, a rejected key), the paragraph is restored exactly as it was and the
reason is returned. The audio URL carries the paragraph's `updated_at` and is
served `Cache-Control: no-store`, so a browser never replays an old reading. A
paragraph being narrated is in `units_in_progress`, and the job worker skips it.

Outcomes are distinguished rather than lumped together: *Correcta*, *Corregida y
revalidada*, *Aceptada automáticamente*, *Requiere revisión humana*, *Sin
resolver*, *Fallo técnico*, *Pendiente*. A numeric difference that normalization resolved never appears as
an error, and the report says so at the top. Book text is HTML-escaped.
