"""Figures for a single run and for the scenario matrix.

Every chart uses one measurement axis, a fixed categorical colour order, and
carries a legend plus direct labels wherever a reader would otherwise have to
match a colour by eye.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from . import constants as C  # noqa: E402
from .network import CORRIDOR_ZONES  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#8a8984"
GRID = "#e5e4e0"
SEQUENTIAL = "viridis"

RATING_LABELS = {0: "Static rating", 1: "Ambient-adjusted", 2: "Full-weather DLR"}


def _style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "font.size": 9.5,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titlesize": 11,
        "axes.titleweight": "bold", "axes.titlecolor": INK,
        "text.color": INK, "xtick.color": INK_2, "ytick.color": INK_2,
        "grid.color": GRID, "grid.linewidth": 0.8,
        "legend.frameon": False, "figure.dpi": 130,
    })


def _clean(ax, ylabel="", title="", xlabel=""):
    ax.set_title(title, loc="left", pad=10)
    ax.set_ylabel(ylabel)
    ax.set_xlabel(xlabel)
    ax.grid(True, axis="y", alpha=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _legend_below(ax, ncols=3, pad=0.16):
    """Place the legend under the axes, where it cannot cover data or a limit label."""
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -pad), ncols=ncols,
              handlelength=1.6, columnspacing=1.6, borderaxespad=0.0)


def _reference(ax, value, label, inside=False):
    """Muted dashed limit line, labelled clear of the data.

    The label sits in the right margin by default; `inside` puts it above the
    line at the left, for axes that already have something in that margin.
    """
    ax.axhline(value, color=MUTED, lw=1.2, ls=(0, (4, 3)), zorder=1)
    if inside:
        ax.annotate(label, xy=(0.01, value), xycoords=("axes fraction", "data"),
                    ha="left", va="bottom", fontsize=8.5, color=INK_2,
                    xytext=(0, 4), textcoords="offset points")
    else:
        ax.annotate(label, xy=(1.0, value), xycoords=("axes fraction", "data"),
                    ha="left", va="center", fontsize=8.5, color=INK_2,
                    xytext=(6, 0), textcoords="offset points", annotation_clip=False)


def _corridor(result: pd.DataFrame, template: str, how: str) -> pd.Series:
    """Combine the per-zone columns named by `template` into one series."""
    frame = pd.concat([result[template.format(zone=z)] for z in CORRIDOR_ZONES], axis=1)
    return frame.max(axis=1) if how == "max" else frame.min(axis=1)


def rating_timeseries(cfg, result: pd.DataFrame, path: Path):
    """How the operative rating moves with weather, against the current carried."""
    _style()
    ok = result[result["converged"]]
    fig, ax = plt.subplots(figsize=(10, 4.0))
    rating = _corridor(ok, "rating_{zone}_a", "min").rolling(8, min_periods=1).mean()
    current = _corridor(ok, "i_{zone}_a", "max").rolling(8, min_periods=1).mean()

    ax.plot(rating.index, rating, color=SERIES[0], lw=1.6, label="Operative rating")
    ax.plot(current.index, current, color=SERIES[1], lw=1.6, label="Corridor current")
    ax.fill_between(rating.index, current, rating, where=rating >= current,
                    color=SERIES[0], alpha=0.08, lw=0)
    _reference(ax, cfg.static_rating_a, f"Static rating {cfg.static_rating_a:.0f} A")
    _clean(ax, "Amperes", f"Corridor rating and loading  ·  {RATING_LABELS[cfg.dlr_mode]}")
    ax.set_ylim(0, max(float(rating.max()), float(current.max()),
                       cfg.static_rating_a) * 1.12)
    _legend_below(ax, ncols=2, pad=0.34)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def loading_duration(runs: dict, path: Path):
    """Duration curve of corridor loading under each rating method."""
    _style()
    fig, ax = plt.subplots(figsize=(8.6, 4.2))
    for i, (label, result) in enumerate(runs.items()):
        ok = result[result["converged"]]
        loading = _corridor(ok, "loading_{zone}_pct", "max").sort_values(ascending=False).to_numpy()
        pct = np.linspace(0, 100, len(loading))
        ax.plot(pct, loading, color=SERIES[i % len(SERIES)], lw=1.8, label=label)
    _reference(ax, 100.0, "Thermal limit")
    _clean(ax, "Corridor loading (% of operative rating)",
           "Loading duration curve by rating method", "Share of time exceeded (%)")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, None)
    ax.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def curtailment_matrix(table: pd.DataFrame, path: Path):
    """Curtailed share of available energy across the scenario matrix."""
    _style()
    order = ["static", "dlr1", "dlr2", "twin"]
    labels = {"static": "Static rating", "dlr1": "Ambient-adjusted",
              "dlr2": "Full-weather DLR", "twin": "Twin conductor"}
    table = table.copy()
    table["group"] = np.where(table["conductor"] == "twin", "twin",
                              "dlr" + table["dlr_mode"].astype(str))
    table["group"] = table["group"].replace({"dlr0": "static"})
    table = table[table["n_der"] > 0]

    fig, ax = plt.subplots(figsize=(9, 4.4))
    der_counts = sorted(table["n_der"].unique())
    width = 0.8 / len(order)
    x = np.arange(len(der_counts))
    for i, group in enumerate(order):
        subset = table[(table["group"] == group) & (table["storage"] == 0)]
        by_count = subset.groupby("n_der")["curtailed_pct"].mean()
        values = [float(by_count.get(n, 0.0)) for n in der_counts]
        offset = (i - (len(order) - 1) / 2) * width
        bars = ax.bar(x + offset, values, width * 0.92, label=labels[group],
                      color=SERIES[i], linewidth=0)
        # Every bar is labelled: a zero-curtailment series draws no bar at all,
        # and without a number it reads as missing rather than as zero.
        for bar, value in zip(bars, values, strict=True):
            ax.annotate(f"{value:.1f}", (bar.get_x() + bar.get_width() / 2, value),
                        ha="center", va="bottom", fontsize=7.5, color=INK_2,
                        xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(x, [f"{n} plant" if n == 1 else f"{n} plants" for n in der_counts])
    _clean(ax, "Curtailed energy (% of available)",
           "Curtailment against generation connected", "")
    ax.set_ylim(0, max(1.0, ax.get_ylim()[1]) * 1.08)
    ax.legend(loc="upper left", ncols=2)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def rating_vs_weather(result: pd.DataFrame, path: Path, static_rating_a: float):
    """What actually drives the rating: wind speed, shaded by air temperature."""
    _style()
    ok = result[result["converged"]].dropna(subset=["wind_Z1_ms", "rating_Z1_a"])
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    scatter = ax.scatter(ok["wind_Z1_ms"], ok["rating_Z1_a"], c=ok["t_air_c"],
                         cmap=SEQUENTIAL, s=9, alpha=0.55, linewidths=0)
    _reference(ax, static_rating_a, f"Static rating {static_rating_a:.0f} A", inside=True)
    bar = fig.colorbar(scatter, ax=ax, pad=0.02)
    bar.set_label("Air temperature (°C)", color=INK_2)
    bar.outline.set_visible(False)
    _clean(ax, "Operative rating (A)", "Rating against wind speed at conductor height",
           "Wind speed (m/s)")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def storage_operation(cfg, result: pd.DataFrame, path: Path):
    """Export at the interface and the state of charge behind it."""
    _style()
    ok = result[result["converged"]]
    fig, axes = plt.subplots(2, 1, figsize=(10, 5.4), sharex=True,
                             gridspec_kw={"height_ratios": [1.25, 1]})

    axes[0].plot(ok.index, ok["pcc_p_mw"], color=SERIES[0], lw=1.3, label="Export at interface")
    axes[0].plot(ok.index, ok["storage_p_grid_mw"], color=SERIES[1], lw=1.3,
                 label="Storage power (+ discharge)")
    _reference(axes[0], cfg.export_cap_mw, f"Export cap {cfg.export_cap_mw:.0f} MW")
    _clean(axes[0], "MW", "Interface export and storage dispatch")
    axes[0].set_ylim(top=cfg.export_cap_mw * 1.42)
    axes[0].legend(loc="upper left", ncols=2, framealpha=0.0)

    axes[1].plot(ok.index, ok["storage_soc_pct"], color=SERIES[2], lw=1.6,
                 label="State of charge")
    _reference(axes[1], 100 * C.BESS_SOC_RESERVE_FLOOR, "Reserve floor")
    _clean(axes[1], "State of charge (%)", "")
    axes[1].set_ylim(0, 118)
    _legend_below(axes[1], ncols=2, pad=0.30)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def voltage_profile(result: pd.DataFrame, path: Path):
    """Voltage along the corridor, from the head to the grid interface."""
    _style()
    order = ["SUB_A", "TAP_PV", "TAP_W", "SUB_C", "TAP_B", "PCC"]
    ok = result[result["converged"]]
    cols = [f"v_{b}_pu" for b in order]
    fig, ax = plt.subplots(figsize=(8, 4.0))
    x = np.arange(len(order))
    low = [ok[c].quantile(0.01) for c in cols]
    high = [ok[c].quantile(0.99) for c in cols]
    median = [ok[c].median() for c in cols]

    ax.fill_between(x, low, high, color=SERIES[0], alpha=0.16, lw=0,
                    label="1st to 99th percentile")
    ax.plot(x, median, color=SERIES[0], lw=2.0, marker="o", ms=5, label="Median")
    _reference(ax, C.V_MAX_PU, "Upper band")
    _reference(ax, C.V_MIN_PU, "Lower band")
    ax.set_xticks(x, order)
    _clean(ax, "Voltage (pu)", "Voltage along the corridor")
    _legend_below(ax, ncols=2)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def run_figures(cfg, result: pd.DataFrame, out_dir: Path) -> list:
    """Figures that describe one run."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    rating_timeseries(cfg, result, out_dir / f"{cfg.stem}_rating.png")
    made.append(out_dir / f"{cfg.stem}_rating.png")
    voltage_profile(result, out_dir / f"{cfg.stem}_voltage.png")
    made.append(out_dir / f"{cfg.stem}_voltage.png")
    if cfg.dlr_mode == 2:
        rating_vs_weather(result, out_dir / f"{cfg.stem}_rating_drivers.png",
                          cfg.static_rating_a)
        made.append(out_dir / f"{cfg.stem}_rating_drivers.png")
    if cfg.storage_enabled:
        storage_operation(cfg, result, out_dir / f"{cfg.stem}_storage.png")
        made.append(out_dir / f"{cfg.stem}_storage.png")
    return made
