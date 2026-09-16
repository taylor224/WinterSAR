"""``wintersar research repr-phase|stitch|synth|experiment`` (plan §4.5: every command
honours --json / --lang)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from wintersar.cli import app
from wintersar.io.igrams import load_igram_stack

runner = CliRunner()


def _json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_synth_igrams_then_repr_phase_json(tmp_path: Path):
    npz = tmp_path / "igrams.npz"
    payload = _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "synth",
                "--out",
                str(npz),
                "--n-dates",
                "5",
                "--shape",
                "36",
                "36",
                "--looks",
                "16",
            ],
        )
    )
    assert payload["ok"] and payload["command"] == "research synth"
    assert payload["data"]["n_dates"] == 5 and npz.exists()
    st = load_igram_stack(npz)
    assert st.n_pairs == payload["data"]["n_pairs"] and "unw_true" in st.truth
    out = tmp_path / "repr.npz"
    res = _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "repr-phase",
                "--igram",
                str(npz),
                "--method",
                "coh_weighted",
                "--factor",
                "3",
                "--out",
                str(out),
                "-p",
                "p=2",
            ],
        )
    )
    assert res["ok"] and res["data"]["shape"] == [12, 12]
    assert res["data"]["params"] == {"p": 2} and res["data"]["phase_rmse_rad"] < 1.0
    with np.load(out) as z:
        assert z["repr_complex"].shape == (12, 12) and str(z["method"]) == "coh_weighted"


def test_repr_phase_phase_link_infers_pair_from_stack(tmp_path: Path):
    igrams = tmp_path / "igrams.npz"
    slc = tmp_path / "slc.npz"
    _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "synth",
                "--out",
                str(igrams),
                "--n-dates",
                "5",
                "--shape",
                "32",
                "32",
            ],
        )
    )
    _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "synth",
                "--kind",
                "slc",
                "--out",
                str(slc),
                "--n-dates",
                "5",
                "--shape",
                "32",
                "32",
            ],
        )
    )
    res = _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "repr-phase",
                "--igram",
                str(igrams),
                "--stack",
                str(slc),
                "--method",
                "phase_link",
                "--factor",
                "4",
                "--index",
                "1",
                "--out",
                str(tmp_path / "pl.npz"),
            ],
        )
    )
    assert res["data"]["params"]["pair"] == [
        0,
        2,
    ]  # pair index 1 of a 48-day SBAS network = dates 0,2
    res2 = runner.invoke(
        app,
        [
            "--json",
            "research",
            "repr-phase",
            "--igram",
            str(igrams),
            "--stack",
            str(slc),
            "--method",
            "phase_link",
            "--factor",
            "4",
            "--pair",
            "1",
            "3",
            "--out",
            str(tmp_path / "pl2.npz"),
        ],
    )
    assert _json(res2)["data"]["params"]["pair"] == [1, 3]


def test_repr_phase_errors_are_findings(tmp_path: Path):
    igrams = tmp_path / "igrams.npz"
    _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "synth",
                "--out",
                str(igrams),
                "--n-dates",
                "4",
                "--shape",
                "16",
                "16",
            ],
        )
    )
    res = runner.invoke(
        app,
        [
            "--json",
            "research",
            "repr-phase",
            "--igram",
            str(igrams),
            "--method",
            "shp",
            "--out",
            str(tmp_path / "x.npz"),
        ],
    )
    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert not payload["ok"] and payload["findings"][0]["rule_id"] == "RES-003"
    res = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "research",
            "repr-phase",
            "--igram",
            str(igrams),
            "--method",
            "nope",
            "--out",
            str(tmp_path / "x.npz"),
        ],
    )
    assert res.exit_code == 1 and "Unknown method" in res.output


def test_synth_tiles_then_stitch_both_methods(tmp_path: Path):
    tiles = tmp_path / "tiles.npz"
    payload = _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "synth",
                "--kind",
                "tiles",
                "--out",
                str(tiles),
                "--shape",
                "60",
                "60",
                "--rows",
                "2",
                "--cols",
                "3",
                "--overlap",
                "6",
                "--looks",
                "16",
            ],
        )
    )
    truth = payload["data"]["offsets_cycles"]
    for method in ("coarse_ref", "overlap_consensus"):
        out = tmp_path / f"{method}.npz"
        res = _json(
            runner.invoke(
                app,
                [
                    "--json",
                    "research",
                    "stitch",
                    "--tiles",
                    str(tiles),
                    "--method",
                    method,
                    "--out",
                    str(out),
                ],
            )
        )
        assert res["data"]["offsets_cycles"] == truth
        assert res["data"]["offsets"]["n_wrong_offsets"] == 0
        assert res["data"]["boundary_jumps"]["n_boundaries_with_jump"] == 0
        assert res["data"]["unwrap_error_fraction"] == 0.0
        assert out.exists() and out.with_suffix(".json").exists()
    text = runner.invoke(
        app,
        [
            "research",
            "stitch",
            "--tiles",
            str(tiles),
            "--method",
            "overlap_consensus",
            "--out",
            str(tmp_path / "t.npz"),
        ],
    )
    assert text.exit_code == 0 and "스티칭 완료" in text.output


def test_stitch_coarse_ref_needs_reference(tmp_path: Path):
    from wintersar.research.stitching import save_tiles_npz

    arr = np.zeros((10, 10))
    p = save_tiles_npz(tmp_path / "t.npz", [(arr, slice(0, 10), slice(0, 10))], (10, 10))
    res = runner.invoke(
        app,
        [
            "--json",
            "research",
            "stitch",
            "--tiles",
            str(p),
            "--method",
            "coarse_ref",
            "--out",
            str(tmp_path / "o.npz"),
        ],
    )
    assert res.exit_code == 1 and json.loads(res.stdout)["findings"][0]["rule_id"] == "RES-004"


def test_experiment_command_runs_and_lists(tmp_path: Path):
    spec = tmp_path / "tiny.yaml"
    spec.write_text(
        "name: tiny\nkind: stitching\nfactor: 2\nseeds: [0]\n"
        "data: {shape: [32, 32], rows: 2, cols: 2, overlap: 4}\n"
        "methods: [overlap_consensus]\nmetrics: [offsets_exact, wall_s]\n",
        encoding="utf-8",
    )
    res = _json(
        runner.invoke(
            app, ["--json", "research", "experiment", str(spec), "--out", str(tmp_path / "out")]
        )
    )
    assert res["ok"] and res["data"]["status"] == "ok"
    assert res["data"]["summary"]["overlap_consensus"]["offsets_exact"]["mean"] == 1.0
    assert (tmp_path / "out" / "tiny.md").exists()
    skipped = _json(
        runner.invoke(
            app,
            [
                "--json",
                "research",
                "experiment",
                "S_synth_seq_estimator_ab",
                "--out",
                str(tmp_path / "ab"),
            ],
        )
    )
    assert skipped["data"]["status"] == "skipped" and skipped["findings"][0]["rule_id"] == "RES-002"
    text = runner.invoke(
        app,
        [
            "--lang",
            "en",
            "research",
            "experiment",
            "S_synth_seq_estimator_ab",
            "--out",
            str(tmp_path / "ab2"),
        ],
    )
    assert text.exit_code == 0 and "skipped" in text.output
    listing = _json(runner.invoke(app, ["--json", "research", "experiments"]))
    assert {e["name"] for e in listing["data"]["experiments"]} >= {
        "S_synth_repr_phase",
        "S_synth_stitching",
    }
    bad = runner.invoke(
        app, ["--json", "research", "experiment", "does_not_exist", "--out", str(tmp_path / "x")]
    )
    assert bad.exit_code == 1
