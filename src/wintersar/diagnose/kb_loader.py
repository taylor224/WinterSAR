"""Knowledge-base (KB) loader and schema (plan §5.5, ADR-0035).

Every ``kb/*.yaml`` file is a list of entries::

    - id: KB-SNAPHU-001
      engine: snaphu                    # snaphu | isce2 | mintpy | hyp3 | asf | any
      stage: unwrap                     # optional pipeline stage the failure belongs to
      pattern: "Exceeded maximum number of secondary nodes"   # python regex, MULTILINE
      ignore_case: false                # optional
      extract: [swath]                  # optional: named groups copied to Finding.params
      cause: {ko: "...", en: "..."}
      fix:   {ko: "...", en: "..."}
      refs:  ["https://..."]
      severity: FAIL                    # FAIL | WARN | INFO
      retry_hint:                       # optional, consumed by ``pipeline`` (attach_retry_hint)
        stage: unwrap
        action: retry
        params: {tile_cost_thresh: 250}
        note: "..."
      supersedes: [KB-ENV-002]          # optional: drop these ids when this entry matches too
      pattern_verified: true            # regex derived from the upstream source string
      pattern_source: "https://github.com/.../snaphu_tile.c (TraceRegions)"

The same ``cause``/``fix`` text must exist as i18n keys ``diagnose.<ID>.cause`` /
``diagnose.<ID>.fix`` (rule 11.6); ``scripts/render_kb_docs.py --write-i18n`` keeps them in
sync and ``tests/unit/diagnose/test_i18n.py`` asserts equality.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wintersar.io.schemas import Severity

KB_DIR = Path(__file__).resolve().parent / "kb"

KBEngine = Literal["snaphu", "isce2", "mintpy", "hyp3", "asf", "any"]
KB_ENGINES: tuple[str, ...] = ("snaphu", "isce2", "mintpy", "hyp3", "asf")
UNKNOWN_RULE_ID = "KB-UNKNOWN"

_ID_RE = re.compile(r"^KB-[A-Z0-9]+-\d{3}$")


class LocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ko: str = Field(min_length=1)
    en: str = Field(min_length=1)

    def get(self, lang: str) -> str:
        return self.ko if lang == "ko" else self.en


class RetryHint(BaseModel):
    """Machine-readable retry suggestion attached to a KB entry."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    action: str = Field(
        default="retry",
        description="retry | tile | adjust_tiles | assemble | retry_later | manual",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class KBEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    engine: KBEngine
    stage: str | None = None
    pattern: str = Field(min_length=1)
    ignore_case: bool = False
    extract: list[str] | None = None
    cause: LocalizedText
    fix: LocalizedText
    refs: list[str] = Field(default_factory=list)
    severity: Severity
    retry_hint: RetryHint | None = None
    supersedes: list[str] = Field(
        default_factory=list,
        description="KB ids dropped from the result when this entry also matches the same text",
    )
    pattern_verified: bool
    pattern_source: str | None = None

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not _ID_RE.match(v):
            msg = f"KB id {v!r} must look like KB-<ENGINE>-<NNN>"
            raise ValueError(msg)
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        try:
            re.compile(v, re.MULTILINE)
        except re.error as e:
            msg = f"invalid regex {v!r}: {e}"
            raise ValueError(msg) from e
        return v

    @model_validator(mode="after")
    def _extract_groups_exist(self) -> KBEntry:
        if self.extract:
            names = set(self.regex.groupindex)
            missing = [g for g in self.extract if g not in names]
            if missing:
                msg = f"{self.id}: extract groups {missing} are not named groups of the pattern"
                raise ValueError(msg)
        if self.pattern_verified and not self.pattern_source:
            msg = f"{self.id}: pattern_verified entries must give pattern_source"
            raise ValueError(msg)
        return self

    # ---------------------------------------------------------------- helpers
    @property
    def regex(self) -> re.Pattern[str]:
        return _compile(self.pattern, self.ignore_case)

    @property
    def message_key(self) -> str:
        return f"diagnose.{self.id}.cause"

    @property
    def fix_key(self) -> str:
        return f"diagnose.{self.id}.fix"

    @property
    def extract_groups(self) -> list[str]:
        """Named groups copied into ``Finding.params`` (all named groups when unset)."""
        if self.extract is not None:
            return list(self.extract)
        return list(self.regex.groupindex)


@lru_cache(maxsize=256)
def _compile(pattern: str, ignore_case: bool) -> re.Pattern[str]:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    return re.compile(pattern, flags)


def load_kb_file(path: Path) -> list[KBEntry]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or []
    if not isinstance(data, list):
        msg = f"{path}: top level must be a list of KB entries"
        raise ValueError(msg)
    entries: list[KBEntry] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            msg = f"{path}[{i}]: entry must be a mapping"
            raise ValueError(msg)
        try:
            entries.append(KBEntry.model_validate(raw))
        except ValueError as e:
            msg = f"{path}[{i}] ({raw.get('id', '?')}): {e}"
            raise ValueError(msg) from e
    return entries


@lru_cache(maxsize=4)
def _load_dir(kb_dir: str) -> tuple[KBEntry, ...]:
    entries: list[KBEntry] = []
    for f in sorted(Path(kb_dir).glob("*.yaml")):
        entries.extend(load_kb_file(f))
    seen: dict[str, str] = {}
    for e in entries:
        if e.id in seen:
            msg = f"duplicate KB id {e.id}"
            raise ValueError(msg)
        seen[e.id] = e.engine
    for e in entries:
        for other in e.supersedes:
            if other not in seen:
                msg = f"{e.id}: supersedes unknown KB id {other}"
                raise ValueError(msg)
    return tuple(entries)


def load_kb(kb_dir: Path | None = None) -> list[KBEntry]:
    """All KB entries (sorted by id) from ``kb_dir`` (default: the packaged ``kb/``)."""
    return sorted(_load_dir(str(kb_dir or KB_DIR)), key=lambda e: e.id)


def kb_index(kb_dir: Path | None = None) -> dict[str, KBEntry]:
    return {e.id: e for e in load_kb(kb_dir)}


def entries_for_engine(engine: str | None, kb_dir: Path | None = None) -> list[KBEntry]:
    """Entries applicable to ``engine`` (engine-specific + ``any``); all when ``engine`` is None."""
    entries = load_kb(kb_dir)
    if engine is None:
        return entries
    return [e for e in entries if e.engine in (engine, "any")]
