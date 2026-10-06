"""SQLite engine/session setup and initial schema migration."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .migrations import run_initial_migration


def create_session_factory(database_url: str, data_root: Path):
    if database_url.startswith("sqlite:///") and database_url.endswith("vocality.db"):
        path = data_root / "vocality.db"
        database_url = f"sqlite:///{path.resolve().as_posix()}"
    engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
    run_initial_migration(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
