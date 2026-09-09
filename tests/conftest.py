"""Test fixtures.

Every test gets its own ``CHANNEL_LENS_HOME``, so a test run can never read or
write the real database, settings, or cached thumbnails.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CHANNEL_LENS_HOME", str(home))

    # The engine and settings are process-wide singletons; drop both so they
    # rebuild against this test's directory rather than a previous test's.
    from channel_lens import config, db
    from channel_lens.services import jobs

    db.reset_state_for_tests()
    config._cached = None
    # Background jobs are process-global; a leftover "running" job would make
    # the next test's import be refused as a duplicate.
    jobs.reset_for_tests()
    db.init_db()

    yield home

    db.reset_state_for_tests()
    config._cached = None
    jobs.reset_for_tests()


@pytest.fixture
def session() -> Iterator:
    from channel_lens.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def client() -> Iterator:
    from fastapi.testclient import TestClient

    from channel_lens.main import create_app

    # The scheduler is not started here: a background thread polling YouTube
    # during a test run is exactly the kind of surprise tests exist to prevent.
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def settings():
    from channel_lens.config import get_settings

    return get_settings(refresh=True)
