"""Общие фикстуры тестов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coverage.config import Settings, load_settings  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SMALL = FIXTURES / "small"
OPEN_DATA = ROOT / "data"


@pytest.fixture
def settings() -> Settings:
    return load_settings(ROOT / "config.yaml")


def csv_bytes(header: str, rows: list[str]) -> bytes:
    return (header + "\n" + "\n".join(rows) + "\n").encode("utf-8")
