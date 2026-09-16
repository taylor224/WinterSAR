from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.pipeline._support import SMALL, EngineSpy, spy_fake_engine, write_fake_config
from wintersar.pipeline.config import Config


@pytest.fixture
def fake_cfg(tmp_path: Path, cache_dir: Path) -> Config:
    return write_fake_config(tmp_path)


@pytest.fixture
def small() -> dict[str, dict[str, object]]:
    return {k: dict(v) for k, v in SMALL.items()}


@pytest.fixture
def engine_spy(monkeypatch: pytest.MonkeyPatch) -> EngineSpy:
    return spy_fake_engine(monkeypatch)
