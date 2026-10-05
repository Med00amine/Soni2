# Accessible web application

The Phase 5 demonstration adds a local React/TypeScript frontend and a
FastAPI HTTP layer without replacing the existing CLI pipeline. Phase 6 adds a
durable local job repository and a bounded worker queue for long-running
generation.

```text
React + TypeScript
        ↓ HTTP/JSON
FastAPI (app.backend.api)
        ↓
Job repository → bounded in-process queue → worker
        ↓
DTBook parser → Spanish pronunciation → MeloTTS/MockTTS
        ↓
audio inspection → synchronization → DAISY builder → validator
```

## Local development

Install the Python project and development dependencies:

```powershell
D:/Program/anaconda3/envs/machinelearning/python.exe -m pip install -e ".[dev]"
```

Start the API from the repository root:

```powershell
D:/Program/anaconda3/envs/machinelearning/python.exe -m uvicorn app.backend.main:app --reload
```

Install and start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

The browser frontend uses `/api` by default. The Vite development server can
proxy that prefix to the API, or `VITE_API_URL` can be set to an absolute API
URL.

## Demonstration workflow

1. Upload a `.xml` or `.dtbook` DTBook file.
2. Review the parsed title, author, language, chapters, and sentence count.
3. Select a voice reported by `GET /api/voices`.
4. Start generation and monitor `GET /api/jobs/{job_id}`.
5. Open the generated synchronized source text and sentence audio.
6. Navigate chapters, control playback speed, and download the validated DAISY
   ZIP package.

Only DTBook XML is accepted. PDF and EPUB are intentionally not advertised
until parsers for those formats exist.

## API surface

- `POST /api/books` — upload and parse a DTBook.
- `GET /api/books/{book_id}` and `/chapters` — book metadata.
- `GET /api/voices` — available TTS voice identifiers.
- `POST /api/jobs` — queue generation without blocking the request.
- `GET /api/jobs/{job_id}` — status, stage, and sentence progress.
- `GET /api/audiobooks/{book_id}/text` — source text and synchronization data.
- `GET /api/audiobooks/{book_id}/audio/{filename}` — confined WAV access.
- `GET /api/audiobooks/{book_id}/daisy` — validated DAISY ZIP download.

The API never accepts a client filesystem path. Uploaded files and generated
packages are stored beneath `data/books/{book_id}` with generated identifiers.
User-facing errors omit tracebacks and internal paths.

## Job lifecycle

`POST /api/books/{book_id}/generate` creates a durable JSON record under
`data/jobs/{job_id}.json`, queues the ID, and returns immediately. A bounded
in-process worker claims the record exactly once and transitions it through:

```text
queued → running → completed
                 ↘ failed
```

Stages and sentence counters reflect actual work. `completed` is the number
of synthesized sentences and `total` is the parsed sentence count; progress is
reported as a normalized value from `0` to `1`. Failures are logged with the
job ID but the API exposes only a safe user-facing message. The result contains
relative resource references, never local filesystem paths.

The worker limit defaults to one MeloTTS job and is configurable with
`DAISY_JOB_MAX_CONCURRENCY`. The repository and queue are interfaces in
practice: they can later be replaced with a database and external queue
without changing the audiobook pipeline.

## Accessibility

The frontend uses semantic landmarks, labelled native controls, visible focus
states, a skip link, keyboard playback shortcuts, high-contrast and text-size
settings, reduced-motion support, and selective status announcements. It does
not claim WCAG certification. The synchronized display always uses the
reader-facing `source_text`, never the pronunciation-expanded TTS text.

## Known limitations

- Workers are in-process; durable job records remain on disk, but queued work
  is not automatically recovered after an ungraceful process termination yet.
- Filesystem storage is intended for a local demonstration, not multi-user
  production.
- Browser playback is sentence-segment based; a future service can add a
  durable job queue and object storage without changing the application
  service boundary.
