"""Run outputs: a headline summary, per-step results and derived tables."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import constants as C
from .network import CORRIDOR_ZONES
from .simulate import realised_generation

SEASONS = {"winter": [12, 1, 2], "spring": [3, 4, 5],
           "summer": [6, 7, 8], "autumn": [9, 10, 11]}


def _energy_mwh(series: pd.Series) -> float:
    return float(series.sum() * C.DT_H)


def metrics(cfg, result: pd.DataFrame) -> dict:
    """Headline numbers for one run, used by the summary and the matrix table."""
    ok = result[result["converged"]]
    n = len(ok)
    if n == 0:
        return {"scenario": cfg.stem, "converged_steps": 0}

    available = _energy_mwh(ok["available_total_mw"])
    curtailed = _energy_mwh(ok["curtailed_total_mw"])
    delivered = _energy_mwh(realised_generation(ok))
    loading = pd.concat([ok[f"loading_{z}_pct"] for z in CORRIDOR_ZONES], axis=1).max(axis=1)
    # The export path is one series thermal path, so the governing rating is
    # the lower of the two zones.
    rating = pd.concat([ok[f"rating_{z}_a"] for z in CORRIDOR_ZONES], axis=1).min(axis=1)
    rating_mean = float(rating.mean())

    out = {
        "scenario": cfg.stem,
        "conductor": cfg.conductor,
        "dlr_mode": cfg.dlr_mode,
        "n_der": cfg.n_der,
        "fleet_mw": cfg.der_fleet_mw,
        "storage": int(cfg.storage_enabled),
        "steps": len(result),
        "converged_steps": n,
        "converged_pct": 100.0 * n / len(result),
        "reg_converged_pct": 100.0 * float(ok["reg_converged"].mean()),
        "available_mwh": available,
        "delivered_mwh": delivered,
        "curtailed_mwh": curtailed,
        "curtailed_pct": 100.0 * curtailed / available if available > 0 else 0.0,
        "capacity_factor": delivered / (cfg.der_fleet_mw * n * C.DT_H) if cfg.der_fleet_mw else 0.0,
        "rating_mean_a": rating_mean,
        "rating_uplift_pct": 100.0 * (rating_mean / cfg.static_rating_a - 1.0),
        "corridor_loading_mean_pct": float(loading.mean()),
        "corridor_loading_p95_pct": float(loading.quantile(0.95)),
        "hours_corridor_over_limit": float((loading > 100.0).sum() * C.DT_H),
        "hours_over_temperature": float(sum(
            (ok[f"t_cond_{z}_c"] > cfg.t_cond_max_c).sum() for z in CORRIDOR_ZONES) * C.DT_H),
        "pcc_peak_mw": float(ok["pcc_p_mw"].max()),
        "hours_over_export_cap": float((ok["pcc_p_mw"] > cfg.export_cap_mw).sum() * C.DT_H),
        "hours_voltage_high": float(ok["viol_L1"].sum() * C.DT_H),
        "hours_voltage_low": float(ok["viol_L2"].sum() * C.DT_H),
        "hours_q_outside_window": float((1 - ok["q_within_window"]).sum() * C.DT_H),
        "losses_mwh": _energy_mwh(ok["loss_line_mw"] + ok["loss_trafo_mw"]),
        "oltc_operations": int(ok["oltc_moves"].sum()),
        "reactor_operations": int(ok["reactor_moves"].sum()),
        "q_tracking_error_max_mvar": float(ok["q_tracking_error_mvar"].max()),
        "runtime_s": float(result.attrs.get("runtime_s", float("nan"))),
    }

    for cause in ("corridor", "dso_trafo", "plant", "overvoltage", "export_cap"):
        mask = ok["curtail_cause"] == cause
        out[f"curtailed_{cause}_mwh"] = _energy_mwh(ok.loc[mask, "curtailed_total_mw"])

    if cfg.storage_enabled:
        out.update({
            "storage_discharged_mwh": _energy_mwh(ok["storage_p_grid_mw"].clip(lower=0)),
            "storage_charged_mwh": _energy_mwh((-ok["storage_p_grid_mw"]).clip(lower=0)),
            "storage_cycles": _energy_mwh(ok["storage_p_grid_mw"].clip(lower=0)) / cfg.storage_e_mwh,
            "storage_shortfall_mwh": _energy_mwh(ok["storage_shortfall_mw"].fillna(0)),
            "hours_reserve_short": float(ok["storage_reserve_short"].sum() * C.DT_H),
            "storage_soc_mean_pct": float(ok["storage_soc_pct"].mean()),
        })
    return out


def violations(result: pd.DataFrame) -> pd.DataFrame:
    """Every step where any level was violated after remediation."""
    ok = result[result["converged"]]
    mask = ok[["viol_L1", "viol_L2", "viol_L3", "viol_L4"]].any(axis=1)
    cols = ["binding_all_after", "curtail_cause", "curtailed_total_mw", "margin_after",
            "residual_class", "viol_L1", "viol_L2", "viol_L3", "viol_L4",
            "worst_corridor_pct", "worst_dso_trafo_pct", "worst_plant_pct",
            "pcc_p_mw", "available_total_mw"]
    return ok.loc[mask, [c for c in cols if c in ok.columns]]


def seasonal(cfg, result: pd.DataFrame) -> pd.DataFrame:
    """Season-by-season view; DLR value is strongly seasonal."""
    ok = result[result["converged"]].copy()
    ok["season"] = ok.index.month.map(
        {m: s for s, months in SEASONS.items() for m in months})
    loading = pd.concat([ok[f"loading_{z}_pct"] for z in CORRIDOR_ZONES], axis=1).max(axis=1)
    rating = pd.concat([ok[f"rating_{z}_a"] for z in CORRIDOR_ZONES], axis=1).min(axis=1)
    frame = pd.DataFrame({
        "season": ok["season"],
        "available_mw": ok["available_total_mw"],
        "curtailed_mw": ok["curtailed_total_mw"],
        "rating_a": rating,
        "loading_pct": loading,
        "t_air_c": ok["t_air_c"],
        "pcc_p_mw": ok["pcc_p_mw"],
    })
    agg = frame.groupby("season").agg(
        steps=("available_mw", "size"),
        available_mwh=("available_mw", lambda s: _energy_mwh(s)),
        curtailed_mwh=("curtailed_mw", lambda s: _energy_mwh(s)),
        rating_mean_a=("rating_a", "mean"),
        loading_mean_pct=("loading_pct", "mean"),
        loading_max_pct=("loading_pct", "max"),
        t_air_mean_c=("t_air_c", "mean"),
        pcc_peak_mw=("pcc_p_mw", "max"))
    agg["curtailed_pct"] = 100.0 * agg["curtailed_mwh"] / agg["available_mwh"].replace(0, np.nan)
    return agg.reindex([s for s in SEASONS if s in agg.index])


def summary_text(cfg, result: pd.DataFrame) -> str:
    """Human-readable run report."""
    m = metrics(cfg, result)
    if not m.get("converged_steps"):
        return "No converged timesteps."

    counters = result.attrs.get("counters", {})
    lines = [
        "=" * 74,
        f"  {cfg.stem}",
        "=" * 74,
        f"  window            {result.index[0]:%Y-%m-%d} to {result.index[-1]:%Y-%m-%d}"
        f"  ({m['steps']} steps of {int(C.DT_H * 60)} min)",
        f"  corridor          {cfg.conductor} conductor, {cfg.bundle_n}x, "
        f"static {cfg.static_rating_a:.0f} A, {C.CORRIDOR_LENGTH_KM:.1f} km",
        f"  generation        {cfg.n_der}/4 plants, {cfg.der_fleet_mw:.0f} MW"
        f" | control {cfg.control_mode} | storage {'on' if cfg.storage_enabled else 'off'}",
        f"  rating method     mode {cfg.dlr_mode} "
        f"({['static', 'ambient-adjusted', 'full weather'][cfg.dlr_mode]})",
        "",
        "  CONVERGENCE",
        f"    power flow            {m['converged_pct']:6.2f} %  "
        f"({m['steps'] - m['converged_steps']} steps failed)",
        f"    control loop          {m['reg_converged_pct']:6.2f} %",
        f"    max reactive error    {m['q_tracking_error_max_mvar']:6.2f} MVAr",
        f"    curtailment retries   {counters.get('curtail_failures', 0)}",
        "",
        "  LINE RATING",
        f"    mean operative        {m['rating_mean_a']:6.0f} A "
        f"({m['rating_uplift_pct']:+.1f} % against static)",
        f"    corridor loading      mean {m['corridor_loading_mean_pct']:5.1f} %, "
        f"p95 {m['corridor_loading_p95_pct']:5.1f} %",
        f"    hours over rating     {m['hours_corridor_over_limit']:6.1f}",
        f"    hours over {cfg.t_cond_max_c:.0f} C design  {m['hours_over_temperature']:6.1f}",
        "",
        "  ENERGY",
        f"    available             {m['available_mwh']:10.1f} MWh",
        f"    delivered             {m['delivered_mwh']:10.1f} MWh "
        f"(capacity factor {m['capacity_factor']:.3f})",
        f"    curtailed             {m['curtailed_mwh']:10.1f} MWh "
        f"({m['curtailed_pct']:.2f} % of available)",
        f"    network losses        {m['losses_mwh']:10.1f} MWh",
    ]
    causes = [(c, m[f"curtailed_{c}_mwh"]) for c in
              ("corridor", "dso_trafo", "plant", "overvoltage", "export_cap")]
    for cause, value in causes:
        if value > 0.01:
            lines.append(f"      by {cause:<16s}{value:10.1f} MWh")

    lines += [
        "",
        "  CONSTRAINTS AFTER CONTROL",
        f"    peak export           {m['pcc_peak_mw']:6.1f} MW "
        f"(cap {cfg.export_cap_mw:.0f} MW, {m['hours_over_export_cap']:.1f} h above)",
        f"    hours voltage high    {m['hours_voltage_high']:6.1f}",
        f"    hours voltage low     {m['hours_voltage_low']:6.1f}",
        f"    hours reactive out    {m['hours_q_outside_window']:6.1f} "
        f"(window +/-{cfg.q_window_mvar:.1f} MVAr)",
        "",
        "  CONTROL ACTIVITY",
        f"    tap operations        {m['oltc_operations']:6d}",
        f"    reactor operations    {m['reactor_operations']:6d}",
    ]

    if cfg.storage_enabled:
        lines += [
            "",
            "  STORAGE",
            f"    discharged            {m['storage_discharged_mwh']:10.1f} MWh "
            f"({m['storage_cycles']:.1f} equivalent cycles)",
            f"    charged               {m['storage_charged_mwh']:10.1f} MWh",
            f"    delivery shortfall    {m['storage_shortfall_mwh']:10.1f} MWh "
            f"({counters.get('headroom_clamped', 0)} steps clamped by export headroom)",
            f"    hours reserve short   {m['hours_reserve_short']:6.1f}",
            f"    mean state of charge  {m['storage_soc_mean_pct']:6.1f} %",
        ]

    lines += ["", f"  runtime {m['runtime_s']:.0f} s", "=" * 74]
    return "\n".join(lines)


def write(cfg, result: pd.DataFrame, out_dir: Path) -> dict:
    """Write every output file for one run and return the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = cfg.stem
    paths = {
        "timeseries": out_dir / f"{stem}_timeseries.csv",
        "violations": out_dir / f"{stem}_violations.csv",
        "seasonal": out_dir / f"{stem}_seasonal.csv",
        "metrics": out_dir / f"{stem}_metrics.json",
        "summary": out_dir / f"{stem}_summary.txt",
    }
    result.to_csv(paths["timeseries"], lineterminator="\n")
    violations(result).to_csv(paths["violations"], lineterminator="\n")
    seasonal(cfg, result).to_csv(paths["seasonal"], lineterminator="\n")
    paths["metrics"].write_text(json.dumps(metrics(cfg, result), indent=2), encoding="utf-8")
    paths["summary"].write_text(summary_text(cfg, result), encoding="utf-8")
    return paths
