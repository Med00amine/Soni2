# Soni2 — Step 3, regenerating the ZIP after an edit, and checking it

> 2026-11-18. Step 3 (*Exportar*) has two actions, **Descargar ZIP** and
> **Regenerar ZIP**. Regenerating after an edit only redoes what the edit
> changed. Every ZIP is read back and audited before it is delivered. A reader
> can now **add a chapter**. Measurements and test results:
> [§6](#6-measured-before-and-after) and [§8](#8-tests).

## 1. Step 3 for the reader

Before, Step 3 was a 1040 px card with its own header. It held two radio
cards for choosing an export mode, a production card, a release panel, a table
of differences, a review queue with three buttons and a pronunciation editor
per paragraph, and a sticky dock with two or three more buttons. Some buttons
did the same thing twice (*Descargar ZIP DAISY*, *Descargar paquete revisado*,
*Descargar paquete de diagnóstico*), and *Actualizar estado* only repeated
what the page already polls. It read as a window inside the browser window.

Now Step 3 is one full-width workspace with flat sections separated by rules:

| Part | What it shows |
|---|---|
| Header | The book, one status line (*ZIP listo · 12 archivos · 2:15:22 · 236 MB · validado*, *Has cambiado el libro…*, *Generando…*), and the **only two primary actions** |
| Generación del ZIP | The real stages of the current or last build: what was reused, what was narrated, chapters reused or mastered again, and the time each stage took. The data comes from the server (§3), so nothing here is estimated |
| Párrafos que esperan tu decisión | Only when a paragraph needs a person. Each entry opens **Step 2** with that paragraph selected, where it is heard and decided (accept, narrate again, correct, pronunciation) |
| Contenido del ZIP | Audio files, duration, size, the result of reading the ZIP back, quality state and voice |
| Calidad | The release gate (confirming warnings, publishing) and where the differences are heard. After an edit it shows the previous ZIP's report, labelled as such, and offers no decision on it |
| Datos del libro · Opciones de entrega | Collapsed. Bibliographic data; *Revisión de calidad automática* (on by default; off gives the direct, unverified ZIP); length-based navigation sections for a book without chapters |

| Action | What it does |
|---|---|
| **Descargar ZIP** | Downloads the ZIP of the book *as it stands* if one exists. Otherwise it builds the ZIP and downloads it when ready. The server decides by content: the same text, voice and build settings return the same package |
| **Regenerar ZIP** | Builds a new ZIP from the latest text (`force=true`). Only what changed is narrated, checked and mastered (§4). The result is left ready for *Descargar ZIP* |

Both actions are disabled while a ZIP is being built, whichever action or step
started it. The server also answers a repeated request with the job already
running (§3), so a double click, a second tab or Step 2's *Producir* cannot
start a second paid run. Files are still streamed to disk under the book's
title, with *(SIN VALIDAR)* when the release gate has not passed. Failures are
explained under the actions and say what to do next.

Reviewing moved to Step 2, where editing already was. The Step 2 inspector
offers the same decisions the Step 3 queue offered, plus the player that plays
a new reading at once. Step 2 also gained **Añadir capítulo después** (§4,
scenario B).

## 2. How a book becomes a ZIP

```
draft (upload + edits, saved on the server)
  └─ prepare_book: narration plan → selected sections → edits by slot → ONCE structure
       └─ build_units: text, spoken text (normalized), synthesis text (lexicon)
            └─ job (SQLite: production_jobs / production_sentences / attempts / events)
                 ├─ units: reuse what is known (one transaction), narrate + check the rest
                 ├─ chapters: same job → reuse; same content anywhere → chapter cache; else master
                 ├─ package: DTBook, SMIL, NCX, OPF, resources, chapter MP3s, complete book MP3
                 ├─ validation: structure, DTD, completeness chain incl. timing against the audio
                 ├─ ZIP (audio stored, text deflated) → read back and audited (§5)
                 └─ the draft's earlier ZIPs are removed (the cache never is)
```

**Identities.** Every place in the book has a stable **slot**. `section-7:12`
is paragraph 12 of plan section 7, `section-7:12+` is text added after it, and
`added-1a2b3c4d:0` is paragraph 0 of a chapter the reader added. Edits are
stored by slot, so an edit never moves when others are made. Within a job,
sentence ids are hashes of position and text. Across productions, audio is
identified by **content**, never by position:

| What | Identity (all must match to reuse) |
|---|---|
| Narration audio | `sha256(provider, voice, language, model, speed, voice settings, synthesis text)` |
| Quality verdict | the audio's sha256 + the expected spoken text's sha256 |
| Integrity report | the audio's sha256 + spoken text + the analysis version (all thresholds) |
| Mastered chapter | the mastering signature + every unit's audio sha256, text and paragraph continuation |
| Finished package | source + every unit's text and synthesis text + chapters + metadata + voice + speed + the build signature (mastering, layout, timing targets, ffmpeg, analysis version, review on/off) |

A different voice, model, speed, voice setting, lexicon respelling or text
gives a different key, so nothing is reused (`test_another_voice_narrates_everything_again`).
One exception is deliberate and was there before: the neighbouring text sent
to ElevenLabs as context is *not* part of the key. Correcting a paragraph
therefore does not re-bill its neighbours, whose prosody at the join was
shaped by the old neighbour.

## 3. Progress the page can trust

`run_production_job` and the assembly record their stages in a `PipelineRun`.
Each stage writes a `perf.stage` line to `job.log` with its duration, and the
build ends with a `perf.summary` line and `pipeline.json`:

```
perf.stage: checking finished | stage=checking seconds=…
units.reuse: units taken over with the audio and verdict an earlier production left | reused=… checked=… total=…
perf.stage: narrating finished | …
perf.stage: chapters finished | …
perf.summary: build timings | outcome=completed total_seconds=… checking_seconds=… chapters={"total":…,"reused":…,"rebuilt":…}
```

`GET /api/jobs/{id}?view=live` is what the page polls. It returns the live
stage with real counts (`units: total, reused, to_narrate, narrated`,
`chapters: total, done, reused, rebuilt`) and the package facts. It leaves out
the event history and per-unit evidence the page does not use, and writes
nothing. The full view (no `view`) is unchanged for tools. The direct
download's task status reports the same stages.

## 4. What happens after an edit

| Scenario | Narrated (paid) | Checked by a recognizer | Mastered again | In the ZIP |
|---|---|---|---|---|
| **A. Remove a chapter** | nothing | nothing | the index file (it lists the chapters) | the chapter is gone from DTBook, SMIL, NCX, OPF and audio. The index no longer lists it; the closing phrase moves to the new last chapter |
| **B. Add a chapter** | its heading, its paragraphs, and its new index entry | the same | the new chapter and the index | in its place, with an index entry and navigation |
| **C. Remove a phrase** | nothing | nothing | that chapter | the phrase's text and audio are gone; the chapter's clips and durations are measured again |
| **D. Add or correct a phrase** | that phrase | that phrase | that chapter | the new text and its audio |
| Change a pause or mastering setting | nothing | nothing | every chapter | — |
| Nothing (Regenerar ZIP) | nothing | nothing | nothing | the same content, rebuilt |

**Removing.** The edit stores an empty slot (or, for an added chapter, removes
it from `added_sections` and re-anchors any chapter added after it). The next
build reads the units from the edited text. The removed units are simply not
units any more, so no SMIL clip, manifest item or chapter file refers to them,
and the ZIP audit (§5) proves no orphaned audio remains. Their audio stays in
the shared narration cache, which is never cleaned automatically: undoing the
removal costs nothing, and the cache is what stops paying twice. A newer ZIP of
the same draft removes the older jobs' ZIP files (the largest artifacts), but
not their records, reports or per-unit audio (hard links into the cache).

**Adding a chapter.** The `added_sections` edit is `{"id": "added-…", "after":
<section id>, "title", "paragraphs"}`. A chapter added after a section the
reader excluded keeps that place. The id never changes, so its paragraphs are
corrected, extended and deleted through slots like any other paragraph. The
text is normalized for speech like the rest, then narrated, checked and
mastered on the next build.

**Before, on every regeneration** (§6), each of the book's unchanged units was
processed again: its audio was decoded and analysed (integrity), the whole job
was reloaded from SQLite for each unit, about ten write transactions were made
per unit, and the quality report was re-rendered every 20 units. Then every
chapter and the complete book were decoded in full twice more, once to "finish"
files that were already finished and once to measure their edges for
validation. The direct download re-mastered every chapter every time. The
paid work (narration, recognition) was already incremental; the time went into
redoing free work.

**Now:**

* Unchanged units are taken over in **one transaction** (`_reuse_verified_units`)
  when their cached audio, verdict and integrity report all match. Anything
  that does not match goes through the normal path.
* Integrity reports are stored by content (`AudioCache.get_integrity`).
  Reports made without ffmpeg, or with a transient decoding failure, are not
  kept.
* Each unit is read on its own (`get_sentence`, `job_flags`), not as a reload
  of the whole job. The report is refreshed at most every 15 s.
* An unchanged chapter is not even read. Units and chapters travel as files
  (`AudioFiles`, chapter paths, the complete book written straight to its
  place). Peak memory went from about three times the book's audio to about one
  chapter.
* File edges are measured on 8 s windows at each end (`EDGE_WINDOW_SECONDS`).
  When a window is silent from end to end, the whole file is decoded instead,
  so the result is always the full decode's (`test_edges_measured_on_windows_match_a_full_decode`).
  Validation measures files where they are, without copying the complete book.
* The direct download uses the same chapter cache as a reviewed production.
* `initialize_store` runs once per process, not on every poll.

## 5. Reading the ZIP back

`app/package_integrity.py` opens the archive independently of the code that
wrote it. It runs on every reviewed and direct ZIP before the ZIP replaces
anything. An unreadable archive fails the build; any other failure blocks the
release (`completeness.json` gains a `zip_integrity` check). The CLI
`tools.audit_package` runs it too.

| Check | Fails when |
|---|---|
| `zip_readable` | the archive does not open, a CRC is wrong, an entry escapes the folder or appears twice |
| `zip_layout` | there is not exactly one OPF in `DAISY 3/`, or it does not declare the NCX, DTBook, resources and complete book |
| `zip_xml` | an XML document is malformed |
| `zip_manifest` | the OPF lists a file the ZIP lacks, or the package holds a file the OPF does not list (stray or obsolete) |
| `zip_references` | a SMIL `text`/`audio`, a DTBook `smilref` or an NCX `content` leads nowhere |
| `zip_audio` | an MP3 is not MPEG audio, or no SMIL clip plays it and it is not the complete book (orphaned audio) |
| `zip_order` | spine, navigation and chapter files disagree on the reading order |
| `zip_metadata` | title or `dtb:uid` differ between OPF, DTBook, NCX and SMIL |
| `zip_chapters` | the navigation's sections differ from the book's: a removed chapter lingers or an added one is missing |

It found one real defect at once. The direct download put `quality-status.json`
*inside* the DAISY folder, where the OPF does not list it. It now sits at the
ZIP root, as in a reviewed delivery. The official DAISY Pipeline validator is
still not run (`official_daisy_pipeline: not_run`). The checks above, the
official Z39.86-2005 DTDs and the timing checks against the real audio are
what is run.

## 6. Measured before and after

`tools/benchmark_regeneration.py --chapters 12 --paragraphs 45`, run on
2026-11-18 on the same 12-core Windows desktop before and after the change.
The reports are `docs/evidence/2026-11-18-regeneration-benchmark-before.json`
and `…-after.json`.
The book is synthetic: 2.5 hours, 575 units, 15 files. The audio is real MP3
(shaped noise as long as reading the text takes). No paid service was called.
Every ZIP passed CRC, structure, the DTDs and timing in both runs. The ZIP
audit (§5) exists only in the second run, and it passed there too.

| Scenario | Before | After | Narrated / recognized | Chapters |
|---|---|---|---|---|
| First production (seed) | 544.6 s | 442.5 s | 574 / 574 | 15 mastered |
| *Descargar ZIP*, text unchanged | 155.7 s (rebuilt) | 1.4 s (the same ZIP) | 0 / 0 | — |
| *Regenerar ZIP*, text unchanged (`force`) | — | 25.9 s | 0 / 0 | 15 shared |
| A. Remove a chapter | 144.3 s | 20.3 s | 0 / 0 | 13 shared, 1 mastered (index) |
| D. Correct a phrase | 171.4 s | 45.6 s | 1 / 1 | 13 shared, 1 mastered |
| D. Add a phrase | 169.3 s | 47.4 s | 1 / 1 | 13 shared, 1 mastered |
| C. Remove a phrase | 172.2 s | 45.1 s | 0 / 0 | 13 shared, 1 mastered |
| Direct download (no review) | 403.9 s | 12.9 s | 0 / — | all from the chapter cache |
| Page polling: median / largest answer | 89 ms / 1.9 MB | 38 ms / 0.70 MB | | |

**Where the time went before.** The ZIP itself was never the cost: writing
and checking it takes under a second. Removing a chapter took 72 s of wall
time going through the 528 unchanged units again. That is 235 s of per-unit
work, 5 units at a time, including 53 s of integrity analysis. Assembly took
70 s more, of which 43 s was the completeness chain decoding every file in
full. The direct download re-mastered all 14 chapters (340 s).

**Where it goes now.** Remove a chapter, 18.9 s in total:

| Stage | Seconds |
|---|---|
| checking | 0.9 |
| narrating (nothing to narrate) | 0.1 |
| chapters (the index) | 2.3 |
| package | 5.9 |
| validating | 8.4 |
| zipping | 0.8 |
| verifying | 0.4 |

Correct a phrase, 44.2 s, of which 27.3 s is the chapters stage: mastering the
one changed chapter. That is trimming and pausing each of its passages (about
9 s), then the two-pass EBU R128 correction and the constant-bitrate encode
(about 18 s). That work is real. The chapter's loudness depends on every
passage in it, so it is measured and corrected as a whole. Caching each
trimmed passage would save the first 9 s, but would store a second copy of
the book's audio. It was not done.

The first regeneration after a production spent 7.2 s instead of 0.9 s in
*checking*. This is Windows reading files for the first time after they were
written: in a separate probe on the same machine, 1,725 fresh files were read
in 8.0 s the first time and 0.23 s the second. Antivirus scanning on first
access is the likely cause.
EC2 runs Linux, so that cost should not occur there.

## 7. Known limits

* The Windows desktop measurements are not EC2 figures. The t3.micro has two
  burstable vCPUs and 913 MB of RAM, so absolute times there are several times
  longer. The work removed is CPU and memory, which is exactly what is scarce
  there.
* The first regeneration after deploying still analyses every unit once.
  Verdicts made before this change carry no integrity report; the report is
  stored then, and later regenerations reuse it.
* Removing older ZIPs applies to jobs of the same draft only. Jobs from before
  drafts existed, and ZIPs of abandoned drafts, are not touched.
* The ElevenLabs context (neighbouring text) is not part of the cache key; see §2.
* HTML is not an import format (the picker and the server accept PDF, EPUB,
  DOCX, TXT and XML).

## 8. Tests

* Backend: `tests/test_regeneration.py`: scenarios A–D, adding a chapter
  (placement, editing, removal, validation of the edit), reuse and
  `force`, duplicate requests, a job waiting for a decision, voice change,
  integrity-report reuse and invalidation, the live status and pipeline
  record, the ZIP audit on broken archives, windowed edges against a full
  decode, and the direct download's reuse, chapter cache and layout.
* Frontend: `App.download.test.tsx` (Step 3 actions and states, streaming,
  names, progress, errors), `App.review.test.tsx` (decisions in Step 2 from
  Step 3's list), `exportView.test.ts` (stages, changes since the ZIP),
  `App.release.test.tsx` (a previous ZIP's report offers no decision),
  `App.editor.test.tsx` (playback stops when the text is opened; pausing is
  immediate) and `App.edits.test.tsx` (an undo racing a save reaches the
  server; a reload says it is restoring).
* Browser: `tools/e2e_ui.py` drives the running application in Microsoft
  Edge through the whole journey and every import format, against
  `tools/e2e_server.py` (real MP3 audio, no paid service). It audits every
  downloaded ZIP and writes `results.md`, `inventory.json` and screenshots.

## 9. What the browser run found

The first full run in Edge stopped at step 13 of the journey. These were fixed
and the run repeated until every check passed:

* **Editing after listening.** Clicking *Editar el texto* while the book
  played left the audio playing behind the text. The play button is disabled
  there, so it could not be stopped. Every click in the structure tried to play
  instead of opening the chapter, and the audio's clock pulled the selection
  back to the chapter it was playing, so corrections landed in that chapter.
  Now going between the produced audio and the text stops playback, a pause
  takes effect at once (a shortcut pressed right after it no longer restarts
  the audio), and a section with nothing to play still opens.
* **"Falta ffmpeg" during a build.** The release gate counted paragraphs not
  narrated yet as audio it could not analyse, and said ffmpeg was missing.
  Only audio that exists is counted now; the rest is reported as not narrated
  yet.
* **Publishing an outdated ZIP.** After an edit, Step 3 still offered
  *Publicar el audiolibro* for the previous ZIP. The report is now labelled as
  the previous ZIP's and offers no decision.
* **Phone width.** The collapsed sections' markers sat at the screen's edge,
  and the voice's language split at its hyphen.
* **An undo racing a save.** An undo made while the change it undid was
  still being saved matched what the page had last saved, so the page
  thought nothing was pending. The change then reached the server, and
  nothing saved the undo. The page and the server disagreed, the status read
  *Guardando…* indefinitely, and a reload lost the undo. Now a save that
  finds the page has moved on saves again.
* **"No ZIP" during a reload.** While a reload restored the book, Step 3
  said there was no ZIP. It now says it is recovering the book and its last
  ZIP, and both actions wait until it is done.

The last run of `tools/e2e_ui.py` passed all 55 checks
(`docs/evidence/2026-11-18-e2e-step3-journey.json`). The page inventory
(`…-e2e-step3-inventory.json`) covers 8 states and 102 distinct controls,
with exactly two primary actions (*Descargar ZIP*, *Regenerar ZIP*) in each
of Step 3's three states.
