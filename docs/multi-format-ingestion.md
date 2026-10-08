# Unified multi-format ingestion

Vocality accepts DTBook XML, EPUB, HTML, and text-based PDF documents. Each
format has a parser under `app/ingestion/`, but every parser returns the same
domain `Book` model. The existing normalization, sentence segmentation, TTS,
synchronization, DAISY builder, and validator therefore remain format-agnostic.

```text
DTBook ─┐
EPUB ───┤
HTML ───┼──> parser layer -> Book model -> audiobook pipeline
PDF ────┘
```

## Source metadata and identity

The `Book` model records `source_format`, `source_filename`, and a SHA-256
`content_fingerprint`. The fingerprint is calculated from the source bytes,
not the temporary upload filename, and is deterministic for identical input.
It is preparation for future audiobook reuse/deduplication; Phase 7 does not
add a database.

## Format behavior

- **DTBook:** existing namespace-aware parser and reading order are preserved.
- **EPUB:** the container, OPF metadata, manifest, and spine are read. XHTML
  content is processed in spine order.
- **HTML:** semantic `article`, `section`, headings, and paragraphs are
  extracted. Script, style, navigation, noscript, and hidden content are
  ignored.
- **PDF:** text is extracted page by page with `pypdf`. Complex layout may not
  preserve semantic reading order, and scanned PDFs are rejected when they
  contain no extractable text. OCR is future work.

Uploads are size-limited, extension-checked, and validated using file
signatures/package structure where applicable. EPUB members are never
extracted to disk and archive paths containing traversal components are
rejected. Uploaded filenames are reduced to their basename before temporary
or persistent storage.
