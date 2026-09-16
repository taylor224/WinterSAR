"""burst2safe wrapper: verified CLI flags, ENV-006 absence, credentials, per-date SAFE build."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.engines_isce2.conftest import FakeBurst2Safe
from wintersar.engines import burst2safe as b2s

pytestmark = pytest.mark.engine


def test_build_argv_exact_flags(tmp_path: Path) -> None:
    argv = b2s.build_argv(
        [
            "S1_109903_IW2_20240101T092000_VV_AB12-BURST",
            "S1_109904_IW2_20240101T092000_VV_CD34-BURST",
        ],
        tmp_path,
        polarizations=["VV"],
        swaths=["IW2"],
        mode="IW",
        min_bursts=1,
        all_anns=True,
        keep_files=True,
    )
    assert argv == [
        "burst2safe",
        "S1_109903_IW2_20240101T092000_VV_AB12-BURST",
        "S1_109904_IW2_20240101T092000_VV_CD34-BURST",
        "--output-dir",
        str(tmp_path),
        "--pols",
        "VV",
        "--swaths",
        "IW2",
        "--mode",
        "IW",
        "--min-bursts",
        "1",
        "--all-anns",
        "--keep-files",
    ]
    assert b2s.build_argv(["g"], tmp_path) == ["burst2safe", "g", "--output-dir", str(tmp_path)]
    with pytest.raises(ValueError):
        b2s.build_argv([], tmp_path)


def test_absent_is_env_006_optional(engines_absent: None) -> None:
    assert b2s.detect_version() is None
    findings = b2s.check_install()
    assert [f.rule_id for f in findings] == ["ENV-006"]
    assert findings[0].severity == "WARN" and findings[0].params["package"] == "burst2safe"


def test_credentials_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NETRC", str(tmp_path / "absent.netrc"))
    assert not b2s.credentials_available()
    monkeypatch.setenv("EARTHDATA_USERNAME", "u")
    assert not b2s.credentials_available()  # both user and password are needed
    monkeypatch.setenv("EARTHDATA_PASSWORD", "p")
    assert b2s.credentials_available()
    monkeypatch.delenv("EARTHDATA_USERNAME")
    monkeypatch.setenv("EARTHDATA_TOKEN", "t" * 20)
    assert b2s.credentials_available()
    monkeypatch.delenv("EARTHDATA_TOKEN")
    (tmp_path / "absent.netrc").write_text(
        "machine urs.earthdata.nasa.gov login u password p\n", encoding="utf-8"
    )
    assert b2s.credentials_available()
    monkeypatch.setattr(b2s, "python_module_version", lambda *_a, **_k: "2.0.3")
    monkeypatch.setenv("NETRC", str(tmp_path / "none"))
    findings = b2s.check_install()
    assert [f.rule_id for f in findings] == ["ISCE2-005"]


def test_build_safes_skips_existing_and_reports_failures(tmp_path: Path) -> None:
    runner = FakeBurst2Safe(fail_dates={"20240125"})
    granules = {
        "2024-01-01": ["S1_109903_IW2_20240101T092000_VV_AB12-BURST"],
        "2024-01-13": ["S1_109903_IW2_20240113T092000_VV_AB12-BURST"],
        "2024-01-25": ["S1_109903_IW2_20240125T092000_VV_AB12-BURST"],
    }
    out = tmp_path / "SLC"
    safes, findings = b2s.build_safes(
        granules, out, tmp_path / "logs", polarizations=["VV"], swaths=["IW2"], runner=runner
    )
    assert sorted(safes) == ["2024-01-01", "2024-01-13"]
    assert all(p.name.endswith(".SAFE") for p in safes.values())
    assert [f.rule_id for f in findings] == ["ISCE2-004"]
    assert findings[0].params["date"] == "2024-01-25" and findings[0].severity == "FAIL"
    assert len(runner.calls) == 3 and runner.calls[0][-4:] == ["--pols", "VV", "--swaths", "IW2"]
    # second call: existing SAFEs are not rebuilt (PERF-02)
    runner2 = FakeBurst2Safe()
    safes2, findings2 = b2s.build_safes(granules, out, tmp_path / "logs", runner=runner2)
    assert sorted(safes2) == ["2024-01-01", "2024-01-13", "2024-01-25"] and not findings2
    assert len(runner2.calls) == 1
    assert len(b2s.list_safes(out)) == 3
