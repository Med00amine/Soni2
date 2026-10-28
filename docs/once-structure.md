# The ONCE structure and the audiobook editor (2026-10-28)

Step 2, *Escuchar y revisar*, is now an audiobook editor modelled on how a DAISY
recording editor (PLEXTALK Recording Software) is used, and every book can be
produced with the structure ONCE asks for: three initial audio files, the
chapters and subchapters at their levels, silent page markers, framed notes
and a closing phrase. The structure comes from a **planilla** (production
sheet), which can be imported from a Word document written like the
supervisor's `MODELO ONCE.docx`.

The narration itself did not change: the same texts are sent to the same voice
with the same settings, so a book produced before is reproduced from the cache
byte for byte (see *Regression evidence* below).

## 1. Sources

| Source | What it settles |
|---|---|
| `MODELO ONCE.docx` (supervisor, October 2026), read programmatically | Files 1–3, the legal notice word for word, the bibliographic lines, the index form, page rules, the closing phrase |
| `PRODUCCIÓN ONCE VOZ SINTESIS.docx` (Sonidos S2, 15 Oct 2026) | ONCE structures books in Word with heading styles, page numbers and notes, saves them as DTBook with Save as DAISY, and wants one MP3 per chapter "or per highlighted section" |
| ANSI/NISO Z39.86-2005 ([text](https://dl.daisy.org/standards/z3986/2005/Z3986-2005.html)) and its official DTDs (`backend/app/schemas/z3986-2005`) | Skippable structures (§7.4.3), `par` content (§7.4.7), `playOrder` (§8.4.3), `smilCustomTest`/`bookStruct` (§8.4.4), page list (§8.3), `dtb:depth` |
| The supervisor's instructions as relayed | Initial files and chapters at level 1, subchapters at level 2; notes read "Nota 1." … "Fin de nota 1."; silent page markers |

## 2. The planilla

`backend/app/planilla.py`. Everything that changes from book to book is a
field; MODELO ONCE's rules are the defaults. The model's own invented values
(its title, author, publisher and dates) are **never** defaults: what a book
does not supply is left out and reported as missing.

| Field | Default | Used for |
|---|---|---|
| `absys` | empty (the model has none) | `dc:Identifier scheme="ABSYS"` in the OPF; not narrated |
| `title`, `author` | the book's metadata | "Título, De Autor" in files 1 and 2 and the closing phrase |
| `legal_text` | the model's text, word for word (961 characters) | file 1 |
| `bibliographic_heading` | `DATOS BIBLIOGRÁFICOS` | file 2 heading |
| `edition_line`, `publisher_line` | composed from the metadata | file 2 |
| `isbn` | empty: only a planilla that asks for it | file 2 |
| `production_line` + `production_date` | "Obra grabada en formato digital para el Servicio Bibliográfico de la ONCE, Sonidos S-2 Productions, S.L." + the month of production ("octubre de 2026") | file 2 |
| `file1_page`, `file2_page`, `index_page` | 1, none, none | page markers of files 1–3 |
| `index_heading`, `index_self_entry`, `index_entry`, `index_end` | `Índice.`, `Índice página {page}.`, `{heading}. Página {page}.`, `Fin de índice.` | file 3 |
| `index_depth` | 2 (chapters and subchapters) | 1 reproduces the model, whose index lists chapters only |
| `closing` | `Aquí finaliza la grabación de {title_and_author}.` | end of the last chapter |
| `note_open`, `note_close` | `Nota {n}.`, `Fin de nota {n}.` | notes |
| `chapter_pages` | — | heading → page, from a planilla's own index; used only where the book has no page numbers |
| `expected_files` | — | the files a planilla lists, compared with the book |

**Import.** `POST /api/planilla/parse` reads a `.docx` by the model's own
convention: black text is content, red text is instruction ("ARCHIVO 2 DE
AUDIO.", "EN ESTE CASO PONERLA EN LA PÁGINA 2"). Read from the real model, it
recovers the legal text identical to the default, pages 1/2/3, the edition and
publisher lines, the production date, the index pages, the files 4–8 and the
closing phrase as a template, and infers `index_depth: 1` because the model's
index omits its two subchapters. Anything it cannot identify is reported; the
editor shows the values for a person to confirm before anything is narrated.

**Sending it.** The page sends `planilla_json` with the book to
`/api/books/jobs`, `/api/books/package`, `/api/books/package-tasks` and
`/api/books/outline`. Only the values the reader set are sent; `{}` means "ONCE
structure with the model's rules"; an empty field means no ONCE structure,
exactly as before (every request without it behaves as it always did).

## 3. What a book becomes

`backend/app/once_structure.py`, shared by the production job, the direct
download, the editor's outline and the package validation, so they cannot
disagree.

| File | Content | Page |
|---|---|---|
| 1 | Heading "Título, De Autor", then the legal notice | `file1_page` (1) |
| 2 | "DATOS BIBLIOGRÁFICOS", "Título, De Autor", edition line, publisher line, [ISBN], production line and date | `file2_page` |
| 3 | "Índice.", ["Índice página N."], one entry per heading "Capítulo 1. La llegada. Página 4.", "Fin de índice." | `index_page` |
| 4… | Each chapter (level 1) and subchapter (level 2), in the book's order and its own segmentation | the book's own |

* **Subchapters** come from the book's headings: DTBook `level2`/`h2`, EPUB
  `h2`–`h6` one level below the chapter title, Word *Título 2*. A heading deeper
  than that stays in its subchapter's text. Nothing is guessed from wording.
* **Notes** come from explicit note markup only (DTBook `note`, EPUB
  footnotes/endnotes, PDF footnotes by layout, and now Word footnotes and
  endnotes). They are read where the pipeline already read them, at the end
  of their chapter, as "Nota 1. *text* Fin de nota 1.". A note already ending
  with its closing words is not given them twice, and a note that already
  says "Nota 1." is no longer prefixed again ("Nota. Nota 1." was read aloud).
* **The closing phrase** is appended to the last section once; if the book's
  last paragraph already is that phrase, it is marked instead of repeated.
* **The index** is generated from the sections as built, with the page each one
  starts on in the package, so it cannot disagree with the navigation. An entry
  with no known page says so ("Capítulo 2.") and the outline reports it.
* **Pages** come from the book: DTBook `pagenum`, EPUB page breaks, the PDF's
  pages, and Word's *Page Number DAISY* style (Save as DAISY), which is no
  longer read aloud. A page that begins inside a paragraph is marked at the
  start of that paragraph: synchronization is per paragraph.

## 4. The DAISY package

`build_daisy_package(..., layout=...)` writes the structure; without a layout
it writes exactly what it wrote before.

* Files 1–3 are `level1` sections in the DTBook **front matter** (classes
  `once-title`, `once-bibliographic`, `once-index`); chapters are `level1` in
  the body; subchapters are `level2` inside their chapter, with `h2`.
* One SMIL file and one MP3 per section (`audio/chapter-NNN/chapter-NNN.mp3`):
  ARCHIVO N of the planilla is `chapter-00N`.
* **Silent page markers**: `pagenum` with a SMIL `par` that has text and no
  audio, `customTest="pagenum"` (default: skipped), listed in the NCX
  `pageList` with type and value; `dtb:totalPageCount`/`dtb:maxPageNumber` match.
* **Notes**: `note` elements whose SMIL `seq` has `customTest="note"`
  (default: read), and a `navList class="note"` for jumping between them.
* The NCX declares both as `smilCustomTest` with `bookStruct` PAGE_NUMBER and
  NOTE, nests subchapters, and sets `dtb:depth`.
* **`playOrder` now follows the reading order**, one number per distinct SMIL
  time container (§8.4.3). Before, page targets were numbered after all the
  chapters even when they pointed at the same container (one existing test
  asserted that; it now asserts the rule).

## 5. Validation

`backend/app/once_validation.py` reads the built package back and checks it
against the planned structure; its checks join the completeness chain, so a
failure blocks the release like any broken link:

| Check | Fails when |
|---|---|
| `once_initial_files` | files 1–3 missing or out of order, the title line or legal text not word for word, file 2 without "Servicio Bibliográfico de la ONCE" and "Sonidos S-2 Productions, S.L.", file 1 not ending on its page |
| `once_hierarchy` | a section at the wrong level in the DTBook or the NCX, `dtb:depth` wrong |
| `once_pages` | a page marker with audio (not silent), without `customTest`, missing from or different in the NCX page list, wrong totals |
| `once_index` | an entry naming a section or page the package does not have, no "Fin de índice." |
| `once_notes` | a note without "Nota N." … "Fin de nota N.", or not skippable |
| `once_closing` | the closing phrase missing, repeated, different, or not the last thing read |
| `once_play_order` | `playOrder` not consecutive in reading order, or two numbers for one container |

The existing checks (structural, official DTDs, source → units → audio →
DTBook → SMIL → NCX) still run; page markers are excluded from the unit
matching because they are not narrated. The direct download records the same
checks in its `quality-status.json`.

Text that comes from the planilla (legal notice, bibliographic lines, index
entries, closing phrase, the initial files' headings) cannot be rewritten from
the review queue: the server answers 422 and says to correct the planilla and
produce again. Narrating it again or accepting it is still possible.

## 6. Step 2: the editor

`frontend/src/AudiobookEditor.tsx`, `audiobook-editor.css`.

* **Structure** (left): a keyboard-navigable tree (`role="tree"`: arrows,
  Home/End, Enter) with the initial files grouped, chapters with collapsible
  subchapters, and for each file its number, level, page range, duration and
  status. A search box finds any phrase in the book.
* **Phrases** (centre): the selected file's narration units in order, each with
  its number, start time, duration, role (heading, legal text, note, closing
  phrase…), status and differences; page marks sit between phrases where the
  pages begin. Before production a phrase's text can be corrected in place.
* **Inspector** (right): for a produced phrase, the comparison of the text with
  what the recognizer heard (differences marked), each difference with
  "Escuchar desde ahí", the diagnosis, the decisions (correct / narrate again /
  correct the text) and the pronunciation editor. Before production: the
  sample preview, the planilla summary and what is missing.
* **Transport** (top): play/pause, previous/next phrase and file, previous/next
  problem, a seek bar over the file, the time, the speed, and whether the
  audio is the final mastered file or the phrases' own audio. Shortcuts:
  Space, Alt+←/→ (phrase), Alt+↑/↓ (file), Alt+N (next problem).
* **Status bar** (bottom): the audio format (codec, rate, channels, bitrate),
  counts of files, subchapters, pages and notes, progress, the release state.

Playback follows the text **phrase by phrase**. Once a file is assembled it
plays the delivered, mastered MP3 and follows the measured SMIL clip of each
phrase; before that it plays each phrase's own audio in turn. The backend has
no per-word timing for production audio, so nothing pretends to highlight
words (the status bar says so).

Step 2 opens as soon as the book is read; the paid sample preview is optional.
Production can be started from Step 2 or Step 3 (same job).

API: `POST /api/books/outline`, `GET /api/jobs/{id}/structure`,
`GET /api/jobs/{id}/sections/{n}/audio`, `GET /api/planilla/defaults`,
`POST /api/planilla/parse`.

## 7. Confirmed and assumed

| Rule | Status |
|---|---|
| Files 1–3, their order and wording; legal text verbatim; closing phrase wording | **Confirmed** by MODELO ONCE.docx |
| File 1 ends on page 1; file 2 on page 2 and index on page 3 *in the model's case* | **Confirmed**; pages 2 and 3 are per book (planilla fields) |
| Chapters and initial files level 1, subchapters level 2; one MP3 per section | **Confirmed** (supervisor; production document) |
| Notes "Nota N." … "Fin de nota N." | **Confirmed** (supervisor) |
| Page markers silent: no audio, skippable | Instruction relayed by the user; DAISY form (text-only `par`, `customTest="pagenum"`) **chosen from the standard** |
| Page marker at the start of a section's heading | **Assumption**: the page begins where its first text is read |
| Index includes subchapters by default | **Assumption** from the instructions; the model's index lists only chapters (`index_depth: 1`, inferred on import) |
| Notes play by default (`defaultState="true"`) | **Assumption**: the framing words only make sense if they are heard |
| Initial files in the DTBook front matter | **Assumption**: they are preliminary matter; playback order is unchanged |
| The trailing `”` of the legal text, and "2026" in it | Kept verbatim; **to confirm with the supervisor** (likely a leftover quotation mark; the year may need to follow the production year) |
| ABSYS number as `dc:Identifier scheme="ABSYS"` | **Assumption**; the model only says it identifies the audiobook |
| ISBN | Only when a planilla supplies it |

## 8. Measured results

**Regression of the narration (no cost).** `el-faro-de-cabo-gris.xml` was
prepared by the new code and compared with the real production of 25 October
(ElevenLabs `eleven_v4`, voice Gabo):

* without a planilla: the same 24 units, with identical identifiers, texts and
  narration texts; all 24 narration-cache entries are present, and each is
  byte-identical to the audio that run delivered, so producing the book again
  costs nothing and sounds the same;
* with a planilla: 39 units, of which the 23 body units are reused unchanged;
  the 16 new ones are files 1–3, the closing phrase, and the note (now ending
  "Fin de nota 1.").

**A real ONCE book.** `el-faro-de-la-isla-almendra.xml` with the planilla read
from the real `MODELO ONCE.docx`, through the production job with the
providers in `.env` (ElevenLabs `eleven_v4`/Gabo, Deepgram nova-3, ElevenLabs
Scribe v2). Evidence: [`evidence/2026-10-28-e2e-once-almendra-run1.json`](evidence/2026-10-28-e2e-once-almendra-run1.json),
[`run2`](evidence/2026-10-28-e2e-once-almendra-run2.json),
[`sync`](evidence/2026-10-28-sync-once-almendra.json).

| Run | Units | Result | Retakes | Characters billed | Notes |
|---|---|---|---|---|---|
| 1 | 27 | NEEDS_REVIEW: 26 verified, the production line escalated | 2 | 1,201 | The voice read "S.L." as "Sociedad Limitada" and both recognizers agreed; Deepgram also writes the acronym ONCE as "11". The comparison treated both as errors. |
| 2 | 27 | **PASSED**: 27 verified, ZIP re-validated (DTDs, structure, 7/7 ONCE checks) | 0 | 0 (same narration, from the cache) | After the comparison fix below; WER 0.21 %; 217.6 s of audio in 8 files |

The fix is in the review comparison only (`spanish_normalization.py`,
`qa_review.py`), never in what is narrated: a spoken number no longer swallows
a following "y" ("ONCE) y ha sido" had become "11 hectares"); "S.L.", "S.A.",
"S.L.U." equal their spoken forms; and a letter joined to a number ("S-2",
"S2", "S 2") is one code, whose number still has to match. Without it, every
ONCE book would have paid two retakes and a manual review for the production
line. Regression tests pin the measured cases, and a wrong code or company
form is still an error.

**Synchronization** on the real ONCE package (`tools.measure_sync`): 26 of 27
clips located, median offset 20 ms, maximum 172 ms. The one not located,
"PRIMERA EDICIÓN…", is a limit of the measurement (the recognizer writes
ordinals as digits); its neighbours sit 11 ms and 75 ms from their speech.

**Listening.** Run 2 recommends listening to the legal notice's first part:
Deepgram heard "DACY" for "Daisy" and Scribe did not; how "Daisy" should be
said is for a person to judge.

## 9. Limitations

* Page markers fall on paragraph boundaries; a page that begins mid-paragraph
  is marked at that paragraph's start.
* A book without page numbers gets pages only from the planilla's index; the
  page of a section nobody gave a page to is the one in effect (reported).
* PDF page numbers are the PDF's own (as a viewer numbers them), which match
  the printed pages only when the scan starts at the book's first page.
* Headings deeper than one level below the chapter stay in the text; ONCE's
  instructions define two levels.
* The official DAISY Pipeline validator and a real DAISY player (GOLD,
  Thorium, EasyReader) have not been run on these packages yet.
