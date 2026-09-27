from pathlib import Path

import pytest

from ftbfs.db import DB
from ftbfs.ingest import parse, read_html

FIXTURE = Path(__file__).parent / "fixtures" / "ftbfs-2026-09-27.html.gz"


@pytest.fixture(scope="session")
def page() -> bytes:
    return read_html(FIXTURE)


@pytest.fixture(scope="session")
def snapshot(page):
    return parse(page, fetched_at="2026-09-27T00:00:00+00:00")


@pytest.fixture
def db(tmp_path) -> DB:
    return DB(tmp_path / "ftbfs.db")
