"""Engine and session management.

SQLite is the zero-setup default; set ``DATABASE_URL`` to a PostgreSQL DSN for
a deployment that has to carry 36 states + FCT of historical data.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging_config import get_logger
from app.db.base import Base

logger = get_logger(__name__)


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


def _literal(value: object) -> str | None:
    """Render a scalar column default as SQL, or None if it is not one."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def add_missing_columns() -> list[str]:
    """Add columns the models declare but an existing table does not have.

    ``create_all`` creates missing *tables* and silently ignores missing
    *columns*, so a schema change lands as "no such column" against a database
    that already holds data -- which, for this project, is the one the NPCU has
    been loading real returns into. There is no migration tool here, so this
    does the one safe thing a schema change needs: add. It never drops,
    renames or retypes anything, and it skips any column it cannot add without
    guessing a value for the rows already there.
    """
    from app import models  # noqa: F401  (registers mappers, so the metadata is whole)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    added: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue  # create_all will build it in full
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            clause = f"{column.name} {column.type.compile(engine.dialect)}"
            if not column.nullable:
                default = getattr(column.default, "arg", None)
                rendered = _literal(default) if default is not None else None
                if rendered is None:
                    logger.warning(
                        "cannot add a NOT NULL column without a scalar default",
                        extra={"table": table.name, "column_name": column.name},
                    )
                    continue
                clause += f" NOT NULL DEFAULT {rendered}"

            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {clause}"))
            added.append(f"{table.name}.{column.name}")

    if added:
        logger.info("added missing columns", extra={"columns": added})
    return added


def init_db() -> None:
    """Create any missing tables and columns. Import models first so they register."""
    from app import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)
    add_missing_columns()

