"""Wind and solar dispatch.

Wind output comes from a generic 6 MW turbine power curve. The reference wind
field is extrapolated to hub height using the shear exponent implied by the two
measurement heights, then corrected for air density before the curve is
evaluated, and finally derated for wake and availability losses.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import constants as C

CUT_IN_MS = 3.0
CUT_OUT_MS = 25.0
FARM_EFFICIENCY = 0.92          # wake, electrical and availability losses

# Generic 6 MW onshore turbine, power in kW against wind speed in m/s.
_CURVE_MS = np.array([
    0.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5,
    9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 25.0, 25.5,
])
_CURVE_KW = np.array([
    0.0, 0.0, 60.0, 190.0, 350.0, 565.0, 800.0, 1100.0, 1450.0, 1860.0,
    2340.0, 2890.0, 3490.0, 4140.0, 4760.0, 5350.0, 5680.0, 5850.0, 5950.0,
    6000.0, 6000.0, 0.0,
])

# Site wind-speed multipliers: the farms sit at different exposures, so they do
# not all reach rated output on the same quarter hour.
SITE_EXPOSURE = {"WF_1": 1.02, "WF_2": 1.00, "WF_3": 0.96}


def shear_exponent(v_low, v_high, z_low=10.0, z_high=C.DLR_REF_HEIGHT_M):
    """Power-law shear exponent implied by wind speeds at two heights."""
    v_low = np.maximum(np.asarray(v_low, dtype=float), 1e-3)
    v_high = np.maximum(np.asarray(v_high, dtype=float), 1e-3)
    alpha = np.log(v_high / v_low) / np.log(z_high / z_low)
    return np.clip(alpha, 0.05, 0.40)


def wind_at_hub(v_low, v_high, hub_m=C.HUB_HEIGHT_M, z_high=C.DLR_REF_HEIGHT_M):
    """Extrapolate the reference wind field to hub height."""
    alpha = shear_exponent(v_low, v_high)
    return np.asarray(v_high, dtype=float) * (hub_m / z_high) ** alpha


def air_density(t_air_c, elevation_m=C.DLR_SITE_ELEVATION_M):
    """Air density [kg/m3] from temperature and a barometric pressure estimate."""
    t_k = np.asarray(t_air_c, dtype=float) + 273.15
    pressure = 101325.0 * np.exp(-9.80665 * elevation_m / (287.05 * t_k))
    return pressure / (287.05 * t_k)


def turbine_power_mw(v_hub, t_air_c):
    """Per-turbine output [MW], density corrected per IEC 61400-12."""
    rho = air_density(t_air_c)
    v_eq = np.asarray(v_hub, dtype=float) * (rho / 1.225) ** (1.0 / 3.0)
    kw = np.interp(v_eq, _CURVE_MS, _CURVE_KW, left=0.0, right=0.0)
    return kw / 1000.0


def wind_dispatch(weather: pd.DataFrame) -> pd.DataFrame:
    """Available active power [MW] for each wind farm, before any curtailment."""
    v_low = np.hypot(weather["u10_ms"].to_numpy(), weather["v10_ms"].to_numpy())
    v_high = np.hypot(weather["u100_ms"].to_numpy(), weather["v100_ms"].to_numpy())
    v_hub = wind_at_hub(v_low, v_high)
    t_air = weather["t_air_c"].to_numpy()

    out = pd.DataFrame(index=weather.index)
    for farm, exposure in SITE_EXPOSURE.items():
        per_turbine = turbine_power_mw(v_hub * exposure, t_air)
        total = per_turbine * C.N_TURBINES[farm] * FARM_EFFICIENCY
        out[farm] = np.clip(total, 0.0, C.DER_RATING_MW[farm])
    return out.round(4)


def capacity_factor(dispatch: pd.DataFrame) -> dict:
    """Mean output as a fraction of rating, per farm."""
    return {c: float(dispatch[c].mean() / C.DER_RATING_MW[c]) for c in dispatch.columns}
