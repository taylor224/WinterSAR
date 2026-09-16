"""Helpers shared by pipeline unit and integration tests (fake engine, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from wintersar.engines.fake import FakeEngine
from wintersar.pipeline.config import Config, load_config

SMALL = {"interferogram": {"n_dates": 5, "shape": [24, 24]}}


def write_fake_config(tmp_path: Path, **sections: Any) -> Config:
    """config.yaml on the fake path (engine fake, timeseries fake, unwrap auto)."""
    aoi = tmp_path / "aoi.geojson"
    if not aoi.exists():
        aoi.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    data: dict[str, Any] = {
        "project": {"name": "fake-site", "workdir": "work", "language": "ko"},
        "aoi": "aoi.geojson",
        "time_range": {"start": "2024-01-01", "end": "2024-06-30"},
        "engine": {"interferogram": "fake"},
        "timeseries": {"engine": "fake"},
        "unwrap": {"method": "auto", "coherence_threshold": 0.3},
        "compute": {"cores": 2, "memory_gb": 4},
    }
    for k, v in sections.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_config(p)


class EngineSpy:
    """Records every FakeEngine.run call (stage, params) while installed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def stages(self) -> list[str]:
        return [s for s, _ in self.calls]

    def clear(self) -> None:
        self.calls.clear()


def spy_fake_engine(monkeypatch: pytest.MonkeyPatch) -> EngineSpy:
    spy = EngineSpy()
    original = FakeEngine.run

    def run(
        self: FakeEngine, stage: str, inputs: Any, params: dict[str, Any], log_dir: Path
    ) -> Any:
        spy.calls.append((stage, dict(params)))
        return original(self, stage, inputs, params, log_dir)

    monkeypatch.setattr(FakeEngine, "run", run)
    return spy
