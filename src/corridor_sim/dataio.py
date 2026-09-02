"""Input loading and alignment onto the simulation time grid.

A missing file, a gap in coverage or a duplicated timestamp raises here rather
than being filled in silently: a run that quietly substituted zeros for missing
weather would still produce a plausible-looking result.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import constants as C
from . import ders, dlr

FILES = {
    "weather": "weather.csv",
    "load": "load.csv",
    "pv": "pv_generation.csv",
    "reserve": "reserve_activation.csv",
}


def simulation_index(cfg) -> pd.DatetimeIndex:
    return pd.date_range(cfg.start_ts, cfg.end_ts, freq=f"{int(C.DT_H * 60)}min",
                         tz="UTC", inclusive="left")


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python data/generate.py` to create the dataset.")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.DatetimeIndex(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    if not df.index.is_unique:
        raise ValueError(f"{path.name} has duplicate timestamps")
    return df.sort_index()


def _align(df: pd.DataFrame, index: pd.DatetimeIndex, name: str) -> pd.DataFrame:
    """Reindex onto the simulation grid, interpolating coarser inputs in time."""
    out = df.reindex(index.union(df.index)).interpolate(
        method="time", limit_direction="both").reindex(index)
    if out.isna().any().any():
        raise ValueError(f"{name} does not cover {index[0]} to {index[-1]}")
    return out


def load_inputs(cfg) -> dict:
    """Read every input and return frames aligned to the simulation index."""
    index = simulation_index(cfg)
    data_dir = Path(cfg.data_dir)
    raw = {k: _read(data_dir / v) for k, v in FILES.items()}

    weather_15 = _align(raw["weather"], index, "weather.csv")
    inputs = {
        "index": index,
        "weather": weather_15,
        "load": _align(raw["load"], index, "load.csv"),
        "wind": ders.wind_dispatch(weather_15),
        "reserve": _align(raw["reserve"], index, "reserve_activation.csv")["activation_up_mw"],
    }
    pv = _align(raw["pv"], index, "pv_generation.csv")["p_ac_mw"]
    inputs["pv"] = pv.clip(0.0, C.DER_RATING_MW["PV_1"])
    inputs["rating_weather"] = (dlr.prepare_weather(cfg, raw["weather"], index)
                                if cfg.dlr_mode > 0 else None)
    return inputs
