# Dataset

Everything in this directory is synthetic. It is generated from a fixed seed by
`generate.py`, so the results are reproducible and no measured or proprietary
data is involved.

Regenerate with:

```bash
python data/generate.py --year 2024 --seed 20240101
```

| File | Resolution | Columns |
|---|---|---|
| `weather.csv` | hourly | `t_air_c`, `u10_ms`, `v10_ms`, `u100_ms`, `v100_ms`, `ghi_wm2` |
| `load.csv` | 15 min | active and reactive demand at two substations and one downstream aggregate, plus a switched capacitor step |
| `pv_generation.csv` | 15 min | AC export of the solar plant, `p_ac_mw` |
| `reserve_activation.csv` | hourly | upward balancing-energy activation, `activation_up_mw` |

## How it is built

The four files are mutually consistent, which matters: a study that pairs
independent weather and demand series will never see the coincidences that
actually cause congestion.

* **Weather.** Air temperature is a seasonal cycle plus a diurnal cycle plus
  persistent AR(1) noise. Wind speed is a winter-heavy seasonal mean multiplied
  by a mean-normalised log-normal gust factor, with a slowly rotating direction
  so the wind angle of attack against the line varies through the year.
  Irradiance is clear-sky beam from solar geometry at 63° latitude, attenuated
  by a persistent cloud process.
* **Solar.** Driven by the same irradiance the line-rating engine sees, through
  a plane-of-array uplift, a cell-temperature derating, DC oversizing and
  inverter clipping, with output suppressed on cold dark days for snow cover.
* **Demand.** A daily and weekly shape scaled by an electric-heating term that
  responds to the same air temperature.
* **Reserve activation.** A sparse upward-regulation signal: roughly 14 % of
  hours active, with a gamma-distributed depth.

Resulting annual statistics: mean wind speed 7.3 m/s at 100 m, air temperature
−24 °C to +31 °C, global irradiance 925 kWh/m², wind capacity factor 0.44–0.47
per farm and solar capacity factor 0.13 — a cold, windy, high-latitude site,
which is where dynamic line rating has the most to offer.
