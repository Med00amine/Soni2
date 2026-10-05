# Accessible web application

The Phase 5 demonstration adds a local React/TypeScript frontend and a
FastAPI HTTP layer without replacing the existing CLI pipeline.

```text
React + TypeScript
        ↓ HTTP/JSON
FastAPI (app.backend.api)
        ↓
BackendApplication
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

## Accessibility

The frontend uses semantic landmarks, labelled native controls, visible focus
states, a skip link, keyboard playback shortcuts, high-contrast and text-size
settings, reduced-motion support, and selective status announcements. It does
not claim WCAG certification. The synchronized display always uses the
reader-facing `source_text`, never the pronunciation-expanded TTS text.

## Known limitations

- Jobs are in-process and are lost when the API process stops.
- Filesystem storage is intended for a local demonstration, not multi-user
  production.
- Browser playback is sentence-segment based; a future service can add a
  durable job queue and object storage without changing the application
  service boundary.
