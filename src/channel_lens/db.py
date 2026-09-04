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

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import database_path
from .models import Base

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
    """Create any missing tables.

    The schema is additive only — new columns arrive with defaults and old rows
    stay valid — so plain ``create_all`` is enough and there is no migration
    tool here. If that ever stops being true, this is where Alembic goes.
    """
    Base.metadata.create_all(get_engine())


def reset_state_for_tests() -> None:
    """Drop the cached engine so a test can repoint ``CHANNEL_LENS_HOME``."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
