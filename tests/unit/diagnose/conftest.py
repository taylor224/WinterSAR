"""Fixtures for the diagnose tests (log excerpts live in tests/fixtures/logs/)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

LOGS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "logs"


@pytest.fixture
def logs_dir() -> Path:
    return LOGS_DIR


def load_manifest() -> list[dict[str, Any]]:
    data = yaml.safe_load((LOGS_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


@pytest.fixture
def manifest() -> list[dict[str, Any]]:
    return load_manifest()


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the fixture paths (/home/user/...) look like the current home for masking tests."""
    monkeypatch.setenv("HOME", "/home/user")
    return "/home/user"
