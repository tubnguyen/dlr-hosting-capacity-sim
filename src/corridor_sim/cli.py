"""Command-line entry point for a single scenario."""
from __future__ import annotations

import sys
from pathlib import Path

from . import dataio, dlr, network, plots, report, simulate
from .config import parse_args

RATING_NAMES = {0: "static", 1: "ambient-adjusted", 2: "full-weather"}


def run_scenario(cfg, make_plots: bool = True, progress: bool = True) -> dict:
    """Build, simulate, report and plot one scenario. Returns its metrics."""
    net, buses = network.build(cfg)
    if progress:
        print(f"  network   {network.summary(cfg, net)}")
        check = dlr.calibration(cfg)
        print(f"  rating    mode {cfg.dlr_mode} ({RATING_NAMES[cfg.dlr_mode]}), "
              f"model against static rating {check['deviation_pct']:+.2f} %")

    inputs = dataio.load_inputs(cfg)
    if progress:
        print(f"  inputs    {len(inputs['index'])} steps, "
              f"{cfg.start_ts:%Y-%m-%d} to {cfg.end_ts:%Y-%m-%d}")

    result = simulate.run(cfg, net, buses, inputs, progress=progress)

    out_dir = Path(cfg.out_dir) / cfg.stem
    paths = report.write(cfg, result, out_dir)
    if make_plots:
        plots.run_figures(cfg, result, out_dir / "figures")
    if progress:
        print()
        print(report.summary_text(cfg, result))
        print(f"  output -> {out_dir}")
    return report.metrics(cfg, result), paths


def main(argv=None) -> int:
    cfg, make_plots = parse_args(argv)
    print(f"corridor-sim  ·  {cfg.stem}")
    run_scenario(cfg, make_plots=make_plots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
