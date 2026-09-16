"""select/dem: content-addressed DEM cache (PERF-02) and sardem detection (ENV-006)."""

from __future__ import annotations

import importlib.machinery
import sys
import types
from pathlib import Path

import pytest

from wintersar import i18n
from wintersar.select import dem as demmod

AOI = "POLYGON((126.90 37.50,127.00 37.50,127.00 37.60,126.90 37.60,126.90 37.50))"


class _Recorder:
    """Fake fetcher: writes a small file and records the calls."""

    __name__ = "fake_fetcher"

    def __init__(self, payload: bytes = b"DEM") -> None:
        self.calls: list[tuple[demmod.BBox, str, Path, Path]] = []
        self.payload = payload

    def __call__(self, bbox: demmod.BBox, source: str, out: Path, tiles: Path) -> Path:
        self.calls.append((bbox, source, out, tiles))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.payload)
        return out


def test_bbox_from_wkt_buffer_and_clamp() -> None:
    assert demmod.bbox_from_wkt(AOI) == pytest.approx((126.90, 37.50, 127.00, 37.60))
    left, b, r, t = demmod.bbox_from_wkt(AOI, buffer_deg=0.05)
    assert (left, b, r, t) == pytest.approx((126.85, 37.45, 127.05, 37.65))
    _, b2, _, t2 = demmod.bbox_from_wkt(
        "POLYGON((0 -89.99,1 -89.99,1 89.99,0 89.99,0 -89.99))", 1.0
    )
    assert (b2, t2) == (-90.0, 90.0)
    with pytest.raises(ValueError):
        demmod.bbox_from_wkt("POLYGON EMPTY")


def test_cache_key_is_stable_under_rounding_and_distinct_per_source() -> None:
    bbox = (126.85, 37.45, 127.05, 37.65)
    k1 = demmod.dem_cache_key(bbox, "copernicus_glo30")
    k2 = demmod.dem_cache_key((126.850004, 37.45, 127.05, 37.649996), "copernicus_glo30")
    assert k1 == k2
    assert demmod.dem_cache_key(bbox, "srtm1") != k1
    assert demmod.dem_cache_key((126.85, 37.45, 127.06, 37.65), "copernicus_glo30") != k1
    with pytest.raises(ValueError):
        demmod.normalize_bbox((1.0, 2.0, 1.0, 3.0))


def test_get_dem_fetches_once_then_hits_cache(tmp_path: Path) -> None:
    fetcher = _Recorder()
    p1 = demmod.get_dem(AOI, tmp_path, fetcher=fetcher)
    assert p1.exists() and p1.read_bytes() == b"DEM"
    assert p1.parent == tmp_path / "dem"
    assert p1.name.startswith("copernicus_glo30_") and p1.suffix == ".tif"
    assert len(fetcher.calls) == 1
    bbox, source, out, tiles = fetcher.calls[0]
    assert source == "copernicus_glo30"
    assert bbox == pytest.approx((126.85, 37.45, 127.05, 37.65))
    assert out != p1 and not out.exists()  # temp file renamed atomically
    assert tiles == tmp_path / "dem" / "tiles"

    p2 = demmod.get_dem(AOI, tmp_path, fetcher=fetcher)
    assert p2 == p1
    assert len(fetcher.calls) == 1, "cache hit must not fetch again (PERF-02)"

    prov = demmod.dem_provenance(p1)
    assert prov is not None
    assert prov["source"] == "copernicus_glo30" and prov["sardem_data_source"] == "COP"
    assert prov["fetcher"] == "fake_fetcher"
    assert "Copernicus" in prov["attribution"] or "DLR" in prov["attribution"]

    p3 = demmod.get_dem(AOI, tmp_path, source="srtm1", fetcher=fetcher)
    assert p3 != p1 and len(fetcher.calls) == 2
    assert fetcher.calls[1][1] == "srtm1"


def test_get_dem_failed_fetch_leaves_no_cache_entry(tmp_path: Path) -> None:
    def broken(bbox: demmod.BBox, source: str, out: Path, tiles: Path) -> Path:
        return out  # never written

    with pytest.raises(demmod.DemFetchError):
        demmod.get_dem(AOI, tmp_path, fetcher=broken)
    assert not list((tmp_path / "dem").glob("*.tif"))
    assert not list((tmp_path / "dem").glob("*.part*"))


def test_get_dem_unknown_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        demmod.get_dem(AOI, tmp_path, source="moon_dem", fetcher=_Recorder())
    assert "moon_dem" in str(exc.value)


def test_env006_finding_renders_in_both_languages() -> None:
    f = demmod.env006_finding()
    assert f.rule_id == "ENV-006" and f.params["package"] == "sardem"
    for lang in ("ko", "en"):
        cause = i18n.t(f.message_key, lang, **f.params)
        fix = i18n.t(f.fix_key or "", lang, **f.params)
        assert "sardem" in cause and "wintersar[dem]" in fix
        assert "{" not in cause and "{" not in fix


@pytest.mark.skipif(demmod.sardem_available(), reason="sardem installed in this environment")
def test_missing_sardem_maps_to_env006(tmp_path: Path) -> None:
    assert demmod.check_install()[0].rule_id == "ENV-006"
    assert demmod.check_install()[0].severity == "WARN"
    with pytest.raises(demmod.DemNotInstalledError) as exc:
        demmod.get_dem(AOI, tmp_path)
    assert not list(tmp_path.rglob("*.tif"))
    assert exc.value.finding.rule_id == "ENV-006"
    assert exc.value.finding.severity == "FAIL"
    assert "sardem" in str(exc.value)


def test_check_install_ok_when_sardem_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demmod, "sardem_available", lambda: True)
    assert demmod.check_install() == []


def test_sardem_fetcher_calls_main_with_verified_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake ``sardem.dem.main`` with the upstream keyword names (source comment in dem.py)."""
    calls: list[dict[str, object]] = []

    def fake_main(**kwargs: object) -> None:
        calls.append(kwargs)
        Path(str(kwargs["output_name"])).write_bytes(b"x" * 16)

    pkg = types.ModuleType("sardem")
    pkg.__spec__ = importlib.machinery.ModuleSpec("sardem", None, is_package=True)
    pkg.__path__ = []  # type: ignore[attr-defined]
    sub = types.ModuleType("sardem.dem")
    sub.main = fake_main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sardem", pkg)
    monkeypatch.setitem(sys.modules, "sardem.dem", sub)
    assert demmod.sardem_available()

    out = tmp_path / "dem" / "x.tif"
    got = demmod.sardem_fetcher(
        (126.85, 37.45, 127.05, 37.65), "copernicus_glo30", out, tmp_path / "tiles"
    )
    assert got == out and out.exists()
    kw = calls[0]
    assert kw["bbox"] == (126.85, 37.45, 127.05, 37.65)
    assert kw["data_source"] == "COP"
    assert kw["output_format"] == "GTiff" and kw["output_type"] == "float32"
    assert kw["cache_dir"] == str(tmp_path / "tiles")
    assert (tmp_path / "tiles").is_dir()

    # end-to-end through get_dem with the default fetcher
    p = demmod.get_dem(AOI, tmp_path / "cache")
    assert p.exists() and len(calls) == 2
    assert demmod.get_dem(AOI, tmp_path / "cache") == p and len(calls) == 2

    def silent_main(**kwargs: object) -> None:
        calls.append(kwargs)

    sub.main = silent_main  # type: ignore[attr-defined]
    with pytest.raises(demmod.DemFetchError):
        demmod.sardem_fetcher((0.0, 0.0, 1.0, 1.0), "srtm1", tmp_path / "none.tif", tmp_path / "t")
