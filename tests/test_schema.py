"""Additive schema sync.

``create_all`` creates missing tables and silently ignores missing columns, so
without this a model change lands as "no such column" against the database the
NPCU has been loading real returns into. There is no migration tool here, so
the one safe operation -- add -- is done on startup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, Table, inspect, text

from app.db.base import Base
from app.db.session import add_missing_columns, engine


@pytest.fixture()
def scratch_table():
    """A table that exists in the database but is missing a column."""
    name = "schema_sync_probe"
    table = Table(
        name,
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("kept", String(16)),
    )
    table.create(bind=engine, checkfirst=True)
    try:
        yield table
    finally:
        table.drop(bind=engine, checkfirst=True)
        Base.metadata.remove(table)


def _columns(name: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(name)}


def test_a_new_nullable_column_is_added_to_an_existing_table(db, scratch_table):
    scratch_table.append_column(Column("added_later", String(32)))
    assert "added_later" not in _columns(scratch_table.name)

    assert f"{scratch_table.name}.added_later" in add_missing_columns()
    assert "added_later" in _columns(scratch_table.name)


def test_existing_rows_keep_their_data_and_take_the_default(db, scratch_table):
    with engine.begin() as connection:
        connection.execute(
            text(f"INSERT INTO {scratch_table.name} (id, kept) VALUES (1, 'original')")
        )

    scratch_table.append_column(
        Column("with_default", Integer, nullable=False, default=7)
    )
    add_missing_columns()

    with engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT kept, with_default FROM {scratch_table.name} WHERE id = 1")
        ).one()
    assert row.kept == "original"
    assert row.with_default == 7


def test_a_not_null_column_without_a_default_is_skipped_not_guessed(db, scratch_table):
    scratch_table.append_column(Column("no_default", String(16), nullable=False))
    assert add_missing_columns() == []
    assert "no_default" not in _columns(scratch_table.name)


def test_running_it_twice_changes_nothing(db, scratch_table):
    scratch_table.append_column(Column("once", String(16)))
    assert add_missing_columns()
    assert add_missing_columns() == []


def test_the_live_models_are_already_in_step(db):
    """The test database is built by create_all, so nothing should be missing."""
    assert add_missing_columns() == []


def test_the_indicator_time_basis_column_is_reachable(db):
    from app.models import Indicator

    indicator = db.query(Indicator).first()
    indicator.time_basis = "SUM"
    db.flush()
    assert db.query(Indicator).filter_by(time_basis="SUM").count() == 1
