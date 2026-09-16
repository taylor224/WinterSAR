"""Mask personal paths and secrets in logs/reports (rule 11.11).

Use :func:`mask_text` on any string that ends up in a report, a log excerpt, a Finding
``evidence`` value or a JSON output. Use :func:`mask_mapping` for nested dicts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Earthdata / generic bearer tokens & JWTs
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # key=value style secrets
    re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|authorization)\s*[=:]\s*\S+"),
    # netrc lines
    re.compile(r"(?i)(password\s+)\S+"),
    # URL credentials  https://user:pass@host
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),
)

_HOME_ENV_VARS = ("EARTHDATA_TOKEN", "HYP3_TOKEN", "CDSE_TOKEN", "AWS_SECRET_ACCESS_KEY")


def _home_dirs() -> list[str]:
    homes = {str(Path.home())}
    for var in ("HOME", "USERPROFILE"):
        v = os.environ.get(var)
        if v:
            homes.add(v)
    return sorted(homes, key=len, reverse=True)


def mask_text(text: str, extra_secrets: list[str] | None = None) -> str:
    """Replace secrets and home-directory prefixes in ``text``."""
    if not text:
        return text
    out = text
    for var in _HOME_ENV_VARS:
        v = os.environ.get(var)
        if v and len(v) >= 8:
            out = out.replace(v, "***")
    for s in extra_secrets or []:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(_replacement, out)
    for home in _home_dirs():
        out = out.replace(home, "~")
    return out


def _replacement(m: re.Match[str]) -> str:
    # keep the leading capture group (e.g. 'Bearer ', 'token=', 'https://'), mask the rest
    groups = m.groups()
    if groups and groups[0] is not None:
        prefix = groups[0]
        if prefix.lower().startswith(
            ("token", "password", "passwd", "secret", "api", "authorization")
        ):
            return f"{prefix}=***"
        if prefix.startswith("http"):
            return f"{prefix}***:***@"
        return f"{prefix}***"
    return "***"


def mask_mapping(obj: Any, extra_secrets: list[str] | None = None) -> Any:
    """Recursively mask strings inside dicts/lists (returns a new object)."""
    if isinstance(obj, str):
        return mask_text(obj, extra_secrets)
    if isinstance(obj, Path):
        return mask_text(str(obj), extra_secrets)
    if isinstance(obj, Mapping):
        return {k: mask_mapping(v, extra_secrets) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [mask_mapping(v, extra_secrets) for v in obj]
    return obj
