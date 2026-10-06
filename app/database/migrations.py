"""Small initial migration runner for the SQLite catalog."""

from sqlalchemy import Engine, text

from .models import Base


INITIAL_SCHEMA_VERSION = "1"


def run_initial_migration(engine: Engine) -> None:
    """Create the initial schema and record its version on a fresh database."""
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version VARCHAR(32) PRIMARY KEY)"
        )
        connection.execute(
            text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"),
            {"version": INITIAL_SCHEMA_VERSION},
        )
