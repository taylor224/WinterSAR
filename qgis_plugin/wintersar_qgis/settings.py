"""Plugin settings persisted as a JSON file (ADR-0071: environment discovery inputs).

Pure python: no QGIS import at module level. The settings file lives in the QGIS user
profile directory when QGIS is importable (``QgsApplication.qgisSettingsDirPath()``), and in
a per-user config directory otherwise (tests, command-line use).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

SETTINGS_FILENAME = "wintersar_qgis.json"
DEFAULT_TIMEOUT_S = 3600.0
MAX_RECENT = 8


@dataclass
class PluginSettings:
    """Everything the plugin remembers between sessions."""

    python_exe: str | None = None
    env_hint: str | None = None
    lang: str = "ko"
    timeout_s: float = DEFAULT_TIMEOUT_S
    last_config: str | None = None
    last_aoi: str | None = None
    last_candidates: str | None = None
    last_refpoint_ts: str | None = None
    last_ts_file: str | None = None
    last_leveling: str | None = None
    last_gnss: str | None = None
    recent_configs: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ io
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginSettings:
        """Tolerant constructor: unknown keys ignored, wrong types coerced/defaulted."""
        known = {f.name for f in fields(cls)}
        clean: dict[str, Any] = {}
        for k, v in data.items():
            if k not in known:
                continue
            clean[k] = v
        recent_raw = clean.pop("recent_configs", [])
        timeout_raw = clean.pop("timeout_s", DEFAULT_TIMEOUT_S)
        lang_raw = clean.pop("lang", "ko")
        s = cls(**clean)
        s.lang = str(lang_raw) if lang_raw in ("ko", "en") else "ko"
        try:
            s.timeout_s = float(timeout_raw)
        except (TypeError, ValueError):
            s.timeout_s = DEFAULT_TIMEOUT_S
        if s.timeout_s <= 0:
            s.timeout_s = DEFAULT_TIMEOUT_S
        recent = recent_raw if isinstance(recent_raw, list) else []
        s.recent_configs = [str(p) for p in recent][:MAX_RECENT]
        for name in (
            "python_exe",
            "env_hint",
            "last_config",
            "last_aoi",
            "last_candidates",
            "last_refpoint_ts",
            "last_ts_file",
            "last_leveling",
            "last_gnss",
        ):
            v = getattr(s, name)
            setattr(s, name, str(v) if v not in (None, "") else None)
        return s

    def remember_config(self, path: str | Path) -> None:
        p = str(path)
        self.last_config = p
        self.recent_configs = [p, *[x for x in self.recent_configs if x != p]][:MAX_RECENT]


def _platform() -> str:
    return sys.platform  # indirection so mypy does not prune the other OS branches


def default_settings_dir() -> Path:
    """QGIS profile dir when running inside QGIS, else a per-user config directory."""
    try:
        from qgis.core import QgsApplication

        # source: https://qgis.org/pyqgis/3.44/core/QgsApplication.html
        #   static qgisSettingsDirPath() -> str  "path to the settings directory in user's home dir"
        d = str(QgsApplication.qgisSettingsDirPath())
        if d:
            return Path(d)
    except Exception:
        pass
    env = os.environ.get("WINTERSAR_QGIS_SETTINGS_DIR")
    if env:
        return Path(env)
    platform = _platform()
    if platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "wintersar"
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wintersar"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "wintersar"


def default_settings_path() -> Path:
    return default_settings_dir() / SETTINGS_FILENAME


def load_settings(path: str | Path | None = None) -> PluginSettings:
    """Read the JSON settings file; a missing or corrupt file yields defaults."""
    p = Path(path) if path else default_settings_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PluginSettings()
    if not isinstance(raw, dict):
        return PluginSettings()
    return PluginSettings.from_dict(raw)


def save_settings(settings: PluginSettings, path: str | Path | None = None) -> Path:
    """Atomic write (tmp + replace) of the settings JSON; returns the path written."""
    p = Path(path) if path else default_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        Path(tmp_name).replace(p)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return p
