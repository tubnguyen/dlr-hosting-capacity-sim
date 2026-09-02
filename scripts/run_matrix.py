"""Run the scenario matrix and build the comparison table and figures.

    python scripts/run_matrix.py --days 30 --jobs 8
    python scripts/run_matrix.py --only dlr2_der4 static_der4
    python scripts/run_matrix.py --collect-only      # rebuild outputs from runs

Each scenario runs in its own single-threaded process; pandapower's solve is
serial, so giving every worker its own thread pool only creates contention.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from corridor_sim import plots  # noqa: E402
from corridor_sim.cli import run_scenario  # noqa: E402
from corridor_sim.config import PRESETS, build_config  # noqa: E402

for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

TABLE_COLUMNS = [
    "scenario", "conductor", "dlr_mode", "n_der", "fleet_mw", "storage",
    "available_mwh", "delivered_mwh", "curtailed_mwh", "curtailed_pct",
    "rating_mean_a", "rating_uplift_pct", "corridor_loading_p95_pct",
    "hours_corridor_over_limit", "pcc_peak_mw", "hours_over_export_cap",
    "hours_voltage_high", "hours_voltage_low", "losses_mwh",
    "converged_pct", "reg_converged_pct", "runtime_s",
]


def _run_one(args) -> dict:
    preset, days, start, out_dir, data_dir, make_plots = args
    cfg = build_config(preset, days=days, start=start, out_dir=Path(out_dir),
                       data_dir=Path(data_dir), label=preset)
    t0 = time.time()
    try:
        metrics, _ = run_scenario(cfg, make_plots=make_plots, progress=False)
        metrics["status"] = "ok"
    except Exception as exc:                          # keep the matrix going
        metrics = {"scenario": preset, "status": f"failed: {type(exc).__name__}: {exc}"}
    metrics["wall_s"] = time.time() - t0
    return metrics


def collect(out_dir: Path) -> pd.DataFrame:
    """Gather every scenario's metrics into one table."""
    rows = []
    for path in sorted(out_dir.glob("*/*_metrics.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    if not rows:
        raise FileNotFoundError(f"no metrics found under {out_dir}")
    table = pd.DataFrame(rows)
    ordered = [c for c in TABLE_COLUMNS if c in table.columns]
    rest = [c for c in table.columns if c not in ordered]
    return table[ordered + rest].sort_values(
        ["conductor", "dlr_mode", "n_der", "storage"]).reset_index(drop=True)


def _load_series(out_dir: Path, scenario: str):
    path = out_dir / scenario / f"{scenario}_timeseries.csv"
    if not path.exists():
        return None
    return pd.read_csv(path, index_col=0, parse_dates=True)


def build_outputs(out_dir: Path, figures_dir: Path, redraw: bool = True) -> pd.DataFrame:
    """Write the comparison table and every figure, from the saved time series."""
    table = collect(out_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(out_dir / "matrix_summary.csv", index=False, lineterminator="\n")

    plots.curtailment_matrix(table, figures_dir / "curtailment_matrix.png")

    # Loading duration curve needs the per-step results of the three rating
    # methods at full generation.
    runs = {}
    for preset, label in (("static_der4", "Static rating"),
                          ("dlr1_der4", "Ambient-adjusted"),
                          ("dlr2_der4", "Full-weather DLR")):
        result = _load_series(out_dir, preset)
        if result is not None:
            runs[label] = result
    if len(runs) >= 2:
        plots.loading_duration(runs, figures_dir / "loading_duration.png")

    if redraw:
        for scenario in table["scenario"]:
            result = _load_series(out_dir, scenario)
            if result is None:
                continue
            plots.run_figures(build_config(scenario, label=scenario), result,
                              out_dir / scenario / "figures")
    return table


def _headline(table: pd.DataFrame) -> str:
    view = table[table["n_der"] == 4].copy()
    if view.empty:
        return ""
    lines = ["", "  Full generation connected (4 plants):", ""]
    lines.append(f"    {'scenario':<22s}{'rating A':>10s}{'curtailed %':>13s}"
                 f"{'delivered MWh':>15s}{'hours >limit':>14s}")
    for _, row in view.iterrows():
        lines.append(f"    {row['scenario']:<22s}{row['rating_mean_a']:>10.0f}"
                     f"{row['curtailed_pct']:>13.2f}{row['delivered_mwh']:>15.0f}"
                     f"{row['hours_corridor_over_limit']:>14.1f}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    parser.add_argument("--only", nargs="+", metavar="PRESET")
    parser.add_argument("--out", type=Path, default=ROOT / "runs")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--figures", type=Path, default=ROOT / "docs" / "figures")
    parser.add_argument("--no-plots", dest="plots", action="store_false")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args(argv)

    if args.collect_only:
        table = build_outputs(args.out, args.figures)
        print(table.to_string(index=False))
        return 0

    presets = args.only or list(PRESETS)
    unknown = set(presets) - set(PRESETS)
    if unknown:
        print(f"unknown preset(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    print(f"Running {len(presets)} scenarios over {args.days} days, "
          f"{args.jobs} at a time -> {args.out}")
    jobs = [(p, args.days, args.start, str(args.out), str(args.data_dir), args.plots)
            for p in presets]
    t0 = time.time()
    failures = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_run_one, job): job[0] for job in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            metrics = future.result()
            status = metrics.get("status", "?")
            mark = "ok  " if status == "ok" else "FAIL"
            print(f"  [{done:2d}/{len(jobs)}] {mark} {metrics['scenario']:<22s}"
                  f"{metrics['wall_s'] / 60:6.1f} min"
                  + ("" if status == "ok" else f"  {status}"), flush=True)
            if status != "ok":
                failures.append(metrics)

    print(f"\nCompleted in {(time.time() - t0) / 60:.1f} min "
          f"({len(jobs) - len(failures)}/{len(jobs)} succeeded)")
    if failures:
        return 1

    table = build_outputs(args.out, args.figures)
    print(_headline(table))
    print(f"\n  table   -> {args.out / 'matrix_summary.csv'}")
    print(f"  figures -> {args.figures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
