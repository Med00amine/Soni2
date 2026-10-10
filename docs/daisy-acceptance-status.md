# Soni2 pipeline acceptance status

This document describes what this checkout actually does. A passing synthetic test is not ONCE acceptance.

## Working and tested locally

- Supported books are parsed into chapter and paragraph structure once. Preview and DAISY export use that same parser.
- Uploading a readable book returns extracted text even when TTS credentials are missing or the TTS service fails. Audio errors are returned separately from the text.
- The exporter creates a provisional DAISY 3 package with DTBook text, one SMIL file per chapter, paragraph audio references, NCX chapter navigation, metadata, and a ZIP download.
- Structural validation checks package files and references. Eight backend tests pass, using synthetic text and mocked audio. They do not test real narration audio or the ONCE acceptance profile.
- The frontend production build passes.

## Implemented but not verified with a real provider or playback device

- Deepgram, Azure, and ElevenLabs TTS adapters exist. ElevenLabs can return preview phrase timing. The private `.env` currently selects ElevenLabs but has no API key or voice configured, so narration, synced playback, voice selection, and Scribe review are unavailable in the running app.
- Scribe review compares preview transcription with source text. Differences are suggestions for a human, not confirmed errors.
- Edited chapter paragraphs from the book outline are sent into final text and audio generation. A corrected preview can be regenerated, but its replacement audio is not reused in the complete book package; the book is synthesized again.
- Chapter MP3 files are assembled from encoded paragraph audio. Tests check the bytes and references, not whether a DAISY player decodes and plays them correctly.

## Pending ONCE acceptance

- Obtain the official ONCE sample, exact legal and identification wording, and required output profile and filenames.
- Confirm DAISY 2.02 with `.ncc` or DAISY 3 with NCX. The current exporter emits NCX and does not create `.ncc`.
- Compare a real multi-chapter output against the official sample and run it through the DAISY Pipeline and a target playback device.
- Validate source headings and page references for representative EPUB, PDF, DOCX, and XML books. Scanned PDFs do not have OCR support. The web app can search extracted chapter paragraphs, but it has no page navigation controls. Search and page navigation have not been checked with a DAISY Pipeline playback device.

## Architecture work still needed

- Whole-book package generation is a single synchronous HTTP request. It has no durable job record, progress reporting, retry/resume, or cost-aware queue.
- Review and retake are preview-only. There is no segment review state or persisted approved audio for package generation.
- Until those gaps are addressed, treat this as a working local prototype, not a production audiobook pipeline.
