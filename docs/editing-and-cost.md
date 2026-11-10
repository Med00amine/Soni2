# Soni2 — natural pauses, editing in step 2, and paying only for what changed

## Natural pauses

**The cause of the "train" effect.** Measured on 50 real ElevenLabs paragraphs
(439 s, `eleven_v4`), the voice already pauses well *inside* a paragraph:
600–1160 ms at a full stop, 300–500 ms at a comma. The problem was the joins:
mastering trims each clip's silence and only put 350 ms back, so a paragraph
boundary was *shorter* than an ordinary full stop.

| Where | Setting | Default | How it is applied |
|---|---|---|---|
| After a paragraph | `AUDIO_PARAGRAPH_GAP_MS` | 1000 | Gap appended after the passage |
| Between parts of one long paragraph | `AUDIO_SENTENCE_PAUSE_MS` | 700 | Same gap, shorter |
| Between chapters (full-book MP3) | `AUDIO_CHAPTER_GAP_MS` | 2000 | Gap between chapter files |
| After `. ! ? …` inside a passage | `AUDIO_SENTENCE_PAUSE_MS` | 700 | Lengthened if shorter |
| After `; : — –` | `AUDIO_CLAUSE_PAUSE_MS` | 450 | Lengthened if shorter |
| After `,` | `AUDIO_COMMA_PAUSE_MS` | 300 | Lengthened if shorter |

`AUDIO_NATURAL_PAUSES=false` turns the pauses inside a passage off.

**How a pause is placed** (`audio_master.plan_pauses`). Each passage is decoded
once while ffmpeg's `silencedetect` reports its quiet stretches. The punctuation
of the text the voice read is expected at the point of the speech its position
suggests (letters and digits count, spaces and marks do not), and marks and
silences are aligned in order: a mark may have no pause (the voice read through
a comma) and a pause may have no mark (a breath).

The promises this keeps:

* **No provider call.** Only silence is added, in post-production.
* **No word is cut.** Silence goes only in the middle of silence already there,
  and quiet under 100 ms (a consonant closure) never counts as a pause.
* **No doubled pauses.** Only the missing difference is added; below 40 ms nothing.
* **Synchronisation stays measured.** Clips are measured after the pauses exist.
  On real audio speech starts a constant 51–58 ms after each clip begins, with
  no drift along a chapter (it was 36–268 ms before). Recognizer timings used
  to locate a difference are moved by the pauses before them.

Result on the same real audio: 107 of 108 marks matched a pause the voice had
made, and 3.2 s of silence (0.7%) was added.

Changing a pause setting changes the chapter fingerprints, so chapters are
mastered again from the audio already narrated. It costs CPU, not credits.

## Editing in step 2

Each phrase can be corrected, followed by a new phrase, or deleted; a chapter can
be deleted with its subchapters. Deleting asks first and shows what goes. The
initial ONCE files come from the planilla and cannot be deleted; their index and
the closing phrase are rebuilt from what is left.

**The edited text is the book** (`app/book_edits.py`). A change is attached to a
*slot*, a place that does not move when others change: `section-7:12` is paragraph
12 of narration-plan section 7, and `section-7:12+` is text added right after it.
A paragraph edited phrase by phrase is stored as the list of its narration units,
so an untouched phrase stays byte for byte what it was and keeps its paid audio.

**Saved on the server** (`book_drafts`, `/api/drafts`). The upload is stored once
and every change is saved as it is made, with a revision so a stale window cannot
overwrite newer edits. A reload finds the same book on the same step, and a
finished production of exactly that text comes back with its download. A job still
narrating when the page closes is still stopped, as before, so nothing spends
unattended. Drafts untouched for `DRAFT_RETENTION_DAYS` (30), or beyond the
`MAX_DRAFTS` (50) most recent, are removed; jobs are not affected.

**Production names the draft, not the text.** The page sends `draft_id` and the
voice, and first waits for pending saves. If they cannot be saved, nothing is
narrated. Requests that upload a file with `corrections_json` or `edits_json`
still work, for scripts.

### What used to lose edits

| Cause | Effect | Now |
|---|---|---|
| All state was in the page | A reload lost everything | Saved on the server |
| A preview replaced the chapter state | Edits made before it vanished | A preview reads the saved edits |
| Corrections were keyed by position in a flat list | Changing the selected sections moved them onto other paragraphs | Slots are stable, and an impossible one is refused |
| A running job matched on file, voice and speed only | An edited book got the job narrating the old text | The match includes the narrated text |
| Corrections could only replace a paragraph | No way to add or delete | Slots hold lists; empty deletes |
| The outline ignored corrections with manual sections | Screen and audiobook disagreed | One path applies edits for all |

## Paying only for what changed

| Action | ElevenLabs | Recognizers | Mastering |
|---|---|---|---|
| Change a pause setting | none | none | every chapter |
| Delete a phrase or paragraph | none | none | that chapter |
| Delete a chapter | none | none | none beyond the book join |
| Add or correct a phrase | that phrase | that phrase | that chapter |
| Move a phrase | none | none | that chapter |
| Produce again, unchanged | none | none | none |

* **Narration cache.** `sha256(provider, voice, language, model, speed, text)`,
  plus the ElevenLabs `voice_settings` only when they are set, so audio already
  paid for keeps its key. No index and no job id: a phrase that moves still hits.
* **Verdict reuse.** A verified verdict is stored beside the audio with the hash
  of that audio and of the expected speech. The same audio for the same text is
  not transcribed again. This saves Deepgram, and ElevenLabs credits for Scribe,
  the second recognizer. A reviewer's acceptance is remembered the same way.
  Free integrity checks still run on every take, and a retake still pays.
* **Chapter cache.** Mastered chapters are stored by what went into them (audio,
  text, mastering settings), so a chapter an edit did not touch is reused.
* **Hard links.** A job's audio files link to the cache where the bytes match, so
  producing a book again does not store its audio again.

Checked on the real 25 October book with any provider call failing the run: all
24 paragraphs came from the cache byte-identically, and deleting a paragraph and
a chapter cost 0 calls, mastered 1 chapter again and reused the other.

## Known limits

* A pause can only lengthen silence that exists. Where the voice read through a
  comma with no gap, none is added.
* Pauses need ffmpeg. Without it, or with `AUDIO_MASTERING=false`, the audio is
  joined as before and the job log says so.
* The direct download does not record pause positions, because it has no
  recognizer timings to move.
* A page-less book under the ONCE structure logs an index check failure on the
  direct download, whose index has no page numbers. This predates these changes.
