"""Generate the synthetic reference-year dataset shipped with this repo.

Everything here is fabricated from a fixed seed: no measured or proprietary
data is used. Output is self-consistent (PV comes from the same irradiance
the DLR engine sees, load correlates with temperature) so the simulation
exercises realistic coincidences between generation, demand and weather.

    python data/generate.py [--year 2024] [--seed 20240101] [--out data]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

LAT_DEG = 63.0          # notional site latitude, drives the solar geometry
STEP_MIN = 15


def _hourly_index(year: int) -> pd.DatetimeIndex:
    return pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h",
                         tz="UTC", inclusive="left")


def _quarter_index(year: int) -> pd.DatetimeIndex:
    return pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq=f"{STEP_MIN}min",
                         tz="UTC", inclusive="left")


def _ar1(rng: np.random.Generator, n: int, rho: float, sigma: float) -> np.ndarray:
    """Stationary AR(1) noise — gives weather its hour-to-hour persistence."""
    e = rng.normal(0.0, sigma, n)
    out = np.empty(n)
    out[0] = e[0]
    for i in range(1, n):
        out[i] = rho * out[i - 1] + e[i]
    return out


def _solar_elevation(idx: pd.DatetimeIndex) -> np.ndarray:
    """Solar elevation angle [rad], standard declination/hour-angle geometry."""
    doy = idx.dayofyear.to_numpy()
    hour = idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0
    decl = np.radians(23.45) * np.sin(2 * np.pi * (284 + doy) / 365.0)
    lat = math.radians(LAT_DEG)
    omega = np.radians(15.0 * (hour - 12.0))
    sin_h = np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(omega)
    return np.arcsin(np.clip(sin_h, -1.0, 1.0))


def make_weather(year: int, rng: np.random.Generator) -> pd.DataFrame:
    """Hourly ambient temperature, 10 m / 100 m wind vectors and global irradiance."""
    idx = _hourly_index(year)
    n = len(idx)
    doy = idx.dayofyear.to_numpy()
    hour = idx.hour.to_numpy()

    seasonal = -np.cos(2 * np.pi * (doy - 15) / 365.25)
    diurnal = -np.cos(2 * np.pi * (hour - 14) / 24.0)
    t_c = 3.0 + 16.0 * seasonal + (2.0 + 2.0 * np.clip(seasonal, 0, None)) * diurnal
    t_c += _ar1(rng, n, 0.92, 1.6)

    # Wind: Weibull-like speed with a winter-heavy seasonal cycle, plus a
    # slowly rotating direction so the DLR attack angle varies through the year.
    speed_base = 7.5 - 1.8 * seasonal
    sigma_ln = 0.45                                 # stationary log-std of the gust factor
    rho = 0.95
    gust = np.exp(_ar1(rng, n, rho, sigma_ln * math.sqrt(1 - rho ** 2)))
    gust /= math.exp(0.5 * sigma_ln ** 2)           # normalise to unit mean
    ws100 = np.clip(speed_base * gust, 0.2, 28.0)
    ws10 = ws100 * (10.0 / 100.0) ** 0.16          # power-law shear to 10 m

    direction = (np.degrees(np.cumsum(_ar1(rng, n, 0.995, 0.05))) + 210.0) % 360.0
    theta = np.radians(direction)
    out = pd.DataFrame(index=idx)
    out.index.name = "time"
    out["t_air_c"] = t_c.round(2)
    out["u10_ms"] = (-ws10 * np.sin(theta)).round(3)
    out["v10_ms"] = (-ws10 * np.cos(theta)).round(3)
    out["u100_ms"] = (-ws100 * np.sin(theta)).round(3)
    out["v100_ms"] = (-ws100 * np.cos(theta)).round(3)

    # Clear-sky beam attenuated by a persistent cloud-cover process.
    elev = _solar_elevation(idx)
    clear = np.clip(1050.0 * np.sin(elev), 0.0, None)
    cloud = np.clip(0.5 + 0.32 * _ar1(rng, n, 0.90, 1.0), 0.0, 1.0)
    out["ghi_wm2"] = (clear * (1.0 - 0.78 * cloud)).round(1)
    return out


def make_pv(weather: pd.DataFrame, idx15: pd.DatetimeIndex, p_rated_mw: float,
            rng: np.random.Generator) -> pd.DataFrame:
    """AC export of a fixed-tilt PV plant driven by the generated irradiance."""
    w = weather.reindex(idx15).interpolate(limit_direction="both")
    ghi = w["ghi_wm2"].to_numpy()
    t_air = w["t_air_c"].to_numpy()

    poa = ghi * 1.12                                   # plane-of-array uplift, fixed tilt
    t_cell = t_air + poa / 800.0 * 25.0                # NOCT-style cell temperature
    dc_ratio = 1.25                                    # DC/AC oversizing
    p_dc = p_rated_mw * dc_ratio * (poa / 1000.0) * (1.0 - 0.0035 * (t_cell - 25.0))
    p_dc *= 0.965 - 0.02 * rng.random(len(idx15))      # soiling and array losses
    p_ac = np.clip(p_dc * 0.985, 0.0, p_rated_mw)      # inverter efficiency, then clipping

    # Winter snow cover suppresses output on the coldest days.
    snow = (t_air < -4.0) & (ghi < 260.0)
    p_ac[snow] *= 0.05

    out = pd.DataFrame({"p_ac_mw": np.round(p_ac, 4)}, index=idx15)
    out.index.name = "time"
    return out


def make_load(weather: pd.DataFrame, idx15: pd.DatetimeIndex,
              rng: np.random.Generator) -> pd.DataFrame:
    """Three demand points: two MV substations and a downstream aggregate."""
    w = weather.reindex(idx15).interpolate(limit_direction="both")
    t_air = w["t_air_c"].to_numpy()
    hour = idx15.hour.to_numpy() + idx15.minute.to_numpy() / 60.0
    weekday = idx15.dayofweek.to_numpy() < 5

    shape = (0.72
             + 0.20 * np.sin(2 * np.pi * (hour - 8.5) / 24.0)
             + 0.10 * np.sin(4 * np.pi * (hour - 6.0) / 24.0))
    shape *= np.where(weekday, 1.0, 0.90)
    heating = 1.0 + 0.030 * np.clip(15.0 - t_air, 0.0, None)   # electric heating

    def _point(p_max: float, q_over_p: float, noise: float) -> tuple[np.ndarray, np.ndarray]:
        p = p_max * shape * heating * (1.0 + noise * rng.standard_normal(len(idx15)))
        p = np.clip(p, 0.05 * p_max, p_max)
        return np.round(p, 4), np.round(p * q_over_p, 4)

    p_a, q_a = _point(21.0, 0.28, 0.03)
    p_b, q_b = _point(9.5, 0.29, 0.04)
    p_agg, q_agg = _point(34.0, 0.22, 0.03)

    out = pd.DataFrame(index=idx15)
    out.index.name = "time"
    out["p_sub_a_mw"], out["q_sub_a_mvar"] = p_a, q_a
    out["p_sub_b_mw"], out["q_sub_b_mvar"] = p_b, q_b
    out["p_agg_mw"], out["q_agg_mvar"] = p_agg, q_agg
    # MV capacitor bank at substation A, switched in on high-demand quarters.
    out["q_shunt_a_mvar"] = np.where(p_a > 0.72 * 21.0, 3.0, 0.0)
    return out


def make_reserve(year: int, rng: np.random.Generator) -> pd.DataFrame:
    """Hourly upward balancing-energy activation signal [MW] for the storage layer."""
    idx = _hourly_index(year)
    n = len(idx)
    active = rng.random(n) < 0.14
    depth = rng.gamma(shape=2.0, scale=9.0, size=n)
    out = pd.DataFrame({"activation_up_mw": np.round(np.where(active, depth, 0.0), 2)},
                       index=idx)
    out.index.name = "time"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--seed", type=int, default=20240101)
    ap.add_argument("--pv-mw", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    idx15 = _quarter_index(args.year)

    weather = make_weather(args.year, rng)
    files = {
        "weather.csv": weather,
        "pv_generation.csv": make_pv(weather, idx15, args.pv_mw, rng),
        "load.csv": make_load(weather, idx15, rng),
        "reserve_activation.csv": make_reserve(args.year, rng),
    }
    for name, df in files.items():
        path = args.out / name
        df.to_csv(path, lineterminator="\n")
        print(f"  {path.name:24s} {len(df):6d} rows  {path.stat().st_size / 1e6:5.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
