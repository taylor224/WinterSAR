"""``wintersar bench`` (plan §4.5 / §5.9): run a site YAML, write ``bench_result.json``,
optionally compare with a baseline and fail on regression (CI, plan §6.3 item 5).

Honours the global ``--json`` / ``--lang`` options (``wintersar.util.clistate.state``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from wintersar.i18n import t
from wintersar.io.schemas import Finding
from wintersar.util.clistate import state
from wintersar.util.output import console, emit_json, err_console, print_findings


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _stage_table(data: dict[str, Any], lang: str) -> Table:
    table = Table(title=t("bench.table.title", lang, repeats=data.get("repeats", "?")), expand=True)
    for key in ("stage", "wall", "cpu", "rss", "disk", "net"):
        table.add_column(t(f"bench.table.{key}", lang))
    rows: dict[str, dict[str, Any]] = dict(data.get("stages", {}))
    if data.get("total"):
        rows[t("bench.table.total", lang)] = data["total"]
    for name, s in rows.items():
        net = s.get("network_bytes")
        table.add_row(
            name,
            _fmt(s.get("wall_time_s")),
            _fmt(s.get("cpu_time_s")),
            _fmt(s.get("peak_rss_gb"), 3),
            _fmt(s.get("disk_peak_gb"), 3),
            _fmt(None if net is None else float(net) / 1e6, 1),
        )
    return table


def register(app: typer.Typer) -> None:
    @app.command("bench")
    def bench(
        site: Annotated[Path, typer.Option("--site", help="Site YAML (benchmarks/sites/*.yaml).")],
        compare: Annotated[
            Path | None,
            typer.Option("--compare", help="Baseline bench_result.json to compare against."),
        ] = None,
        out: Annotated[Path, typer.Option("--out", help="Where to write the result JSON.")] = Path(
            "bench_result.json"
        ),
        repeats: Annotated[
            int | None, typer.Option("--repeats", help="Override the site's repeat count.")
        ] = None,
        fail_on_regression: Annotated[
            bool,
            typer.Option(
                "--fail-on-regression",
                help="Exit 1 when any stage is slower than the baseline by more than the threshold.",
            ),
        ] = False,
        threshold: Annotated[
            float | None,
            typer.Option(
                "--threshold", help="Regression threshold as a fraction (site default 0.15)."
            ),
        ] = None,
        runner: Annotated[
            str | None,
            typer.Option("--runner", help="pipeline | fake (default: the site's 'runner')."),
        ] = None,
        workdir: Annotated[
            Path | None,
            typer.Option("--workdir", help="Keep run directories here (default: temporary)."),
        ] = None,
        allow_network: Annotated[
            bool, typer.Option("--allow-network", help="Permit sites with 'network: true'.")
        ] = False,
        markdown: Annotated[
            Path | None,
            typer.Option("--markdown", help="Also write the comparison table as Markdown."),
        ] = None,
    ) -> None:
        """Run a benchmark site (median of N repeats) and write bench_result.json (plan §5.9)."""
        from wintersar.bench.runner import RUNNERS, run_site
        from wintersar.bench.sites import load_site

        lang = state.lang
        if runner is not None and runner not in RUNNERS:
            err_console.print(f"[red]--runner must be one of {sorted(RUNNERS)}[/]")
            raise typer.Exit(code=2)
        try:
            site_obj = load_site(site)
        except (OSError, ValueError) as e:
            f = Finding(
                rule_id="BENCH-004",
                severity="FAIL",
                message_key="bench.BENCH-004.cause",
                fix_key="bench.BENCH-004.fix",
                params={"site": str(site), "placeholders": str(e)},
            )
            if state.json:
                emit_json("bench", {"site": str(site)}, [f], ok=False)
            else:
                print_findings([f], lang)
            raise typer.Exit(code=2) from e

        def progress(i: int, n: int, outcome: Any) -> None:
            if state.json:
                return
            wall = None if outcome.total is None else outcome.total.wall_time_s
            rss = None if outcome.total is None else outcome.total.peak_rss_gb
            console.print(
                t(
                    "bench.run.repeat_done",
                    lang,
                    i=i,
                    n=n,
                    wall_time_s=wall if wall is not None else float("nan"),
                    peak_rss_gb=rss if rss is not None else float("nan"),
                )
            )

        if not state.json:
            console.print(
                t(
                    "bench.run.start",
                    lang,
                    site=site_obj.name,
                    size=site_obj.size,
                    repeats=repeats or site_obj.repeats,
                    runner=runner or site_obj.runner,
                )
            )
        result = run_site(
            site_obj,
            out,
            compare_with=compare,
            runner=runner,
            repeats=repeats,
            workdir=workdir,
            threshold=threshold,
            fail_on_regression=fail_on_regression,
            allow_network=allow_network,
            progress=progress,
        )
        if markdown is not None and result.comparison is not None:
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_text(result.comparison.to_markdown(lang), encoding="utf-8")
        if state.json:
            emit_json("bench", result.data, result.findings, ok=result.ok)
        else:
            console.print(
                t(
                    "bench.run.git",
                    lang,
                    git_sha=(result.data.get("git_sha") or "?")[:8],
                    version=result.data.get("wintersar_version", "?"),
                )
            )
            if result.data.get("stages"):
                console.print(_stage_table(result.data, lang))
            metrics = result.data.get("metrics") or {}
            for k, v in metrics.items():
                console.print(f"{t(f'bench.metrics.{k}', lang)}: {_fmt(v, 4)}")
            if result.comparison is not None:
                console.print(result.comparison.to_markdown(lang))
            if result.path is not None:
                console.print(t("bench.run.done", lang, path=str(result.path)))
            print_findings(result.findings, lang)
        if not result.ok:
            raise typer.Exit(code=1)
