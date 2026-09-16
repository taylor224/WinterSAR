"""smallbaselineApp.cfg generation (ADR-0021, PERF-09)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wintersar.engines import mintpy_template as tpl
from wintersar.util.sysinfo import MachineSpec

KEYS_SNAPSHOT = (
    Path(__file__).resolve().parents[2] / "fixtures" / "mintpy" / "smallbaselineApp_keys.txt"
)


def _machine(cores: int, mem: float) -> MachineSpec:
    return MachineSpec(
        cores=cores, memory_gb=mem, gpu=False, gpu_name=None, python="3.11", os="test"
    )


def test_every_emitted_key_exists_in_official_template() -> None:
    official = {
        ln.strip() for ln in KEYS_SNAPSHOT.read_text().splitlines() if ln.startswith("mintpy.")
    }
    assert set(tpl.TEMPLATE_KEYS) <= official, sorted(set(tpl.TEMPLATE_KEYS) - official)
    for pattern_set in (tpl.HYP3_LOAD_PATTERNS, tpl.ISCE_TOPSSTACK_LOAD_PATTERNS):
        assert set(pattern_set) <= official


def test_step_list_matches_mintpy_1_6_4() -> None:
    assert tpl.STEP_LIST[:6] == (
        "load_data",
        "modify_network",
        "reference_point",
        "quick_overview",
        "correct_unwrap_error",
        "invert_network",
    )
    assert tpl.STEP_LIST[6:15] == (
        "correct_LOD",
        "correct_SET",
        "correct_ionosphere",
        "correct_troposphere",
        "deramp",
        "correct_topography",
        "residual_RMS",
        "reference_date",
        "velocity",
    )
    assert tpl.STEP_LIST[15:] == ("geocode", "google_earth", "hdfeos5")


def test_hyp3_template_exact_keys_and_values(tmp_path: Path) -> None:
    data = tmp_path / "hyp3"
    weather = tmp_path / "cache" / "weather" / "ERA5"
    entries = tpl.build_template(
        processor="hyp3",
        data_dir=data,
        timeseries={
            "engine": "mintpy",
            "reference_point": (37.55, 126.95),
            "troposphere": "era5",
            "deramp": "linear",
            "unwrap_error_correction": "phase_closure",
            "coherence_threshold": 0.7,
        },
        selection={"max_temporal_baseline_days": 48, "max_perp_baseline_m": 150.0},
        machine=_machine(8, 32.0),
        weather_dir=weather,
    )
    d = str(data.resolve())
    assert entries == {
        "mintpy.compute.maxMemory": "25.6",
        "mintpy.compute.cluster": "local",
        "mintpy.compute.numWorker": "7",
        "mintpy.load.processor": "hyp3",
        "mintpy.load.unwFile": f"{d}/*/*/*_unw_phase.tif",
        "mintpy.load.corFile": f"{d}/*/*/*_corr.tif",
        "mintpy.load.connCompFile": f"{d}/*/*/*_conncomp.tif",
        "mintpy.load.demFile": f"{d}/*/*/*_dem.tif",
        "mintpy.load.incAngleFile": f"{d}/*/*/*_lv_theta.tif",
        "mintpy.load.azAngleFile": f"{d}/*/*/*_lv_phi.tif",
        "mintpy.load.waterMaskFile": f"{d}/*/*/*_water_mask.tif",
        "mintpy.load.updateMode": "yes",
        "mintpy.network.tempBaseMax": "48",
        "mintpy.network.perpBaseMax": "150",
        "mintpy.reference.lalo": "37.550000,126.950000",
        "mintpy.troposphericDelay.method": "pyaps",
        "mintpy.troposphericDelay.weatherModel": "ERA5",
        "mintpy.troposphericDelay.weatherDir": str(weather.resolve()),
        "mintpy.deramp": "linear",
        "mintpy.unwrapError.method": "phase_closure",
        "mintpy.network.coherenceBased": "yes",
        "mintpy.network.minCoherence": "0.70",
        "mintpy.geocode": "yes",
        "mintpy.save.kmz": "yes",
    }


def test_template_variants(tmp_path: Path) -> None:
    e = tpl.build_template(
        processor="hyp3",
        data_dir=tmp_path,
        timeseries={
            "reference_point": "auto_recommend",
            "troposphere": "none",
            "deramp": "no",
            "unwrap_error_correction": "no",
        },
        machine=_machine(2, 8.0),
        patterns={"unwFile": "*/*/*_unw_phase_clip.tif", "corFile": "*/*/*_corr_clip.tif"},
    )
    assert e["mintpy.reference.lalo"] == "auto"
    assert (
        e["mintpy.troposphericDelay.method"] == "no"
        and "mintpy.troposphericDelay.weatherModel" not in e
    )
    assert e["mintpy.deramp"] == "no" and e["mintpy.unwrapError.method"] == "no"
    assert e["mintpy.compute.cluster"] == "none" and e["mintpy.compute.numWorker"] == "auto"
    assert (
        e["mintpy.load.unwFile"].endswith("_unw_phase_clip.tif") and "mintpy.load.demFile" not in e
    )
    g = tpl.build_template(
        processor="hyp3",
        data_dir=tmp_path,
        timeseries={"troposphere": "gacos"},
        gacos_dir=tmp_path / "GACOS",
        clip_suffix="_clip",
    )
    assert g["mintpy.troposphericDelay.method"] == "gacos" and g[
        "mintpy.troposphericDelay.gacosDir"
    ] == str((tmp_path / "GACOS").resolve())
    assert g["mintpy.load.demFile"].endswith("*_dem_clip.tif")
    h = tpl.build_template(
        processor="hyp3", data_dir=tmp_path, timeseries={"troposphere": "height_correlation"}
    )
    assert h["mintpy.troposphericDelay.method"] == "height_correlation"


def test_isce_topsstack_patterns(tmp_path: Path) -> None:
    e = tpl.build_template(processor="isce", data_dir=tmp_path / "stack", timeseries={})
    root = str((tmp_path / "stack").resolve())
    assert e["mintpy.load.processor"] == "isce"
    assert e["mintpy.load.metaFile"] == f"{root}/reference/IW*.xml"
    assert e["mintpy.load.baselineDir"] == f"{root}/baselines"
    assert e["mintpy.load.unwFile"] == f"{root}/merged/interferograms/*/filt_*.unw"
    assert e["mintpy.load.connCompFile"] == f"{root}/merged/interferograms/*/filt_*.unw.conncomp"
    assert e["mintpy.load.lookupYFile"] == f"{root}/merged/geom_reference/lat.rdr"
    assert e["mintpy.load.shadowMaskFile"] == f"{root}/merged/geom_reference/shadowMask.rdr"
    with pytest.raises(ValueError, match="unsupported processor"):
        tpl.build_template(processor="gamma", data_dir=tmp_path)


@pytest.mark.parametrize(
    ("cores", "mem", "over_cores", "over_mem", "expected"),
    [
        (
            8,
            32.0,
            None,
            None,
            {
                "mintpy.compute.maxMemory": "25.6",
                "mintpy.compute.cluster": "local",
                "mintpy.compute.numWorker": "7",
            },
        ),
        (
            16,
            64.0,
            4,
            10.0,
            {
                "mintpy.compute.maxMemory": "10.0",
                "mintpy.compute.cluster": "local",
                "mintpy.compute.numWorker": "3",
            },
        ),
        (
            2,
            8.0,
            None,
            None,
            {
                "mintpy.compute.maxMemory": "6.4",
                "mintpy.compute.cluster": "none",
                "mintpy.compute.numWorker": "auto",
            },
        ),
        (
            1,
            0.5,
            None,
            None,
            {
                "mintpy.compute.maxMemory": "1.0",
                "mintpy.compute.cluster": "none",
                "mintpy.compute.numWorker": "auto",
            },
        ),
    ],
)
def test_compute_settings_perf09(
    cores: int, mem: float, over_cores: int | None, over_mem: float | None, expected: dict[str, str]
) -> None:
    assert (
        tpl.compute_settings(_machine(cores, mem), cores=over_cores, memory_gb=over_mem) == expected
    )


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    with pytest.raises(tpl.TemplateKeyError):
        tpl.build_template(processor="hyp3", data_dir=tmp_path, extra={"mintpy.load.unwfile": "x"})
    with pytest.raises(tpl.TemplateKeyError):
        tpl.render_template({"mintpy.nope": "1"})
    with pytest.raises(tpl.TemplateKeyError):
        tpl.load_settings("hyp3", tmp_path, patterns={"mintpy.network.tempBaseMax": "12"})
    e = tpl.build_template(
        processor="hyp3",
        data_dir=tmp_path,
        extra={"mintpy.save.hdfEos5": True, "mintpy.plot": False, "mintpy.compute.numWorker": 3},
    )
    assert (
        e["mintpy.save.hdfEos5"] == "yes"
        and e["mintpy.plot"] == "no"
        and e["mintpy.compute.numWorker"] == "3"
    )


def test_render_write_parse_roundtrip(tmp_path: Path) -> None:
    entries = tpl.build_template(
        processor="hyp3",
        data_dir=tmp_path,
        timeseries={"reference_point": [37.5, 127.0]},
        machine=_machine(4, 16),
    )
    text = tpl.render_template(entries, "ko")
    assert text.startswith("# vim: set filetype=cfg:\n")
    assert "wintersar가 생성한 MintPy 템플릿" in text
    assert "mintpy.reference.lalo" in text
    assert tpl.parse_template(text) == entries
    out = tpl.write_template(tmp_path / "wintersar_mintpy.cfg", entries, "en")
    assert "generated by wintersar" in out.read_text(encoding="utf-8")
    assert "\n\n\n" not in text


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (
            {
                "mintpy.troposphericDelay.method": "pyaps",
                "mintpy.troposphericDelay.weatherModel": "ERA5",
                "mintpy.deramp": "linear",
            },
            "timeseries_ERA5_ramp_demErr.h5",
        ),
        (
            {
                "mintpy.troposphericDelay.method": "no",
                "mintpy.deramp": "no",
                "mintpy.topographicResidual": "no",
            },
            "timeseries.h5",
        ),
        (
            {"mintpy.troposphericDelay.method": "gacos", "mintpy.deramp": "quadratic"},
            "timeseries_GACOS_ramp_demErr.h5",
        ),
        (
            {"mintpy.troposphericDelay.method": "height_correlation", "mintpy.deramp": "no"},
            "timeseries_tropHgt_demErr.h5",
        ),
        ({}, "timeseries_ERA5_demErr.h5"),
    ],
)
def test_expected_timeseries_filename(entries: dict[str, str], expected: str) -> None:
    assert tpl.expected_timeseries_filename(entries) == expected


def test_invalid_config_values(tmp_path: Path) -> None:
    for bad in (
        {"troposphere": "merra"},
        {"deramp": "cubic"},
        {"unwrap_error_correction": "magic"},
    ):
        with pytest.raises(ValueError):
            tpl.build_template(processor="hyp3", data_dir=tmp_path, timeseries=bad)
