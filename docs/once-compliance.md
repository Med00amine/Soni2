# ONCE compliance upgrade (2026-11-10)

Branch `feature/once-compliance-upgrade`, from `9c97cbc`. Six commits, one per
phase. Nothing is pushed, merged or deployed.

Statuses: **PASS** implemented and verified · **PARTIAL** implemented, not
fully verified · **BLOCKED** needs information or infrastructure we do not
have · **DEFERRED** postponed by ONCE.

## 1. Checklist

| Req | What ONCE asked | Status | Where | Tests |
|---|---|---|---|---|
| Timing | SMIL timing valid against the real audio; validator without timing errors | **PASS** on Soni2's validator; **BLOCKED** on the official validator | `daisy_timing.py`, `daisy_export.smil_boundaries`, `audio_master.finish_file` | `test_daisy_timing.py` (28) |
| D | 1.0 s of silence before, more than 1 s after, every audio file | **PASS** | `audio_master.finish_file`, `join_book` | `test_boundary_silence.py` (9) |
| I | Every XML file indented | **PASS** | `daisy_export._write_xml`, `_indent` | `test_package_xml.py` |
| H | `<?xml-stylesheet href="dtbookbasic.css" type="text/css"?>` | **PASS** | `daisy_export`, `package_files/dtbookbasic.css` | `test_package_xml.py` |
| J | Fewer nested folders | **PARTIAL**: flat layout built and validated; not tested on a player | `PackagePaths`, `DAISY_PACKAGE_LAYOUT` | `test_package_xml.py` |
| G | "Texto en recuadro" / "Texto sombreado" … "Fin de …" | **PASS** | `book_structure` (detection), `once_structure`, `daisy_export` (sidebar), `once_validation._frames` | `test_boxed_shaded.py` (11) |
| C | Table of contents with page numbers (índice, sumario, contenido…) | **PASS** | `toc.py`, `once_structure._assign_pages` | `test_book_toc.py` (21) |
| A | "Nota" … "Fin de nota"; keep content and references | **PARTIAL**: framing for every format; references kept for DTBook and EPUB only | `once_structure._note`, `book_structure.note_ref`, `daisy_export._with_note_refs` | `test_notes.py` (6) and earlier note tests |
| B | Spell foreign words in headings, and foreign surnames in bibliographies and notes | **PARTIAL**: implemented; ONCE has not confirmed the format and no one has listened to the voice | `foreign_spelling.py`, `spanish_text_normalizer` contexts, `spanish_normalization._fold_spelled_letters` | `test_context_reading.py` (64) |
| F | Roman numerals, acronyms, abbreviations, numbers | **PARTIAL**: rules and dataset tested; pronunciation not checked by ear | `spanish_text_normalizer._context_rules` | `test_context_reading.py` |
| E | Unpaginated photo inserts at the end, numbered | **PARTIAL**: PDF and EPUB only; ONCE has not confirmed the wording or placement | `photo_inserts.py`, `once_structure._photo_section` | `test_photo_inserts.py` (12) |
| — | Descriptions of graphs, diagrams, figures and drawings; reading tables | **DEFERRED** | Nothing added; images keep only a numbered label as `alt` | — |

## 2. Timing: what was wrong and what changed

An audit of the delivered *Cuentos de la Selva* package found five faults.
Evidence is in `evidence/2026-11-10-timing-audit-cuentos-delivered.json`.

1. **Variable-bitrate MP3 with no length header.** Estimated from its first
   frame, a chapter is 18–28 % shorter than it really is: chapter 1 is
   56.7 s against a real 69.5 s. Every later `clipEnd` then appears to be
   beyond the end of the audio. Z39.86-2005 says no `clipEnd` may exceed the
   file's duration (§7.3), and requires players to support only constant
   bitrate (§5.1).
2. **648 gaps between clips.** The pauses sat outside the clips, so a SMIL
   player skipped them, and each SMIL file was shorter than its audio.
3. **`dtb:totalElapsedTime` was summed from file durations, not clips.** It
   was 189 s off by chapter 6.
4. **`dtb:totalTime` = 8144.7 s.** The SMIL files add up to 7477.6 s and the
   chapters to 8123.0 s; the complete MP3 added 2 s gaps.
5. **No silence at the file edges.**

Now:

* Every delivered file is constant bitrate (`AUDIO_MP3_BITRATE_KBPS`, 128).
  The loudness correction, the edge silence and the CBR encode happen in one
  encode, so there is no extra lossy generation.
* SMIL clips tile each file in whole milliseconds. The first starts at 0;
  each starts where the next passage's speech starts; the last ends at the
  file's end, rounded down.
* `totalElapsedTime`, the root `seq dur` and `totalTime` are all summed from
  those same clips.
* The complete audiobook is the chapter files copied frame for frame, so it
  is exactly as long as its parts.
* `daisy_timing.verify` reads the package back. It checks file and text
  existence, clock syntax, begin < end ≤ decoded duration, contiguity, all
  declared times, CBR and sample rate, edge silence measured on the
  waveform, and transitions. It is part of the completeness chain, so a
  failure blocks the release. `tools/audit_package.py` runs it on any
  package or ZIP.

**Result on real narration.** Almendra and Faro (ElevenLabs) were
re-produced from cache with blocked keys: 0 characters billed. All checks
pass and the release gate reports PASSED. Clip boundaries were measured on
the waveform: 19/19 and 21/21 fall in a pause followed by speech. Every file
starts with 1.020–1.025 s and ends with 1.208–1.229 s of digital silence
(RMS −inf). Evidence: `evidence/2026-11-10-final-audit-*-real-audio.json`.

**Still BLOCKED.** ONCE's own timing report is not available, so these are
the faults a validator would report, not confirmed to be the ones ONCE saw.
The official DAISY Pipeline / ZedVal was not run: it is not installed here.

## 3. What changes in the delivery

The package gains:

* `dtbookbasic.css`, and `images/photo-NNN.*` when a book has a photo insert;
* indented XML;
* `sidebar` elements for boxed and shaded text;
* `noteref` elements;
* a `navList class="photo"`;
* `seq dur` on every SMIL file.

`DAISY_PACKAGE_LAYOUT=flat` puts every file beside the OPF, with no folders.
The default stays `nested`.

Paid audio:

* Books with nothing new to announce re-use every recording; both real
  productions were checked at 24/24 and 27/27
  (`evidence/2026-11-10-cache-regression.json`).
* Every cached chapter is mastered again once, because the mastering
  settings changed. This costs CPU only.
* New narration is needed only for:
  * the announcements (once per voice);
  * "Fotografía N.";
  * spelled headings;
  * notes and bibliography entries whose citations are now read out;
  * an index title other than "Índice".

New settings (`.env.example`, `docker-compose.yml`):

* `AUDIO_LEADING_SILENCE_MS`, `AUDIO_TRAILING_SILENCE_MS`, `AUDIO_MP3_BITRATE_KBPS`;
* `DAISY_PACKAGE_LAYOUT`, `DAISY_DTBOOK_STYLESHEET`, `DAISY_STYLESHEET_MEDIA`;
* `SPEECH_CONTEXTS`, `SPELLING_FORMAT`, `SPELL_ALWAYS`, `SPELL_NEVER`.

New planilla fields:

* `boxed_open|close`, `shaded_open|close`;
* `index_title_from_book`;
* `photo_inserts`, `photo_insert_pages`, `photo_insert_documents`,
  `photo_section_title`, `photo_entry`, `photo_reference`.

There are no database or API changes; request and response shapes are
unchanged. The editor shows labels for the new roles.

## 4. Limitations

* **Note references**:
  * kept only where the source marks them (DTBook `noteref`, EPUB
    `noteref` links);
  * Word and PDF references are still dropped from the text;
  * a reference whose paragraph was edited is left out, never misplaced.
* **Boxed and shaded text**:
  * plain text has no formatting to detect;
  * a "box" covering about 90 % or more of a chapter is reported, not
    announced;
  * in PDF only rectangles that are drawn count; shading made of images
    does not.
* **Table of contents**:
  * pages come from the book's own table of contents only where the text
    has no page marker;
  * a page that begins inside a chapter without a marker stays unknown.
* **Photo inserts**:
  * DTBook uploads carry no image files, and Word inserts are not detected;
  * a candidate needs a planilla confirmation;
  * a caption is attached to a photo only when its page has one photo.
* **Foreign spelling**:
  * needs clear evidence; a foreign word written like Spanish
    ("Bianchi") is not detected unless the book marks its language or it
    is in `SPELL_ALWAYS`;
  * only "Surname, I." or "Surname (year)" forms count as surnames.
* **Not run**:
  * Docker (Linux image) tests: Docker Desktop was not running;
  * a real DAISY player (flat layout, legacy players);
  * listening to letter names, acronyms or announcements in the real voice.

## 5. To confirm with ONCE

1. Their timing report, and the validator and version they used.
2. **Spelling format**: currently `Shakespeare (ese, hache, a, ka…)`.
   Letter names follow RAE 2010: erre, uve doble, ye. Should the author line
   of file 2 (the book's own bibliographic data) also spell?
3. For a book whose contents are titled "Sumario": "Sumario." and
   "Fin de sumario.", or always "Índice."?
4. **Photo insert**:
   * the section title "Fotografías.";
   * "Fotografía N." for each photograph;
   * the sentence read where the insert was;
   * whether the photographs sit before the closing phrase (current) and
     whether the index lists them.
5. The style sheet line has no `media` attribute, exactly as ONCE wrote it;
   §12.2 of the standard asks for one. `DAISY_STYLESHEET_MEDIA` adds it.
6. Whether the flat layout works on their players. Once it does, set
   `DAISY_PACKAGE_LAYOUT=flat`.
7. Whether 1.2 s is the right closing silence; it is the suggested value.
   Between chapters of the complete MP3 the listener hears 2.2 s.
8. Whether boxed and shaded text should be skippable on a player. They are
   `render="required"` now: always read.

## 6. Producing and checking a book

* **Produce**: as before (`docker compose up`, the page, or
  `python -m tools.e2e_book <book> --out <folder> [--planilla MODELO.docx]`).
  New features apply when the book is produced with a planilla.
* **Audit a delivery**:
  `python -m tools.audit_package <ZIP or package folder> --alignment`.
* **Check that cached audio still matches**:
  `python -m tools.cache_regression --planilla "D:/Downloads/MODELO ONCE.docx"`.
* **Every feature at once, offline**:
  `python -m tools.e2e_once_features --out <folder>`
  (evidence: `evidence/2026-11-10-e2e-once-features.json`).

## 7. Rollback

* `git switch feature/pauses-step2-edits-tts-cache`: the code before this
  work, `9c97cbc`, still what EC2 runs.
* Or set `AUDIO_LEADING_SILENCE_MS=0` and `AUDIO_TRAILING_SILENCE_MS=0` and
  `SPEECH_CONTEXTS=off`, which removes the edge silence and the context
  rules. Constant bitrate, contiguous clips and SMIL times stay: they are
  the timing fix.
