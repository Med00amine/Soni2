# DAISY Audiobook Pipeline

This repository contains the CLI-first core for converting DTBook XML into a
validated DAISY 3 package with deterministic test audio. The implementation is
intentionally modular so real TTS engines can be added without changing the
book model, synchronization layer, or DAISY builder.

## Architecture

```text
DTBook -> parser -> Book -> normalizer/segmenter -> TTSEngine
       -> WAV inspection -> synchronization -> DAISY builder -> validator
```

Phase 2 creates `content/book.xml`, sentence-level WAV files, chapter SMIL
files, `book.ncx`, `book.opf`, and `book.res`. The validator checks XML,
manifest resources, text/audio references, and clip ranges against measured
audio durations.

## Development

```powershell
D:/Program/python/python.exe -m pip install -e ".[dev]"
D:/Program/python/python.exe -m pytest
```

Parse the supplied example and generate mock WAV segments:

```powershell
D:/Program/python/python.exe -m app.cli convert `
  --input ejemplo_inicial/ejemplo.xml `
  --output data/output/example-daisy
```

Validate the generated package:

```powershell
D:/Program/python/python.exe -m app.cli validate `
  --input data/output/example-daisy
```

The generated structure is:

```text
example-daisy/
├── content/book.xml
├── audio/*.wav
├── smil/*.smil
├── book.ncx
├── book.opf
└── book.res
```

The package uses WAV as the Phase 2 lossless intermediate/final audio format.
It has been structurally validated by the project validator, but has not yet
been tested with every commercial DAISY player. MP3 encoding, richer DAISY
resource metadata, and external player certification remain future work.

## Production TTS

Phase 3 adds an optional [MeloTTS](https://github.com/myshell-ai/MeloTTS)
Spanish adapter. It is lazy-loaded, keeps one model per engine process, retries
failed sentences, records RTF metrics, and reuses valid cached WAV files.
Install it separately. On Windows/Python 3.12, follow the compatible
dependency steps in [docs/production-tts.md](docs/production-tts.md) rather
than relying on the upstream source-only pins:

```powershell
python -m pip install -e ".[production-tts]"
```

Use production TTS explicitly:

```powershell
D:/Program/python/python.exe -m app.cli convert `
  --input ejemplo_inicial/ejemplo.xml `
  --output data/output/example-daisy `
  --tts production
```

See [docs/production-tts.md](docs/production-tts.md) for model selection,
licensing, configuration, lifecycle, retries, caching, and deployment notes.

Spanish pronunciation preprocessing preserves reader-facing source text while
expanding common numbers, times, units, abbreviations, symbols, and configured
technical terms for TTS. See
[docs/spanish-text-normalization.md](docs/spanish-text-normalization.md), or
inspect source/TTS pairs with:

```powershell
python -m app.cli inspect-text `
  --input ejemplo_inicial/ejemplo.xml
```

## Web application

Phase 5 provides an accessible local demonstration built with FastAPI and
React/TypeScript. It accepts DTBook XML, generates the existing pipeline's
validated audiobook, exposes synchronized source text and audio, and offers a
DAISY download.

Start the backend:

```powershell
D:/Program/anaconda3/envs/machinelearning/python.exe -m uvicorn app.backend.main:app --reload
```

Start the frontend in another terminal:

```powershell
cd frontend
npm install
npm run dev
```

See [docs/web-application.md](docs/web-application.md) for the API, demo
workflow, and accessibility notes.

Generation jobs are persisted under `data/jobs` and run through a bounded
local worker queue. Set `DAISY_JOB_MAX_CONCURRENCY=1` (the default) to limit
CPU/memory-intensive MeloTTS jobs.

Supported uploads are DTBook XML, EPUB, HTML, and text-based PDF. All formats
are converted into the common `Book` model before entering the existing
normalization and audiobook pipeline. See
[docs/multi-format-ingestion.md](docs/multi-format-ingestion.md).
