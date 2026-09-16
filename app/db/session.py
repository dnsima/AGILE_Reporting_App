"""Engine and session management.

SQLite is the zero-setup default; set ``DATABASE_URL`` to a PostgreSQL DSN for
a deployment that has to carry 36 states + FCT of historical data.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import Base


def _build_engine() -> Engine:
    url = settings.database_url
    kwargs: dict = {"echo": settings.sql_echo, "future": True}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" not in url:
            # Make sure the parent directory exists before SQLite opens the file.
            db_path = url.split("///", 1)[-1]
            if db_path and db_path != ":memory:":
                Path(db_path).expanduser().resolve().parent.mkdir(
                    parents=True, exist_ok=True
                )
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

    return create_engine(url, **kwargs)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Enable FK enforcement and WAL on SQLite connections."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for scripts and background work."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create any missing tables. Import models first so they register."""
    from app import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)
