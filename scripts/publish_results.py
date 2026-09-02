"""Copy the small, readable outputs of a matrix run into `results/`.

`runs/` holds the full per-step time series, which is far too large to commit.
This selects what a reader actually needs: the comparison table, every
scenario's metrics, summary, seasonal breakdown and violation log, plus one
compressed full time series so the column schema can be inspected directly.

    python scripts/publish_results.py [--flagship dlr2_der4_bess]
"""
from __future__ import annotations

import argparse
import gzip
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEEP_SUFFIXES = ("_metrics.json", "_summary.txt", "_seasonal.csv", "_violations.csv")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=ROOT / "runs")
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--flagship", default="dlr2_der4_bess",
                        help="scenario whose full time series is kept, compressed")
    args = parser.parse_args(argv)

    if not args.runs.exists():
        raise SystemExit(f"{args.runs} not found - run scripts/run_matrix.py first")
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    total = 0
    summary = args.runs / "matrix_summary.csv"
    if summary.exists():
        shutil.copy2(summary, args.out / summary.name)
        total += summary.stat().st_size

    for path in sorted(args.runs.glob("*/*")):
        if path.name.endswith(KEEP_SUFFIXES):
            shutil.copy2(path, args.out / path.name)
            total += path.stat().st_size

    # The flagship run's figures are what the README shows.
    docs_figures = ROOT / "docs" / "figures"
    docs_figures.mkdir(parents=True, exist_ok=True)
    for figure in sorted((args.runs / args.flagship / "figures").glob("*.png")):
        name = figure.name.replace(f"{args.flagship}_", "")
        shutil.copy2(figure, docs_figures / name)

    series = args.runs / args.flagship / f"{args.flagship}_timeseries.csv"
    if series.exists():
        target = args.out / f"{series.name}.gz"
        with open(series, "rb") as src, gzip.open(target, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        total += target.stat().st_size

    print(f"  {len(list(args.out.iterdir()))} files, {total / 1e6:.2f} MB -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
