"""KB schema validation (plan §5.5, ADR-0035)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from wintersar.diagnose.kb_loader import (
    KB_DIR,
    KB_ENGINES,
    KBEntry,
    entries_for_engine,
    kb_index,
    load_kb,
    load_kb_file,
)

REQUIRED_SEED = {
    "KB-SNAPHU-001",
    "KB-SNAPHU-002",
    "KB-SNAPHU-003",
    "KB-ISCE2-001",
    "KB-ISCE2-002",
    "KB-ISCE2-003",
    "KB-ISCE2-004",
    "KB-MINTPY-001",
    "KB-MINTPY-002",
    "KB-MINTPY-003",
    "KB-HYP3-001",
    "KB-HYP3-002",
    "KB-AUTH-001",
    "KB-ENV-001",
    "KB-ASF-001",
}


def test_every_kb_yaml_validates() -> None:
    files = sorted(KB_DIR.glob("*.yaml"))
    assert files, "no KB yaml files"
    total = 0
    for f in files:
        entries = load_kb_file(f)
        assert entries, f"{f} is empty"
        total += len(entries)
    assert total >= 14


def test_seed_ids_present_and_unique() -> None:
    kb = load_kb()
    ids = [e.id for e in kb]
    assert len(ids) == len(set(ids))
    assert set(ids) >= REQUIRED_SEED
    assert len(ids) >= 14


@pytest.mark.parametrize("entry", load_kb(), ids=lambda e: e.id)
def test_entry_contract(entry: KBEntry) -> None:
    # ids and engines
    assert re.match(r"^KB-[A-Z0-9]+-\d{3}$", entry.id)
    assert entry.engine in (*KB_ENGINES, "any")
    # regex compiles with MULTILINE and extracted groups exist
    assert entry.regex.flags & re.MULTILINE
    for g in entry.extract_groups:
        assert g in entry.regex.groupindex
    # cause -> fix in both languages, refs present, severity valid
    assert entry.cause.ko and entry.cause.en and entry.fix.ko and entry.fix.en
    assert entry.refs, "every entry must cite its sources"
    assert entry.severity in ("FAIL", "WARN", "INFO")
    # verification policy: verified patterns cite where the string came from
    if entry.pattern_verified:
        assert entry.pattern_source
    # no inline global flags (Python 3.11 rejects them when not at the start)
    assert "(?m)" not in entry.pattern and "(?i)" not in entry.pattern


def test_all_seed_patterns_are_verified() -> None:
    unverified = [e.id for e in load_kb() if not e.pattern_verified]
    assert unverified == [], f"unverified patterns need an open-questions row: {unverified}"


def test_message_keys_follow_convention() -> None:
    for e in load_kb():
        assert e.message_key == f"diagnose.{e.id}.cause"
        assert e.fix_key == f"diagnose.{e.id}.fix"


def test_entries_for_engine_includes_any() -> None:
    snaphu = entries_for_engine("snaphu")
    assert {e.engine for e in snaphu} <= {"snaphu", "any"}
    assert any(e.id == "KB-AUTH-001" for e in snaphu)
    assert len(entries_for_engine(None)) == len(load_kb())


def test_supersedes_targets_exist() -> None:
    index = kb_index()
    for e in index.values():
        for other in e.supersedes:
            assert other in index


def test_invalid_entries_rejected(tmp_path: Path) -> None:
    base = {
        "id": "KB-TEST-001",
        "engine": "snaphu",
        "pattern": "x",
        "cause": {"ko": "a", "en": "b"},
        "fix": {"ko": "c", "en": "d"},
        "refs": ["r"],
        "severity": "FAIL",
        "pattern_verified": False,
    }
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump([base]), encoding="utf-8")
    assert load_kb_file(ok)[0].id == "KB-TEST-001"

    for bad_patch in (
        {"id": "bad-id"},
        {"pattern": "(unclosed"},
        {"engine": "gamma"},
        {"severity": "OOPS"},
        {"extract": ["nope"]},
        {"pattern_verified": True},  # verified without source
        {"unknown_field": 1},
        {"cause": {"ko": "only"}},
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.safe_dump([{**base, **bad_patch}]), encoding="utf-8")
        with pytest.raises(ValueError):
            load_kb_file(bad)


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    from wintersar.diagnose import kb_loader

    d = tmp_path / "kb"
    d.mkdir()
    entry = {
        "id": "KB-DUP-001",
        "engine": "any",
        "pattern": "dup",
        "cause": {"ko": "a", "en": "b"},
        "fix": {"ko": "c", "en": "d"},
        "severity": "WARN",
        "pattern_verified": False,
    }
    (d / "a.yaml").write_text(yaml.safe_dump([entry]), encoding="utf-8")
    (d / "b.yaml").write_text(yaml.safe_dump([entry]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        kb_loader.load_kb(d)
