# Project: Accessible AI Audiobook → DAISY 3 Pipeline

You are acting as a senior Python/ML engineer and software architect.

We are building a production-oriented system that converts structured books into accessible DAISY 3 digital talking books, with high-quality Spanish neural TTS.

The project is intended for an accessibility/humanitarian initiative that may eventually be used in collaboration with organizations/government institutions in Spain to improve access to books for blind and visually impaired users.

## IMPORTANT DEVELOPMENT RULE

Do NOT build the frontend/UI yet.

We first need a reliable CLI-based core pipeline.

The first end-to-end milestone is:

DTBook XML
→ parse book
→ normalize text
→ segment text
→ generate TTS audio
→ calculate audio durations
→ create synchronization metadata
→ generate DAISY 3
→ validate output

Only after this pipeline works reliably should we build FastAPI and then the accessible web UI.

---

# 1. Main objective

Build a modular Python application capable of converting:

- DTBook XML
- eventually EPUB 3
- eventually other structured document formats

into:

- synchronized text
- generated speech
- DAISY 3 output

The architecture MUST allow the TTS engine to be replaced without modifying the rest of the pipeline.

Potential TTS engines include:

- XTTS-v2
- F5-TTS
- CosyVoice
- MeloTTS
- external APIs such as Fish Audio

Do not hard-code the project around one TTS model.

---

# 2. Target architecture

Use this architecture:

Input
↓
Ingestion
↓
Internal Book Representation
↓
Text Processing
↓
TTS Engine
↓
Audio Processing
↓
Synchronization
↓
DAISY Builder
↓
Validator
↓
Output

Detailed structure:

book.xml
    ↓
BookParser
    ↓
Book
    ├── Metadata
    ├── Chapters
    ├── Sections
    ├── Paragraphs
    └── Sentences
             ↓
       TextNormalizer
             ↓
       SentenceSegmenter
             ↓
          TTSEngine
             ↓
        AudioSegment
             ↓
      Synchronization
             ↓
       DAISYBuilder
             ↓
        DAISYValidator
             ↓
        output/book/

---

# 3. Technology stack

Use:

Python 3.11 or 3.12

Core:

- Python
- PyTorch
- lxml
- pydantic
- pydantic-settings
- pytest
- pytest-cov
- structlog or Python logging
- FFmpeg
- torchaudio where appropriate

Later:

- FastAPI
- Redis
- Celery or another reliable task queue
- PostgreSQL
- S3-compatible object storage
- Docker
- GitHub Actions

Do not introduce unnecessary dependencies.

Prefer mature, well-maintained libraries.

---

# 4. Repository structure

Create:

daisy-audiobook/
│
├── app/
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── dtbook.py
│   │   ├── epub.py
│   │   └── models.py
│   │
│   ├── text/
│   │   ├── __init__.py
│   │   ├── normalizer.py
│   │   ├── segmenter.py
│   │   └── lexicon.py
│   │
│   ├── tts/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── mock.py
│   │   └── engines/
│   │       ├── __init__.py
│   │       ├── xtts.py
│   │       ├── f5tts.py
│   │       └── cosyvoice.py
│   │
│   ├── audio/
│   │   ├── __init__.py
│   │   ├── processor.py
│   │   ├── encoder.py
│   │   └── quality.py
│   │
│   ├── synchronization/
│   │   ├── __init__.py
│   │   └── synchronizer.py
│   │
│   ├── daisy/
│   │   ├── __init__.py
│   │   ├── builder.py
│   │   ├── smil.py
│   │   ├── ncx.py
│   │   ├── opf.py
│   │   └── validator.py
│   │
│   ├── pipeline/
│   │   ├── __init__.py
│   │   └── orchestrator.py
│   │
│   ├── config.py
│   └── cli.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── data/
│   ├── input/
│   ├── intermediate/
│   └── output/
│
├── scripts/
│
├── docs/
│
├── configs/
│
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
└── LICENSE

Do not create frontend directories yet.

---

# 5. Internal data model

Create strongly typed Pydantic/dataclass models.

At minimum:

Book
- id
- title
- language
- author
- metadata
- chapters

Chapter
- id
- title
- sections
- order

Section
- id
- title
- paragraphs
- order

Paragraph
- id
- text
- sentences

Sentence
- id
- text
- paragraph_id
- chapter_id
- order

AudioSegment
- sentence_id
- file_path
- duration
- sample_rate
- start_time
- end_time

SynchronizationPoint
- text_id
- audio_file
- clip_begin
- clip_end

Keep the models independent of DAISY and independent of any TTS engine.

---

# 6. DTBook parser

Implement:

DTBook XML
→ Internal Book Model

Use lxml.

The parser must:

- validate that the XML is well formed
- extract metadata
- identify chapters
- identify sections
- identify paragraphs
- identify sentences or sentence-level textual units
- preserve stable IDs
- preserve reading order
- preserve important structural information
- handle namespaces correctly
- fail with clear errors

Never silently discard text.

Add tests using the provided `ejemplo.xml`.

---

# 7. Text normalization

Create a modular text normalization pipeline.

Handle:

- whitespace
- Unicode normalization
- numbers
- dates
- currencies
- abbreviations
- acronyms
- symbols
- URLs
- technical terms
- punctuation

Example:

"2026"

may become:

"dos mil veintiséis"

But normalization must preserve the original source text.

The system must maintain:

original_text

and:

tts_text

separately.

Never modify the source book simply because TTS normalization is needed.

---

# 8. Pronunciation lexicon

Implement a pronunciation dictionary abstraction.

Example:

{
    "Dr.": "doctor",
    "GPU": "G P U"
}

The lexicon must be configurable.

Do not hard-code language-specific replacements throughout the code.

Create a clean interface that can later support:

- JSON
- YAML
- PLS
- DAISY lexicon
- database-backed pronunciation dictionaries

---

# 9. TTS abstraction

Create:

class TTSEngine(ABC)

with methods such as:

synthesize(
    text: str,
    output_path: Path,
    voice_id: str | None = None
) -> Path

The pipeline must only depend on TTSEngine.

Never import XTTS/F5/CosyVoice directly into the main orchestration logic.

---

# 10. First implementation

For the first working prototype, implement a mock TTS engine.

Why?

Because all other pipeline components must be testable without requiring a GPU or a large ML model.

MockTTS should generate deterministic test audio.

Then implement the first real TTS adapter.

Start with XTTS-v2 or another selected model only after the pipeline interfaces are working.

Keep model-specific code isolated.

---

# 11. TTS requirements

The TTS system must eventually support:

- Spanish
- voice selection
- reference voice/audio where legally permitted
- GPU inference
- configurable sample rate
- deterministic configuration where possible
- error handling
- retries
- caching

The system should cache synthesized text.

If exactly the same:

text + voice + model + model_version + configuration

has already been synthesized, do not regenerate it unnecessarily.

---

# 12. Audio processing

Generate lossless intermediate audio.

Preferred:

WAV

Then process:

TTS WAV
→ normalization
→ silence handling
→ quality checks
→ final MP3

Use FFmpeg for final encoding.

Store:

- duration
- sample rate
- channels
- peak level
- RMS/loudness information where available
- clipping information

Do not repeatedly convert between compressed and lossless formats.

---

# 13. Synchronization

This is one of the most important components.

For every textual unit, maintain:

text_id
audio_file
clip_begin
clip_end

Example:

<par>
    <text src="book.xml#sentence001"/>
    <audio
        src="audio/ch01_001.mp3"
        clipBegin="0"
        clipEnd="4.231"/>
</par>

The synchronization layer must use actual generated audio duration.

Never guess audio durations.

---

# 14. DAISY 3 builder

Create a dedicated DAISY builder.

It should generate the necessary DAISY resources, including where applicable:

- DTBook XML
- SMIL
- NCX
- OPF
- resource metadata
- audio files

Keep the DAISY output generation separate from TTS.

The builder should consume the internal book model plus audio/synchronization metadata.

---

# 15. DAISY validation

Implement validation before declaring a book complete.

Check:

- XML validity
- required files exist
- all XML IDs are unique
- all SMIL text references exist
- all audio references exist
- clipBegin < clipEnd
- clipEnd does not exceed audio duration
- chapters are navigable
- NCX references valid targets
- OPF references valid resources
- no orphan audio files
- no missing synchronization entries

Return a structured validation report.

Example:

{
    "valid": false,
    "errors": [],
    "warnings": []
}

---

# 16. Pipeline orchestrator

Create:

BookPipeline

with stages:

1. ingest
2. parse
3. normalize
4. segment
5. synthesize
6. process_audio
7. synchronize
8. build_daisy
9. validate
10. package

Each stage should be independently testable.

The pipeline should support resume/restart.

If synthesis fails for sentence 3000, do not regenerate sentences 1–2999.

---

# 17. CLI

Before any UI, provide:

python -m app.cli convert 
    --input data/input/ejemplo.xml 
    --output data/output/book

Also:

python -m app.cli validate 
    --input data/output/book

And:

python -m app.cli benchmark 
    --input data/input/test.xml

Add useful logging and progress information.

Example:

[1/8] Parsing book...
[2/8] Normalizing text...
[3/8] Segmenting...
[4/8] Generating speech...
[5/8] Processing audio...
[6/8] Creating synchronization...
[7/8] Building DAISY...
[8/8] Validating...

---

# 18. Testing strategy

Write tests BEFORE implementing complex components whenever practical.

Unit tests:

- DTBook parser
- normalization
- segmentation
- lexicon
- audio metadata
- synchronization
- SMIL generation
- NCX generation
- OPF generation
- validation

Integration test:

ejemplo.xml
→ MockTTS
→ DAISY output
→ validation

The integration test must run without GPU.

---

# 19. Docker

Create a Docker setup that can eventually support GPU inference.

Do not require a GPU just to run tests.

The CPU/test image should support:

- parsing
- normalization
- synchronization
- DAISY generation
- validation

GPU dependencies should be isolated as much as reasonably possible.

---

# 20. Git strategy

Use Git from the beginning.

Recommended commits:

1. initial project structure
2. add data models
3. add DTBook parser
4. add text normalization
5. add segmentation
6. add TTS interface
7. add mock TTS
8. add audio processing
9. add synchronization
10. add DAISY builder
11. add validation
12. add integration tests
13. add real TTS adapter
14. add Docker
15. add CI

Do not create one giant commit.

---

# 21. GitHub Actions

CI should run:

- formatting
- linting
- type checking
- unit tests
- integration tests
- coverage

The CI pipeline must NOT require a GPU.

---

# 22. Performance requirements

Measure:

- total processing time
- generated audio duration
- Real-Time Factor (RTF)
- GPU memory
- CPU memory
- throughput
- failure rate

RTF:

RTF = synthesis_time / generated_audio_duration

Example:

10 minutes of audio generated in 2 minutes:

RTF = 0.2

Lower is better.

Do not optimize prematurely.

First create a correct baseline.

---

# 23. Future web architecture

DO NOT implement this yet.

Eventually:

Frontend:
Next.js/React

Backend:
FastAPI

Queue:
Redis + Celery/RQ

Database:
PostgreSQL

Storage:
S3-compatible storage

Workers:
GPU TTS workers

Architecture:

Browser
↓
FastAPI
↓
Job Queue
↓
GPU Worker
↓
TTS
↓
DAISY Builder
↓
Object Storage

The API must be asynchronous for long-running book generation.

Do not make the browser wait for the entire conversion request.

---

# 24. Future accessibility requirements

The future UI MUST support:

- keyboard-only navigation
- screen readers
- semantic HTML
- ARIA where necessary
- visible focus states
- high contrast
- adjustable text size
- adjustable playback speed
- chapter navigation
- sentence navigation
- bookmarks
- resume playback
- DAISY download
- accessible error messages

The DAISY package remains a first-class output.

The web application must not become the only way to consume the audiobook.

---

# 25. Security and legal requirements

Do not implement unrestricted voice cloning.

Voice cloning must require a permitted/authorized voice reference.

Store voice metadata and permissions.

Do not assume that a model's code license automatically means the model weights/data can be used commercially or by a government institution.

Create documentation for:

- model license
- model weights license
- voice consent
- book copyright
- data provenance
- generated audio ownership/usage

These issues must be reviewed before production deployment.

---

# 26. Model benchmarking

After the basic pipeline works, create a benchmark framework.

Candidate models:

- XTTS-v2
- F5-TTS
- CosyVoice
- MeloTTS

Do not automatically assume one is best.

Measure:

- RTF
- VRAM
- naturalness
- Spanish pronunciation
- speaker similarity
- long-form stability
- failure rate
- latency
- licensing suitability

Use the same Spanish test corpus for every model.

Produce a benchmark report.

---

# 27. Important engineering rules

1. Do not build the frontend yet.
2. Do not train a TTS model yet.
3. Do not couple DAISY generation to one TTS model.
4. Do not hard-code paths.
5. Do not silently discard text.
6. Do not regenerate successful audio unnecessarily.
7. Do not make the whole pipeline fail because of one sentence.
8. Do not assume cloud TTS is the final architecture.
9. Do not assume a model license is sufficient for government deployment.
10. Keep the system modular.
11. Prefer typed interfaces.
12. Write tests for every important transformation.
13. Make the pipeline resumable.
14. Keep intermediate artifacts for debugging.
15. Document architectural decisions.

---

# 28. Your immediate task

Start ONLY with Phase 1.

Implement:

1. repository structure
2. pyproject.toml
3. configuration
4. data models
5. DTBook parser
6. text normalization interface
7. sentence segmentation
8. TTSEngine interface
9. MockTTS
10. basic CLI
11. unit tests
12. integration test
13. README
14. Dockerfile
15. GitHub Actions

Use the provided `ejemplo.xml` as the primary fixture.

Do NOT implement:

- React
- Next.js
- FastAPI
- Redis
- PostgreSQL
- cloud deployment
- authentication
- production GPU infrastructure

until the CLI pipeline is working.

Before writing a large amount of code, inspect the existing repository and existing files.

If code already exists, modify it rather than duplicating functionality.

After each major phase, explain:

- what was implemented
- why it was implemented
- files changed
- how to run it
- tests added
- known limitations
- next recommended step

Do not claim that something works unless it has actually been tested.

Start by inspecting the repository and then implement Phase 1 incrementally.