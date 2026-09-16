"""Fixtures for the QGIS plugin tests (no QGIS installed: pure-python parts only)."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

QGIS_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "qgis_plugin"
if str(QGIS_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(QGIS_PLUGIN_DIR))

PLUGIN_PKG_DIR = QGIS_PLUGIN_DIR / "wintersar_qgis"
REPO_I18N_DIR = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "i18n"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTERSAR_QGIS_SETTINGS_DIR", str(tmp_path / "settings"))


@pytest.fixture
def venv_python() -> Path:
    return Path(sys.executable)


FakeCli = Callable[..., Path]


@pytest.fixture
def fake_cli(tmp_path: Path) -> FakeCli:
    """Factory for a fake "python" executable that ignores its arguments.

    ``fake_cli(stdout=..., stderr=..., exit_code=..., sleep_s=...)`` returns the path to
    pass as ``python_exe``; the script prints the canned streams and exits.
    """
    if os.name == "nt":  # pragma: no cover - POSIX shebang scripts only
        pytest.skip("fake interpreter script needs POSIX")

    def make(
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        sleep_s: float = 0.0,
        name: str = "fake_python",
    ) -> Path:
        script = tmp_path / name
        body = (
            f"#!{sys.executable}\n"
            "import sys, time\n"
            f"time.sleep({sleep_s!r})\n"
            f"sys.stderr.write({stderr!r}); sys.stderr.flush()\n"
            f"sys.stdout.write({stdout!r}); sys.stdout.flush()\n"
            f"sys.exit({exit_code})\n"
        )
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script

    return make
