"""Shared unwrap-adapter pieces: result contract, parameter mapping, masks, i18n parity."""

from __future__ import annotations

import dataclasses
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from wintersar import i18n
from wintersar.engines import _unwrap_common as uc
from wintersar.io.schemas import Finding

SRC = Path(__file__).resolve().parents[3] / "src" / "wintersar" / "engines"


# ------------------------------------------------------------------ contract


def test_unwrap_result_field_names_are_the_structural_contract() -> None:
    names = [f.name for f in dataclasses.fields(uc.UnwrapResult)]
    assert names == ["unw", "conncomp", "stats"]
    r = uc.UnwrapResult(unw=np.zeros((2, 3), np.float32), conncomp=np.zeros((2, 3), np.uint32))
    assert r.shape == (2, 3) and r.stats == {}


def test_unwrap_error_carries_finding_and_translated_message() -> None:
    err = uc.UnwrapError.from_rule("UNW-004", cost="topo")
    assert isinstance(err.finding, Finding)
    assert err.finding.rule_id == "UNW-004"
    assert err.finding.message_key == "engines_unwrap.UNW-004.cause"
    assert err.finding.fix_key == "engines_unwrap.UNW-004.fix"
    assert "topo" in str(err)
    assert issubclass(uc.StackOnlyEngineError, NotImplementedError)


# ------------------------------------------------------------------ parameters


def test_unwrap_cfg_flattens_nested_section_and_fills_defaults() -> None:
    flat = uc.unwrap_cfg({"unwrap": {"cost": "smooth"}, "_out_dir": "/x", "nlooks": 4})
    assert flat["cost"] == "smooth"
    assert flat["init"] == "mcf"  # UnwrapCfg default
    assert flat["coherence_threshold"] == 0.3
    assert flat["tiles"] == "auto"
    assert flat["_out_dir"] == "/x" and flat["nlooks"] == 4
    # top-level keys win over the nested section
    assert uc.unwrap_cfg({"unwrap": {"cost": "smooth"}, "cost": "defo"})["cost"] == "defo"


@pytest.mark.parametrize(
    ("cfg", "attrs", "expected"),
    [
        ({"nlooks": 12}, None, (12.0, "nlooks")),
        ({"looks": [4, 3]}, None, (12.0, "looks")),
        ({"looks": 5}, None, (5.0, "looks")),
        ({"looks": "auto"}, {"nlooks": 7}, (7.0, "attrs")),
        ({}, {}, (1.0, "default")),
    ],
)
def test_resolve_nlooks_precedence(cfg: dict, attrs: dict | None, expected: tuple) -> None:
    assert uc.resolve_nlooks(cfg, attrs) == expected


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, 1),  # neither key
        ({"nproc_per_igram": 3}, 3),  # UnwrapEngineBase.run / raw UnwrapCfg field
        ({"nproc": 4}, 4),  # unwrap scheduler (_backend_params)
        ({"nproc": 4, "nproc_per_igram": 1}, 4),  # planned value wins (ADR-0047)
        ({"nproc": 0}, 1),  # never below 1
        ({"nproc": None, "nproc_per_igram": 2}, 2),
        ({"nproc": "x", "nproc_per_igram": 2}, 2),
    ],
)
def test_resolve_nproc_precedence(cfg: dict, expected: int) -> None:
    assert uc.resolve_nproc(cfg) == expected


def test_engine_log_accepts_both_scheduler_and_run_spellings(tmp_path: Path) -> None:
    assert uc.engine_log({}, "snaphu") is None
    # scheduler: a directory -> <log_dir>/<engine>.log
    log = uc.engine_log({"_log_dir": str(tmp_path / "logs")}, "snaphu")
    assert log is not None and log.path == tmp_path / "logs" / "snaphu.log"
    log.write("hello")
    assert "hello" in log.path.read_text(encoding="utf-8")
    # run(): an explicit path wins
    both = uc.engine_log(
        {"_log_dir": str(tmp_path / "logs"), "_log_path": str(tmp_path / "x.log")}, "snaphu"
    )
    assert both is not None and both.path == tmp_path / "x.log"


def test_scratch_dir_accepts_both_scheduler_and_run_spellings(tmp_path: Path) -> None:
    # scheduler: one stage directory + the pair key -> a per-pair subdirectory, kept
    path, is_temp = uc.scratch_dir(
        {"_tile_dir": str(tmp_path / "tiles"), "_pair": "20240101_20240113"}, "p-"
    )
    assert path == tmp_path / "tiles" / "20240101_20240113"
    assert path.is_dir() and is_temp is False
    # without a pair key the stage directory is used as-is
    path2, _ = uc.scratch_dir({"_tile_dir": str(tmp_path / "tiles")}, "p-")
    assert path2 == tmp_path / "tiles"
    # run(): the per-pair _scratch_dir wins over the stage directory
    path3, _ = uc.scratch_dir(
        {"_tile_dir": str(tmp_path / "tiles"), "_pair": "a_b", "_scratch_dir": str(tmp_path / "s")},
        "p-",
    )
    assert path3 == tmp_path / "s"
    # nothing given -> a temporary directory the caller must clean up
    path4, is_temp4 = uc.scratch_dir({}, "wintersar-test-")
    assert is_temp4 is True and path4.is_dir() and path4.name.startswith("wintersar-test-")
    path4.rmdir()


def test_resolve_tiles_auto_is_single_tile() -> None:
    spec = uc.resolve_tiles({"tiles": "auto"}, (64, 64))
    assert spec.ntiles == (1, 1) and spec.overlap_px == (0, 0) and not spec.tiled


def test_resolve_tiles_from_fraction_and_minimum() -> None:
    spec = uc.resolve_tiles(
        {"tiles": {"rows": 2, "cols": 4, "overlap": 0.25, "min_overlap_px": 4}}, (64, 64)
    )
    # rows: tile 32 px -> 25% = 8 px; cols: tile 16 px -> 25% = 4 px (== minimum)
    assert spec.ntiles == (2, 4)
    assert spec.overlap_px == (8, 4)
    assert not spec.capped


def test_resolve_tiles_caps_overlap_below_tile_size() -> None:
    spec = uc.resolve_tiles({"tiles": {"rows": 2, "cols": 1, "min_overlap_px": 200}}, (64, 64))
    assert spec.capped
    assert spec.requested_overlap_px == (200, 0)
    assert spec.overlap_px == (31, 0)


def test_resolve_tiles_explicit_ntiles_from_scheduler_wins() -> None:
    spec = uc.resolve_tiles(
        {"tiles": {"rows": 9, "cols": 9}, "ntiles": [2, 3], "tile_overlap": 5}, (60, 90)
    )
    assert spec.ntiles == (2, 3) and spec.overlap_px == (5, 5)
    spec2 = uc.resolve_tiles({"ntiles": (2, 2), "tile_overlap": (3, 7)}, (60, 90))
    assert spec2.overlap_px == (3, 7)


# ------------------------------------------------------------------ arrays


def test_build_masked_combines_threshold_mask_and_nan() -> None:
    ig = np.zeros((2, 3), np.float32)
    ig[0, 0] = np.nan
    coh = np.array([[0.9, 0.1, 0.9], [0.9, np.nan, 0.9]], np.float32)
    mask = np.zeros((2, 3), bool)
    mask[1, 2] = True
    m = uc.build_masked(ig, coh, mask, 0.3)
    assert m.tolist() == [[True, True, False], [False, True, True]]
    m2 = uc.build_masked(ig, coh, None, 0.3, use_coherence=False)
    assert m2.tolist() == [[True, False, False], [False, True, False]]


def test_complex_and_masking_helpers() -> None:
    ig = np.array([[0.5, -0.5]], np.float32)
    coh = np.array([[1.0, 0.5]], np.float32)
    masked = np.array([[False, True]])
    c = uc.to_complex64(ig, coh, masked)
    assert c.dtype == np.complex64
    assert np.isclose(np.angle(c[0, 0]), 0.5) and c[0, 1] == 0
    assert uc.clean_coherence(coh, masked).tolist() == [[1.0, 0.0]]
    unw = uc.nan_masked(np.array([[1.0, 2.0]]), masked)
    assert unw.dtype == np.float32 and np.isnan(unw[0, 1]) and unw[0, 0] == 1.0
    cc = uc.conncomp_masked(np.array([[3, 3]]), masked)
    assert cc.dtype == np.uint32 and cc.tolist() == [[3, 0]]
    st = uc.conncomp_stats(np.array([[1, 1, 2, 0]], np.uint32))
    assert st["n_conncomp"] == 2 and st["conncomp_max_label"] == 2
    assert st["largest_conncomp_fraction"] == 0.5 and st["labelled_fraction"] == 0.75


def test_single_tile_memory_model() -> None:
    assert uc.single_tile_memory_mb(2_000_000, 100.0) == 200.0


# ------------------------------------------------------------------ log / parse_log


def test_engine_log_masks_home(tmp_path: Path) -> None:
    log = uc.EngineLog(tmp_path / "x" / "e.log")
    log.write(f"path={Path.home()}/secret token=abcdefghijklmnop")
    log.write_raw("raw line\n")
    text = log.path.read_text()
    assert str(Path.home()) not in text and "abcdefghijklmnop" not in text
    assert "raw line" in text and " INFO " in text


def test_parse_log_delegates_lazily_to_diagnose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wintersar.engines.snaphu import SnaphuEngine

    log = tmp_path / "snaphu.log"
    log.write_text("x")
    calls: list = []
    api = types.ModuleType("wintersar.diagnose.api")

    def diagnose_logs(path: Path, engine: str | None = None) -> list[Finding]:
        calls.append((path, engine))
        return [Finding(rule_id="KB-SNAPHU-001", severity="FAIL", message_key="k")]

    api.diagnose_logs = diagnose_logs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wintersar.diagnose.api", api)
    out = SnaphuEngine().parse_log(log)
    assert [f.rule_id for f in out] == ["KB-SNAPHU-001"]
    assert calls == [(log, "snaphu")]
    from wintersar.engines.spurt import SpurtEngine
    from wintersar.engines.tophu import TophuEngine

    TophuEngine().parse_log(log)
    assert calls[-1] == (log, "snaphu")  # tophu drives SNAPHU -> SNAPHU knowledge base
    SpurtEngine().parse_log(log)
    assert calls[-1] == (log, None)  # no spurt KB yet -> engine-agnostic
    monkeypatch.setitem(sys.modules, "wintersar.diagnose.api", None)
    assert SnaphuEngine().parse_log(log) == []


def test_parse_log_with_real_diagnose_module_accepts_all_three_engines(tmp_path: Path) -> None:
    pytest.importorskip("wintersar.diagnose.api")
    from wintersar.engines.snaphu import SnaphuEngine
    from wintersar.engines.spurt import SpurtEngine
    from wintersar.engines.tophu import TophuEngine

    log = tmp_path / "snaphu.log"
    log.write_text("snaphu: Exceeded maximum number of secondary arcs\n", encoding="utf-8")
    for eng in (SnaphuEngine(), TophuEngine(), SpurtEngine()):
        findings = eng.parse_log(log)  # must not raise for engines without a KB
        assert isinstance(findings, list)


# ------------------------------------------------------------------ i18n


def _namespace_keys(lang: str) -> set[str]:
    return {k for k in i18n.load_catalog(lang) if k.startswith("engines_unwrap.")}


def test_i18n_engines_unwrap_keys_identical_in_ko_and_en() -> None:
    ko, en = _namespace_keys("ko"), _namespace_keys("en")
    assert ko and ko == en, f"ko-only={sorted(ko - en)} en-only={sorted(en - ko)}"


def test_every_rule_id_used_in_code_has_cause_and_fix() -> None:
    used: set[str] = set()
    for f in ("_unwrap_common.py", "snaphu.py", "tophu.py", "spurt.py"):
        used |= set(re.findall(r'"(UNW-\d{3})"', (SRC / f).read_text(encoding="utf-8")))
    assert used
    for lang in ("ko", "en"):
        cat = i18n.load_catalog(lang)
        for rid in sorted(used):
            assert f"engines_unwrap.{rid}.cause" in cat, (lang, rid)
            assert f"engines_unwrap.{rid}.fix" in cat, (lang, rid)
    # cause → fix render for both languages with the finding params
    f = uc.make_finding("UNW-003", engine="snaphu", returncode=2, log="/tmp/x.log")
    assert "snaphu" in i18n.t(f.message_key, "ko", **f.params)
    assert "snaphu" in i18n.t(f.message_key, "en", **f.params)
    assert f.fix_key is not None and i18n.t(f.fix_key, "en") != f.fix_key
