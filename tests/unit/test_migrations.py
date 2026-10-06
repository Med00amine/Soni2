from pathlib import Path

from sqlalchemy import create_engine, text

from app.database.migrations import run_initial_migration


def test_phase_9_schema_migration_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    run_initial_migration(engine)
    run_initial_migration(engine)
    with engine.connect() as connection:
        versions = set(connection.scalars(text("SELECT version FROM schema_migrations")))
        tables = set(connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'")))
    assert {"1", "2"} <= versions
    assert {"users", "user_library", "favorites", "listening_progress"} <= tables
