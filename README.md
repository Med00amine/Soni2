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
