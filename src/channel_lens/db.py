"""Engine and session handling for the local SQLite database.

The path is always resolved absolutely from :func:`config.app_home`, never
relative to the working directory — a browser-launched app has no guaranteed
CWD, and a relative sqlite URL fails in a way ("unable to open database file")
that looks like corruption rather than a path bug.

WAL mode is on because the background tracker writes snapshots while the UI
reads; without it, SQLite's default locking turns a routine poll into
"database is locked" in the middle of a page load.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import database_path
from .models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    # Wait rather than fail when the tracker and a request collide.
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    # NORMAL is the right durability trade for a cache-like local store: a
    # power cut can cost the last few snapshots, all of which are re-fetchable.
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{database_path()}",
            future=True,
            # Needed because the APScheduler tracker thread shares this engine.
            connect_args={"check_same_thread": False},
        )
        event.listen(_engine, "connect", _configure_sqlite)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create missing tables, then add any missing columns to existing ones.

    ``create_all`` creates tables but never alters them, so on an install that
    already has a database a newly added column simply doesn't exist — and the
    failure arrives later as a confusing ``no such column`` at query time. That
    has caught this project twice, so the column check runs on every startup.
    """
    Base.metadata.create_all(get_engine())
    add_missing_columns()


#: SQLite type names for the column types this schema actually uses.
_SQLITE_TYPES = {
    "INTEGER": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "VARCHAR": "TEXT", "TEXT": "TEXT", "FLOAT": "REAL", "NUMERIC": "NUMERIC",
    "BOOLEAN": "BOOLEAN", "DATETIME": "DATETIME", "DATE": "DATE", "JSON": "JSON",
}


def add_missing_columns() -> list[str]:
    """Add columns present in the models but missing from the database.

    A deliberately minimal migration step, not a migration tool. SQLite's
    ``ALTER TABLE ... ADD COLUMN`` can only append a nullable column (or one
    with a constant default), which is exactly the additive-only change this
    schema is allowed to make. Anything else — renames, type changes, new
    constraints — is out of scope and would need Alembic.

    Returns the ``table.column`` names added, for logging.
    """
    engine = get_engine()
    added: list[str] = []

    with engine.begin() as connection:
        existing_tables = {
            row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all just made it, so it is already current.

            present = {
                row[1] for row in connection.exec_driver_sql(
                    f"PRAGMA table_info('{table.name}')"
                )
            }

            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.server_default is None:
                    # Cannot be added to a table with existing rows.
                    log.warning(
                        "Cannot add non-nullable column %s.%s automatically; "
                        "it needs a real migration.", table.name, column.name,
                    )
                    continue

                type_name = type(column.type).__name__.upper()
                sql_type = _SQLITE_TYPES.get(
                    type_name, column.type.compile(engine.dialect)
                )
                connection.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {sql_type}'
                )
                added.append(f"{table.name}.{column.name}")

    if added:
        log.info("Added missing columns: %s", ", ".join(added))
    return added


def reset_state_for_tests() -> None:
    """Drop the cached engine so a test can repoint ``CHANNEL_LENS_HOME``."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
