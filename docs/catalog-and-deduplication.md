# Catalog and audiobook deduplication

Phase 8 adds a SQLite catalog at `data/vocality.db`. SQLAlchemy models and
repositories are isolated under `app/database/`; generated audio and DAISY
packages remain on the filesystem.

```text
upload -> content fingerprint -> Book catalog
                         -> generation identity
                         -> existing ready audiobook OR one queued job
```

## Book versus audiobook

A `Book` is logical source content. An `Audiobook` is one generated variant of
that source. One book can therefore have multiple variants for different
voices, models, or settings.

Book identity uses the parser's SHA-256 source-content fingerprint. The
filesystem book ID is derived from that fingerprint, so identical uploads do
not create duplicate source directories.

## Generation identity

The generation key is SHA-256 over canonical JSON containing:

- source content fingerprint
- language
- TTS engine and voice
- model version
- normalization version
- generation settings

Keys use sorted JSON keys and compact separators. SQLite enforces uniqueness
on the key. A ready matching audiobook returns the original completed job;
matching processing work returns the existing job instead of queueing another.
Failed variants may be retried.

The initial schema migration is run automatically for a fresh SQLite database
and records version `1` in `schema_migrations`. The database file is ignored
by Git and can be relocated with `DAISY_DATABASE_URL`.
