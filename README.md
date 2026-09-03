# DLR Hosting Capacity Simulator

[![CI](https://github.com/tubnguyen/dlr-hosting-capacity-sim/actions/workflows/ci.yml/badge.svg)](https://github.com/tubnguyen/dlr-hosting-capacity-sim/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An open tool for hosting-capacity studies on a 110 kV distribution network, with
dynamic line rating in the loop.**

Give it a corridor, a weather series and a set of plants, and it answers: how much wind
and solar can this network carry before something binds, what binds first, and how much of
that limit is physics rather than a conservative assumption about the weather.

Quasi-static AC power flow at 15-minute resolution (pandapower), IEEE 738-2012 conductor
heat balance, coordinated voltage control, a four-level constraint hierarchy and a
minimal-curtailment search. Three line-rating modes share one thermal model, so a static
rating and a dynamic one are comparable rather than two separate studies.

A synthetic reference dataset ships with the repository so it runs out of the box — but
the weather, the demand, the plants and the network are all meant to be replaced with your
own. **[Jump to running it on your own data](#running-it-on-your-own-data).**

---

## What is dynamic line rating?

An overhead line's current limit exists to keep the conductor below a design temperature —
80 °C here. Past that it sags too far and loses ground clearance. How much current reaches
that temperature depends entirely on the weather cooling the conductor.

A **static rating** picks one worst case — a hot, still, sunny day — and applies the
resulting current limit all year. It is safe by construction and conservative nearly all of
the time: in cold, windy weather the same conductor could carry much more current without
ever approaching its temperature limit.

**Dynamic line rating (DLR)** computes the limit from the weather the line is actually in.
Convection dominates the heat balance, so wind speed and its angle against the line matter
most, with air temperature and sunshine behind them. Nothing is rebuilt and no safety limit
is relaxed — the same 80 °C is enforced against real cooling instead of assumed cooling.

For a network hosting wind this correlation works in your favour: the windy hours when the
corridor is most congested are the hours the wind is cooling it hardest.

The tool implements three rating modes from a single heat balance:

| Mode | `--dlr` | Air temp | Irradiance | Wind | What it needs in practice |
|---|---|---|---|---|---|
| Static | `0` | reference | reference | reference | nothing — the nameplate rating |
| Ambient-adjusted | `1` | measured | measured | fixed low value, perpendicular | a temperature sensor |
| Full weather | `2` | measured | measured | measured, per-zone angle of attack | wind measurement or forecast |

Mode 0 is not a separate model. It is the *same* heat balance evaluated at the reference
conditions the static rating is declared for, and the code asserts it reproduces the
declared rating to within 0.4 % — otherwise any apparent DLR headroom could just be two
models disagreeing with each other.

![Corridor rating against the current carried](docs/figures/rating.png)

*Example output: the operative rating against the current the network is carrying. The gap
between them is headroom a static rating cannot see.*

---

## The network: 110 kV distribution

The modelled system is a **110 kV distribution network** — the level a distribution system
operator runs across much of Northern and Central Europe — connecting a cluster of wind and
solar plants through one congested export corridor to a stronger grid.

```
 WF_3 72MW                          WF_1 90MW ─── WF_2 108MW
     │                                                  │
     │ 21 km                                     16 km  │
     ▼                                                  ▼
  SUB_A ══════ TAP_PV ══════════ TAP_W ══════ SUB_C ══════ TAP_B ══════ PCC ══> grid
  110/20kV  6km   │        8km          2.5km       16.5km   │      0.7km    │
     │            │ PV_1 60MW                                │ BESS 30MW      │ 15 km
   load           │                                          │ / 60 MWh       ▼
                  └── zone Z1 (110°) ──┴── zone Z2 (95°) ────┘              SUB_E
                                                                              │ 11 km
                     ══ constrained export corridor, 33.7 km               SUB_B
```

The double line is the export corridor and the binding constraint: 33.7 km of single ACSR
conductor, 800 A static rating, 152 MVA, against 330 MW of connected generation. It is
split into two rating zones with different mean bearings, so the same wind gives each a
different angle of attack.

Everything a distribution operator actually controls is in the model: the 110/20 kV
on-load tap changers, an MV shunt reactor, plant reactive droop, the ±5 % voltage band and
a contracted export cap at the interface. Full parameter tables in
[docs/network.md](docs/network.md).

All values are generic — public standards, typical datasheets and round design figures.
Nothing is measured or operator-specific.

---

## Quickstart

```bash
git clone https://github.com/tubnguyen/dlr-hosting-capacity-sim
cd dlr-hosting-capacity-sim
pip install -e ".[dev]"

# one scenario: full-weather DLR, all plants connected, storage on
corridor-sim --preset dlr2_der4_bess --days 30

# the same network on a static rating, for comparison
corridor-sim --preset static_der4 --days 30

# the whole scenario matrix in parallel, plus a comparison table and figures
python scripts/run_matrix.py --days 30 --jobs 8

pytest -q
```

Runs land in `runs/<scenario>/` (not committed — the per-step time series is large).

<details>
<summary>Command-line options</summary>

```
--preset PRESET      named scenario (see the matrix below)
--conductor {single,twin}
--dlr {0,1,2}        0 static, 1 ambient-adjusted, 2 full weather
--control {droop,cosphi}
--der WF_1,WF_2,...  which plants to connect
--storage            enable the battery
--no-curtailment     record what would bind instead of acting on it
--export-cap MW
--start YYYY-MM-DD --days N
--data-dir DIR       your own input time series
--out DIR --label NAME --no-plots
```
</details>

---

## Running it on your own data

Three things make a study yours: the time series, the network parameters, and the scenario
you ask for. Nothing needs to be forked — the first is a directory, the second is one
constants file, the third is a flag.

### 1. Supply your own time series

Put four CSVs in a directory and point the tool at it:

```bash
corridor-sim --data-dir /path/to/my_data --dlr 2 --start 2024-01-01 --days 365
```

The first column of every file is the timestamp and becomes the index. Timestamps without
a zone are read as UTC; anything zoned is converted to UTC.

| File | Column | Unit | Meaning |
|---|---|---|---|
| `weather.csv` | `t_air_c` | °C | air temperature — conductor heat balance and air density |
| | `u100_ms`, `v100_ms` | m/s | wind vector at 100 m — line cooling, angle of attack, and turbine hub-height wind |
| | `u10_ms`, `v10_ms` | m/s | wind vector at 10 m — sets the shear exponent between the two heights |
| | `ghi_wm2` | W/m² | global horizontal irradiance — solar gain on the conductor |
| `load.csv` | `p_sub_a_mw`, `q_sub_a_mvar` | MW, MVAr | demand at the 110/20 kV substation on the corridor |
| | `p_sub_b_mw`, `q_sub_b_mvar` | MW, MVAr | demand at the downstream 110/20 kV substation |
| | `p_agg_mw`, `q_agg_mvar` | MW, MVAr | aggregated downstream demand |
| | `q_shunt_a_mvar` | MVAr | switched capacitor step at SUB_A |
| `pv_generation.csv` | `p_ac_mw` | MW | AC export of the solar plant |
| `reserve_activation.csv` | `activation_up_mw` | MW | upward balancing-energy activation for the battery |

Rules the loader enforces, rather than papering over:

* **Any resolution finer or coarser than 15 minutes is fine.** Coarser series (hourly
  weather, for instance) are interpolated in time onto the simulation grid.
* **Coverage is checked.** If a file does not span the requested window the run stops. It
  will not substitute zeros for missing weather and hand you a plausible-looking answer.
* **Duplicate timestamps are an error**, not a silent last-value-wins.
* Missing columns raise by name, so a schema mistake surfaces immediately.

Wind generation is **modelled from the weather**, not supplied: hub-height extrapolation by
the shear exponent implied by your 10 m and 100 m fields, an air-density correction, a
generic 6 MW power curve, then wake and availability losses ([`ders.py`](src/corridor_sim/ders.py)).
To drive the farms from measured output instead, replace `ders.wind_dispatch()` — it
returns one column of available MW per farm on the simulation index.

To see how a self-consistent dataset is built — weather, solar, demand and reserve driven
by the same underlying processes — read [`data/generate.py`](data/generate.py) and
[`data/README.md`](data/README.md). Pairing independent series is the quiet way to build a
study that never sees the coincidences that actually cause congestion.

### 2. Describe your own network

Every network and physical parameter lives in one file,
[`src/corridor_sim/constants.py`](src/corridor_sim/constants.py). The ones most studies
change first:

| What you want to change | Constant |
|---|---|
| Nominal voltages, frequency, time step | `V_HV_KV`, `V_MV_KV`, `F_HZ`, `DT_H` |
| Voltage band | `V_MAX_PU`, `V_MIN_PU` |
| Conductor: diameter, resistance, emissivity, design temperature | `COND_*`, `T_COND_MAX_C` |
| Conductor build options and their static ratings | `CONDUCTOR_OPTIONS` |
| Corridor section lengths | `LEN_CORR_*` |
| Rating zones and their mean bearings | `DLR_ZONE_AZIMUTH_DEG` |
| Conductor height, terrain roughness, site elevation | `DLR_CONDUCTOR_HEIGHT_M`, `DLR_ROUGHNESS_M`, `DLR_SITE_ELEVATION_M` |
| Conditions the static rating is declared at | `STATIC_REF_CONDITIONS` |
| Plants: names, ratings, turbine count and hub height | `DER_RATING_MW`, `N_TURBINES`, `HUB_HEIGHT_M` |
| Transformers and the tap changer | `DSO_TRAFOS`, `OLTC_TAP`, `WF_TRAFO_*` |
| Voltage-control deadbands and move budgets | `OLTC_DEADBAND_KV`, `REACTOR_DEADBAND_KV`, `*_MOVE_BUDGET` |
| Export cap and reactive window at the interface | `EXPORT_CAP_MW`, `Q_WINDOW_*` |
| Battery rating, energy and efficiency | `BESS_*` |
| External grid strength | `SRC_R_OHM`, `SRC_X_OHM` |

Topology itself is built in [`network.py`](src/corridor_sim/network.py) — one function,
readable top to bottom, if your corridor has a different shape.

Run-level choices are not constants; they are `Config` fields in
[`config.py`](src/corridor_sim/config.py), settable from the CLI or in Python:

```python
from corridor_sim.config import build_config
from corridor_sim.cli import run_scenario

cfg = build_config(dlr_mode=2, conductor="single", days=365,
                   data_dir="/path/to/my_data", export_cap_mw=250.0,
                   der_enabled=["WF_1", "WF_2"], label="my_case")
metrics, paths = run_scenario(cfg)
```

Config validation is fail-fast: an inconsistent knob raises before a run costs time.

### 3. Ask a hosting-capacity question

The usual sequence is to hold the network fixed and sweep what you are actually deciding:

```bash
# does the corridor take a third plant on a static rating?
corridor-sim --dlr 0 --der WF_1,WF_2,WF_3 --days 365

# the same fleet, ambient-adjusted rating — the cheap DLR deployment
corridor-sim --dlr 1 --der WF_1,WF_2,WF_3 --days 365

# and with a wind measurement
corridor-sim --dlr 2 --der WF_1,WF_2,WF_3 --days 365

# what a reconductoring would buy instead
corridor-sim --dlr 0 --conductor twin --der WF_1,WF_2,WF_3 --days 365
```

`--no-curtailment` records what *would* bind without acting on it, which is the honest way
to size a constraint before deciding how to relieve it.

### 4. What a run writes

Each run writes to its own directory under `runs/`:

| File | Contents |
|---|---|
| `*_timeseries.csv` | every solved quantity per 15-minute step: flows, voltages, ratings, conductor temperature, curtailment and its cause, tap and reactor positions, convergence and droop residual |
| `*_violations.csv` | one row per violated interval, with level, asset and margin |
| `*_seasonal.csv` | monthly breakdown — DLR value is strongly seasonal |
| `*_metrics.json` | headline numbers: available, delivered and curtailed energy, rating statistics, hours over each limit, losses, convergence rates |
| `*_summary.txt` | the same, printed for a human |
| `figures/` | operative rating against carried current, voltage profile along the corridor, plus rating drivers under mode 2 and storage operation when the battery is on |

`scripts/run_matrix.py` runs many scenarios in parallel and joins their metrics into one
comparison table.

---

## Methodology

Full derivations and the reasoning behind each choice: [docs/methodology.md](docs/methodology.md).

### Line rating — IEEE 738-2012

Both directions of the heat balance are implemented. The **forward** solve fixes the
conductor at its design temperature and returns the ampacity the weather supports. The
**inverse** solve fixes the current the network is carrying and returns the temperature the
conductor actually reaches — the check that says whether a rating was safe rather than
merely permitted.

Convection takes the larger of the two forced-convection correlations and the
natural-convection floor, so still air is handled without a discontinuity. The wind angle
of attack is folded onto [0°, 90°]: wind along the line cools far less than wind across it.
Wind is brought from the 100 m reference field down to conductor height by a neutral-stability
log law over a roughness length, with the geometry validated rather than assumed.

![What drives the rating](docs/figures/rating_drivers.png)

### Coordinated voltage control

| Actuator | Role | Deadband | Budget per interval |
|---|---|---|---|
| MV shunt reactor | fine, primary | ±0.18 kV | 4 steps |
| On-load tap changer | coarse, backup | ±0.50 kV | 3 taps |
| Plant reactive droop | continuous | ±0.010 pu | — |

The reactor acts first, the network is re-solved, and only then is the tap changer tested
against what remains — the same deadband on both produces a limit cycle. Both freeze after
reversing direction within an interval, because an actuator that has reversed has bracketed
its setpoint.

The droop loop re-solves after every reactive update, so recorded reactive power is always
a solved value rather than a command the network never saw. Convergence requires both a
settled voltage *and* a closed residual, and the residual is written to every row so the
claim is checkable. Each unit under-relaxes adaptively, halving its damping on reversal.

At extreme loading the droop has no fixed point — a genuine property of a weak corridor.
The loop backtracks to the last solved state instead of failing, so a timestep always ends
solvable and curtailment can still act on it.

### Constraint hierarchy

Four levels, all evaluated on every scan:

| | Constraint | Actionable |
|---|---|---|
| L1 | Over-voltage | yes |
| L2 | Under-voltage | no — cutting active power makes it worse |
| L3 | Thermal (corridor / distribution / plant, scanned separately) | yes |
| L4 | Export cap | yes |

Returning only the first violated level would let a non-actionable under-voltage hide a
coincident thermal overload, during exactly the hours that matter most. Splitting thermal
by asset owner is what stops a plant-owned transformer from silently setting a *network*
hosting capacity.

### Minimal curtailment

Curtailment is a one-dimensional monotone problem — total megawatts removed, shared pro
rata — so the search brackets the feasibility boundary, interpolates on a signed margin,
and applies **the smallest cut that clears**, not the last cut tried. The residual bracket
width is reported, so discretisation error is a number rather than an unknown. Every
curtailed megawatt-hour is attributed to exactly one cause.

Trials evaluate what will actually be applied — the droop is re-settled inside every trial —
and each trial restarts from the entry reactive power, so the bracket stays meaningful.

### Storage and market layer

Two-phase dispatch: an intent set before the solve, reconciled against the export headroom
the solved network leaves. Generation has priority, so a delivery that would breach the cap
is clamped and the shortfall recorded. State of charge integrates on realised power only,
and when a limit clamps the result the realised power is backed out of the actual energy
change so the two stay consistent.

Reach is asymmetric both ways: a battery cannot relieve a thermal overload upstream of its
tap, but it does share the corridor sections downstream of it, where its output competes
with generation for the same capacity. Whether storage relieves congestion or adds to it is
therefore an outcome of siting and charging strategy — both configurable
(`storage_connection`, `storage_charge_source`) rather than assumed.

---

## Scenario matrix

21 preset scenarios: four rating-and-build options against increasing generation, each with
and without storage. Useful as a template for your own sweep.

| | 1 plant | 2 plants | 3 plants | 4 plants | 4 + storage |
|---|---|---|---|---|---|
| **Static rating** | `static_der1` | `static_der2` | `static_der3` | `static_der4` | `static_der4_bess` |
| **Ambient-adjusted** | `dlr1_der1` | `dlr1_der2` | `dlr1_der3` | `dlr1_der4` | `dlr1_der4_bess` |
| **Full-weather DLR** | `dlr2_der1` | `dlr2_der2` | `dlr2_der3` | `dlr2_der4` | `dlr2_der4_bess` |
| **Twin conductor** | `twin_der1` | `twin_der2` | `twin_der3` | `twin_der4` | `twin_der4_bess` |

Plus `baseline`, the network with no generation connected.

Every element is built on every run: disconnecting a plant sets its power to zero rather
than removing it from the network, so all scenarios share one topology and a difference
between them is a hosting-capacity difference, not a topology difference.

---

## Repository layout

```
src/corridor_sim/
  config.py        immutable Config, preset table, CLI
  constants.py     network and physical parameters  ← edit for your own network
  network.py       pandapower model
  dlr.py           IEEE 738 heat balance, three rating modes
  ders.py          wind power curve, shear, density correction
  dataio.py        input loading and time alignment  ← your CSV schema
  controls.py      tap changer, reactor, droop, regulation loop
  constraints.py   four-level hierarchy, owner groups
  curtailment.py   minimal-curtailment search
  storage.py       battery dispatch, state of charge, reserve
  simulate.py      15-minute loop
  report.py        metrics, summaries, seasonal tables
  plots.py         figures
data/              synthetic reference dataset + its generator
scripts/           parallel matrix runner
docs/              methodology, network description
tests/             unit and end-to-end tests
```

---

## Verification

`pytest` covers the parts where a silent error would be most expensive:

* forward and inverse IEEE 738 solves agree — the current a rating permits heats the
  conductor to exactly the temperature the rating assumed;
* the model reproduces the declared static rating at its reference conditions;
* reactive power reported for a step matches what the solved network delivered;
* a non-actionable under-voltage does not mask a coincident thermal overload;
* the applied curtailment clears the violation and a meaningfully smaller cut does not;
* storage round-trip energy matches the stated efficiency, and clamped power matches the
  actual state-of-charge change;
* delivered plus curtailed energy equals available energy, every interval.

CI runs the suite on Python 3.11 and 3.12, plus a one-day smoke run of the CLI and the
matrix runner.

---

## Scope and limitations

Quasi-static and intact-network: no contingency analysis, no protection or stability study,
no sag and clearance calculation — the design conductor temperature stands in for the
clearance limit that governs a real line. One weather point represents the corridor, and
each rating zone carries a single mean bearing, so a per-span rating would come out lower
than a zone-mean one. Full list in [docs/methodology.md](docs/methodology.md#6-what-is-deliberately-not-modelled).

The shipped dataset describes a cold, windy, high-latitude site, which is where dynamic
line rating has most to offer. DLR value is strongly seasonal — every run writes a monthly
breakdown for exactly that reason, and a short winter window will flatter it against an
annual average.

## License

MIT — see [LICENSE](LICENSE).
