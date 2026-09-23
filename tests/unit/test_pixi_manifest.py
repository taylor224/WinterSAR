"""PERF-12: pixi manifest / engines Dockerfile contract (ADR-0105/0106/0107).

pixi is not installed on the development machine (CLAUDE.md Environment), so these tests
verify what can be verified without it: the manifest parses, the tables the install docs rely
on exist, platform exclusions match the conda-forge facts recorded in ADR-0105, and neither the core
dependency list nor any mirrored extra can drift from ``pyproject.toml`` (``scripts/check_env.py``:
every pyproject extra and every pixi feature is compared or explicitly declared one-sided).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
PIXI_TOML = REPO / "pixi.toml"
PYPROJECT = REPO / "pyproject.toml"
DOCKERFILE = REPO / "Dockerfile.engines"
CHECK_ENV = REPO / "scripts" / "check_env.py"

ENGINE_FEATURES = ("geo", "hyp3", "unwrap", "tophu", "isce2", "mintpy", "dolphin", "aux")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    with PIXI_TOML.open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def check_env() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_env", CHECK_ENV)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # importlib recipe: register before executing
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ manifest structure


def test_workspace_table(manifest: dict[str, Any]) -> None:
    ws = manifest["workspace"]  # [project] is deprecated since pixi 0.57 (ADR-0105)
    assert "project" not in manifest
    assert ws["name"] == "wintersar"
    assert ws["channels"] == ["conda-forge"]
    assert set(ws["platforms"]) == {"linux-64", "osx-arm64", "osx-64"}


def test_default_environment_is_core(manifest: dict[str, Any]) -> None:
    assert manifest["dependencies"]["python"].startswith(">=3.11")
    pypi = manifest["pypi-dependencies"]
    assert pypi["wintersar"] == {"path": ".", "editable": True}
    # conda side of the default feature is the interpreter only: everything else is PyPI,
    # exactly like the uv environment (ADR-0107)
    assert set(manifest["dependencies"]) == {"python"}


def test_features_exist(manifest: dict[str, Any]) -> None:
    features = manifest["feature"]
    for name in (*ENGINE_FEATURES, "dev"):
        assert name in features, name
    assert features["unwrap"]["dependencies"]["snaphu"] == ">=0.4,<1"
    assert features["tophu"]["dependencies"]["tophu"] == ">=0.2,<1"
    assert features["isce2"]["dependencies"]["isce2"] == ">=2.6,<3"
    assert features["mintpy"]["dependencies"]["mintpy"] == ">=1.5,<2"
    assert features["dolphin"]["dependencies"]["dolphin"] == ">=0.40,<1"
    assert set(features["aux"]["dependencies"]) == {"sentineleof", "sardem"}
    assert features["hyp3"]["pypi-dependencies"]["hyp3-sdk"] == ">=7.0"


def test_platform_exclusions_match_conda_forge_facts(manifest: dict[str, Any]) -> None:
    """ADR-0105: isce2 has no osx-arm64 build; tophu run-requires __linux."""
    features = manifest["feature"]
    assert features["isce2"]["platforms"] == ["linux-64", "osx-64"]
    assert features["tophu"]["platforms"] == ["linux-64"]
    for name in ("unwrap", "mintpy", "dolphin", "aux", "hyp3", "geo", "dev"):
        assert "platforms" not in features[name], f"{name} must be available everywhere"


def test_isce2_activation_puts_topsstack_on_path(manifest: dict[str, Any]) -> None:
    env = manifest["feature"]["isce2"]["activation"]["env"]
    assert "share/isce2/topsStack" in env["PATH"]
    assert env["PATH"].endswith(":$PATH")


def test_environments(manifest: dict[str, Any]) -> None:
    envs = manifest["environments"]
    assert set(envs) >= {"default", "dev", "engines", "engines-portable"}
    assert envs["dev"]["features"] == ["dev"]
    assert set(envs["engines"]["features"]) == set(ENGINE_FEATURES)
    # the portable (macOS / Apple-silicon) environment drops exactly the platform-limited ones
    assert set(envs["engines"]["features"]) - set(envs["engines-portable"]["features"]) == {
        "isce2",
        "tophu",
    }
    for env in envs.values():
        for feat in env.get("features", []):
            assert feat in manifest["feature"], feat


def test_tasks(manifest: dict[str, Any]) -> None:
    assert manifest["tasks"]["check-install"].startswith("wintersar check-install")
    dev_tasks = manifest["feature"]["dev"]["tasks"]
    assert "not network" in dev_tasks["test"] and "not engine_real" in dev_tasks["test"]
    assert dev_tasks["lint"].startswith("ruff check")


def test_engine_versions_match_adapter_constraints(manifest: dict[str, Any]) -> None:
    """The conda specs must be the adapters' ``version_constraint`` (what check-install accepts)."""
    from wintersar.engines.base import list_engines

    engines = list_engines()
    features = manifest["feature"]
    pairs = {
        "snaphu": ("unwrap", "snaphu"),
        "tophu": ("tophu", "tophu"),
        "isce2_topsstack": ("isce2", "isce2"),
        "mintpy": ("mintpy", "mintpy"),
        "dolphin": ("dolphin", "dolphin"),
    }
    for engine, (feature, package) in pairs.items():
        assert features[feature]["dependencies"][package] == engines[engine].version_constraint
    hyp3 = manifest["feature"]["hyp3"]["pypi-dependencies"]["hyp3-sdk"]
    assert engines["hyp3"].version_constraint.startswith(hyp3)


# ------------------------------------------------------------------ pyproject <-> pixi drift


def test_check_env_script_passes_on_repo() -> None:
    proc = subprocess.run(
        [sys.executable, str(CHECK_ENV)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "agree" in proc.stdout
    for group in ("core", "dev", "hyp3", "unwrap", "aux", "plots"):
        assert group in proc.stdout, group


def test_core_list_mirrors_pyproject(check_env: ModuleType, manifest: dict[str, Any]) -> None:
    with PYPROJECT.open("rb") as fh:
        pyproject = tomllib.load(fh)
    assert check_env.compare(pyproject, manifest) == []
    core = check_env.pyproject_group(pyproject, "core")
    assert core == check_env.pixi_group(manifest, "core")
    assert "wintersar" not in core  # the editable path entry is not a dependency


def test_check_env_detects_drift(check_env: ModuleType) -> None:
    pyproject = {
        "project": {
            "dependencies": ["numpy>=1.26", "Types-PyYAML", "rich >= 13.0"],
            "optional-dependencies": {"dev": ["pytest>=8.0"], "hyp3": ["hyp3-sdk>=7.0"]},
        }
    }
    pixi = {
        "pypi-dependencies": {
            "wintersar": {"path": ".", "editable": True},
            "numpy": ">=1.25",  # specifier drift
            "types_pyyaml": "*",  # same package (PEP 503) with 'any version'
            "rich": {"version": ">=13.0"},  # inline-table form
            "extra-pkg": ">=1",  # only in pixi
        },
        "feature": {"hyp3": {"pypi-dependencies": {"hyp3-sdk": ">=7.0"}}},
        # no feature.dev table at all
    }
    problems = check_env.compare(pyproject, pixi)
    joined = "\n".join(problems)
    assert "[core] numpy: pyproject.toml '>=1.26' != pixi.toml '>=1.25'" in joined
    assert "extra-pkg" in joined and "missing in pyproject.toml" in joined
    assert "[dev] pixi.toml has no [feature.dev.pypi-dependencies] table" in joined
    assert "types-pyyaml" not in joined and "rich" not in joined and "hyp3" not in joined
    assert check_env.compare(pyproject, pixi, ["hyp3"]) == []


def test_check_env_cli_reports_drift(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["numpy>=1.26"]\n', encoding="utf-8"
    )
    (tmp_path / "pixi.toml").write_text('[pypi-dependencies]\nnumpy = ">=1.20"\n', encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(CHECK_ENV),
            "--pyproject",
            str(tmp_path / "pyproject.toml"),
            "--pixi",
            str(tmp_path / "pixi.toml"),
            "--group",
            "core",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "drift" in proc.stdout and "numpy" in proc.stdout


def test_requirement_parsing(check_env: ModuleType) -> None:
    assert check_env.parse_requirement("numpy>=1.26") == ("numpy", ">=1.26")
    assert check_env.parse_requirement("snaphu>=0.4,<1") == ("snaphu", "<1,>=0.4")
    assert check_env.parse_requirement('foo[bar] >= 1 ; python_version < "3.12"') == ("foo", ">=1")
    assert check_env.parse_requirement("types-PyYAML") == ("types-pyyaml", "")
    assert check_env.normalize_spec("*") == check_env.normalize_spec(None) == ""


def test_check_env_classifies_every_extra_and_feature(
    check_env: ModuleType, manifest: dict[str, Any]
) -> None:
    """Every pyproject extra and every pixi feature is compared or explicitly one-sided.

    ``unwrap``/``orbits``/``dem`` mirror conda-forge features, ``plots`` is a subset of the dev
    table; ``spurt`` (no conda-forge package, ADR-0025), ``gpu`` (CPU-only engine environments)
    and ``docs`` (mkdocs is the uv/CI path) are uv-only; the conda-only engines and ``geo`` are
    pixi-only (ADR-0107).
    """
    with PYPROJECT.open("rb") as fh:
        pyproject = tomllib.load(fh)
    extras = set(pyproject["project"]["optional-dependencies"])
    mapped = {e for g in check_env.GROUPS.values() for e in g.extras} - {"core"}
    assert mapped == {"dev", "hyp3", "unwrap", "orbits", "dem", "plots"}
    assert mapped <= extras
    assert extras - mapped == set(check_env.UV_ONLY_EXTRAS) == {"spurt", "gpu", "docs"}
    features = set(manifest["feature"])
    mapped_features = {g.table[1] for g in check_env.GROUPS.values() if g.table[0] == "feature"}
    assert mapped_features == {"dev", "hyp3", "unwrap", "aux"}
    assert features - mapped_features == set(check_env.PIXI_ONLY_FEATURES)
    assert set(check_env.PIXI_ONLY_FEATURES) == {"geo", "tophu", "isce2", "mintpy", "dolphin"}
    assert check_env.GROUPS["plots"].subset and not check_env.GROUPS["dev"].subset
    # what the script prints must agree with the tables above
    problems = check_env.compare(pyproject, manifest)
    assert problems == []


def test_check_env_detects_conda_feature_drift(check_env: ModuleType) -> None:
    """Round-2 finding: unwrap/orbits/dem/plots used to be outside the guard."""
    pyproject: dict[str, Any] = {
        "project": {
            "dependencies": [],
            "optional-dependencies": {
                "unwrap": ["snaphu>=0.5"],  # spec drift vs the conda feature
                "orbits": ["sentineleof>=0.99"],  # spec drift
                "dem": ["sardem>=0.11"],  # in sync
                "plots": ["matplotlib>=9.9", "pandas>=2.1"],  # spec drift vs the dev table
            },
        }
    }
    pixi: dict[str, Any] = {
        "pypi-dependencies": {},
        "feature": {
            "unwrap": {"dependencies": {"snaphu": ">=0.4,<1"}},
            "aux": {"dependencies": {"sentineleof": ">=0.10", "sardem": {"version": ">=0.11"}}},
            "dev": {"pypi-dependencies": {"matplotlib": ">=3.8", "pandas": ">=2.1", "pytest": "*"}},
        },
    }
    problems = check_env.compare(pyproject, pixi, ["unwrap", "aux", "plots"])
    joined = "\n".join(problems)
    assert "[unwrap] snaphu: pyproject.toml '>=0.5' != pixi.toml '<1,>=0.4'" in joined
    assert "[aux] sentineleof: pyproject.toml '>=0.99' != pixi.toml '>=0.10'" in joined
    assert "sardem" not in joined
    assert "[plots] matplotlib: pyproject.toml '>=9.9' != pixi.toml '>=3.8'" in joined
    assert "pandas" not in joined
    assert "pytest" not in joined  # plots is a *subset* of the dev table: extra entries are fine
    # an extra that is silently missing on the pixi side is reported, one-sided lists are not
    pixi["feature"]["aux"]["dependencies"].pop("sardem")
    assert "[aux] sardem>=0.11: in pyproject.toml, missing in [feature.aux.dependencies]" in (
        check_env.compare(pyproject, pixi, ["aux"])
    )
    # two extras mapped onto one conda table must agree with each other
    pyproject["project"]["optional-dependencies"]["orbits"].append("sardem>=0.12")
    assert any(
        "[aux] sardem" in p and "orbits" in p and "dem" in p
        for p in check_env.compare(pyproject, pixi, ["aux"])
    )


def test_check_env_reports_unclassified_extra_and_feature(check_env: ModuleType) -> None:
    """A new extra or feature must be mapped or declared one-sided, otherwise it is drift."""
    pyproject: dict[str, Any] = {
        "project": {"dependencies": [], "optional-dependencies": {"newthing": ["foo>=1"]}}
    }
    pixi: dict[str, Any] = {
        "pypi-dependencies": {},
        "feature": {"newfeat": {"dependencies": {"bar": ">=1"}}},
    }
    problems = check_env.compare(pyproject, pixi)
    joined = "\n".join(problems)
    assert "[extras] newthing" in joined and "UV_ONLY_EXTRAS" in joined
    assert "[features] newfeat" in joined and "PIXI_ONLY_FEATURES" in joined
    # the declared one-sided lists are accepted without a counterpart
    pyproject["project"]["optional-dependencies"] = {"spurt": ["spurt>=0.1,<1"]}
    pixi["feature"] = {"geo": {"dependencies": {"gdal": ">=3.5"}}}
    classified = [
        p for p in check_env.compare(pyproject, pixi) if p.startswith(("[extras]", "[features]"))
    ]
    assert classified == []
    # a --group selection compares only that group and skips the classification
    pyproject["project"]["optional-dependencies"] = {"newthing": ["foo>=1"]}
    assert check_env.compare(pyproject, pixi, ["core"]) == []


@pytest.mark.parametrize("bad", ["missing", "invalid-toml", "bad-requirement"])
def test_check_env_cli_bad_input_exits_2(tmp_path: Path, bad: str) -> None:
    """Exit 2 = bad input, distinguishable from 1 = drift (no traceback, message on stderr)."""
    pyproject = tmp_path / "pyproject.toml"
    pixi = tmp_path / "pixi.toml"
    pyproject.write_text('[project]\ndependencies = ["numpy>=1.26"]\n', encoding="utf-8")
    if bad == "invalid-toml":
        pixi.write_text("[pypi-dependencies\nnumpy = \n", encoding="utf-8")
    elif bad == "bad-requirement":
        pixi.write_text('[pypi-dependencies]\nnumpy = ">=1.26"\n', encoding="utf-8")
        pyproject.write_text('[project]\ndependencies = ["-not a requirement"]\n', encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(CHECK_ENV),
            "--pyproject",
            str(pyproject),
            "--pixi",
            str(pixi),
            "--group",
            "core",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stderr.startswith("check_env.py: ")
    assert proc.stdout == ""
    if bad == "bad-requirement":
        assert "not a requirement" in proc.stderr
    else:
        assert "pixi.toml" in proc.stderr


# ------------------------------------------------------------------ Dockerfile.engines


def test_dockerfile_engines_contract() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM ghcr.io/prefix-dev/pixi:" in text
    assert "pixi install -e engines" in text
    assert "pixi shell-hook -e engines" in text
    assert "check-install --strict" in text
    assert "COPY --from=build /app /app" in text


def test_dockerfile_smoke_engines_are_registered_and_in_manifest(
    manifest: dict[str, Any],
) -> None:
    """Every ``--engine`` the smoke test names must exist and be provided by the manifest."""
    from wintersar.engines.base import list_engines

    text = DOCKERFILE.read_text(encoding="utf-8")
    smoke = {tok for tok in text.replace("\\\n", " ").split() if tok not in ("--engine",)}
    names = {n for n in list_engines() if n in smoke}
    assert names == {"fake", "hyp3", "snaphu", "tophu", "isce2_topsstack", "mintpy", "dolphin"}
    assert "spurt" not in names  # no conda-forge package (ADR-0025)
    feature_for = {
        "hyp3": "hyp3",
        "snaphu": "unwrap",
        "tophu": "tophu",
        "isce2_topsstack": "isce2",
        "mintpy": "mintpy",
        "dolphin": "dolphin",
    }
    engines_env = set(manifest["environments"]["engines"]["features"])
    for engine, feature in feature_for.items():
        assert feature in engines_env, engine
