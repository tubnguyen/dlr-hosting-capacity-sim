"""Study configuration: one immutable Config, a preset table and a CLI.

Every knob the study exposes lives here. No module below reads a setting from
a global, so a run is fully described by the Config it was given.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import constants as C

DER_NAMES = tuple(C.DER_RATING_MW)
ALL_ON = dict.fromkeys(DER_NAMES, True)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Config:
    """A complete study specification."""

    # Network build
    conductor: str = "single"                       # "single" | "twin"
    wf_trafo_units: int = C.WF_TRAFO_UNITS          # 2 = as built, 1 = one unit out
    wf_trafo_rating: str = "onaf"                   # "onaf" (forced) | "onan" (natural)

    # Generation
    der_enabled: Mapping[str, bool] = field(default_factory=lambda: dict(ALL_ON))
    control_mode: str = "droop"                     # "droop" | "cosphi"
    pv_control_mode: str | None = None           # None inherits control_mode
    cosphi_sign: str = "absorb"                     # "absorb" | "inject"
    droop_p_min_frac: float = C.DROOP_P_MIN_FRAC
    droop_measurement: str = "local"                # "local" | "pilot_tap_w" | "pilot_sub_a"

    # Line rating
    dlr_mode: int = 1                               # 0 static | 1 ambient-adjusted | 2 full weather
    conductor_height_m: float = C.DLR_CONDUCTOR_HEIGHT_M
    roughness_m: float = C.DLR_ROUGHNESS_M
    displacement_m: float = C.DLR_DISPLACEMENT_M
    low_wind_ms: float = C.DLR_LOW_WIND_MS
    site_elevation_m: float = C.DLR_SITE_ELEVATION_M
    t_cond_max_c: float = C.T_COND_MAX_C

    # Congestion management
    curtailment: bool = True
    export_cap_mw: float = C.EXPORT_CAP_MW
    export_cap_basis: str = "net"                   # "net" | "gross"

    # Storage
    storage_enabled: bool = False
    storage_p_mw: float = C.BESS_P_MW
    storage_e_mwh: float = C.BESS_E_MWH
    storage_soc_init: float = 0.50
    storage_connection: str = "tie"                 # "tie" | "direct"
    storage_charge_source: str = "surplus_then_grid"
    storage_reserve_mw: float = C.BESS_P_MW
    storage_contract_mw: float = C.BESS_P_MW
    storage_q_mode: str = "droop"                   # "droop" | "cosphi" | "fixed" | "unity"
    storage_may_curtail_der: bool = False

    # Window and I/O
    start: str = "2024-01-01"
    days: int | None = 30                        # None uses `end`
    end: str = "2025-01-01"
    data_dir: Path = DATA_DIR
    out_dir: Path = Path("runs")
    label: str = ""

    # ── Derived ──────────────────────────────────────────────────────────────
    @property
    def pv_mode(self) -> str:
        return self.pv_control_mode or self.control_mode

    @property
    def active_der(self) -> list:
        return [n for n in DER_NAMES if self.der_enabled.get(n, False)]

    @property
    def n_der(self) -> int:
        return len(self.active_der)

    @property
    def der_fleet_mw(self) -> float:
        return sum(C.DER_RATING_MW[n] for n in self.active_der)

    @property
    def line(self) -> dict:
        return C.CONDUCTOR_OPTIONS[self.conductor]

    @property
    def bundle_n(self) -> int:
        return self.line["bundle_n"]

    @property
    def static_rating_a(self) -> float:
        """Static (seasonal) bundle ampacity used when dlr_mode == 0."""
        return self.line["max_i_ka"] * 1000.0

    @property
    def cosphi_sign_factor(self) -> float:
        return -1.0 if self.cosphi_sign == "absorb" else 1.0

    @property
    def storage_droop_active(self) -> bool:
        return self.storage_enabled and self.storage_q_mode == "droop"

    @property
    def max_outer_iter(self) -> int:
        """Droop chases a moving reactive target and needs a longer budget."""
        droop = (self.control_mode == "droop" or self.pv_mode == "droop"
                 or self.storage_droop_active)
        return C.DROOP_MAX_ITER if droop else C.COSPHI_MAX_ITER

    @property
    def start_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.start, tz="UTC")

    @property
    def end_ts(self) -> pd.Timestamp:
        if self.days is not None:
            return self.start_ts + pd.Timedelta(days=self.days)
        return pd.Timestamp(self.end, tz="UTC")

    @property
    def n_steps(self) -> int:
        return int(round((self.end_ts - self.start_ts).total_seconds() / 3600 / C.DT_H))

    @property
    def q_window_mvar(self) -> float:
        """Permitted reactive exchange at the PCC, set by the installed fleet."""
        return min(C.Q_WINDOW_FRAC * C.FLEET_MW, C.Q_WINDOW_CAP_MVAR)

    @property
    def soc_min_mwh(self) -> float:
        return C.BESS_SOC_MIN * self.storage_e_mwh

    @property
    def soc_max_mwh(self) -> float:
        return C.BESS_SOC_MAX * self.storage_e_mwh

    @property
    def soc_reserve_mwh(self) -> float:
        return max(C.BESS_SOC_MIN, C.BESS_SOC_RESERVE_FLOOR) * self.storage_e_mwh

    @property
    def soc_recharge_target_mwh(self) -> float:
        """Energy the battery refills to: enough to deliver the contracted
        reserve for a full hour on top of the physical floor."""
        return min(self.soc_max_mwh,
                   self.soc_min_mwh + self.storage_contract_mw / C.BESS_ETA_DIS)

    @property
    def q_limit_storage(self) -> float:
        return self.storage_p_mw * C.TAN_PHI

    @property
    def stem(self) -> str:
        """Filename stem describing this run."""
        if self.label:
            return self.label
        return (f"{self.conductor}_der{self.n_der}_{self.control_mode}"
                f"_dlr{self.dlr_mode}_bess{int(self.storage_enabled)}")


# ── Scenario matrix ──────────────────────────────────────────────────────────
# Rows sweep DER penetration and the storage layer; columns sweep the line
# rating method and the conductor build.
_DER_STEPS = {
    1: ["WF_2"],
    2: ["WF_2", "WF_1"],
    3: ["WF_2", "WF_1", "WF_3"],
    4: ["WF_2", "WF_1", "WF_3", "PV_1"],
}

_MATRIX_COLUMNS = {
    "static": ("single", 0),
    "dlr1": ("single", 1),
    "dlr2": ("single", 2),
    "twin": ("twin", 0),
}

PRESETS = {
    "baseline": dict(conductor="single", control_mode="cosphi", dlr_mode=0, der_enabled=[]),
}
for _tag, (_cond, _dlr) in _MATRIX_COLUMNS.items():
    for _n, _ders in _DER_STEPS.items():
        PRESETS[f"{_tag}_der{_n}"] = dict(conductor=_cond, dlr_mode=_dlr,
                                          control_mode="droop", der_enabled=list(_ders))
    PRESETS[f"{_tag}_der4_bess"] = dict(conductor=_cond, dlr_mode=_dlr, control_mode="droop",
                                        der_enabled=list(_DER_STEPS[4]), storage_enabled=True)


def normalise_der(der):
    """Accept a list of enabled names or a partial dict; return a full map."""
    if der is None:
        return dict(ALL_ON)
    if isinstance(der, (list, tuple, set)):
        unknown = set(der) - set(DER_NAMES)
        if unknown:
            raise ValueError(f"unknown DER {sorted(unknown)}; valid: {list(DER_NAMES)}")
        return {n: (n in der) for n in DER_NAMES}
    unknown = set(der) - set(DER_NAMES)
    if unknown:
        raise ValueError(f"unknown DER key {sorted(unknown)}; valid: {list(DER_NAMES)}")
    return {n: bool(der.get(n, True)) for n in DER_NAMES}


def build_config(preset: str | None = None, **overrides) -> Config:
    """Field defaults, then the named preset, then explicit overrides.

    Overrides are applied as given, `None` included, so a caller can ask for
    `days=None` and mean it. Callers that use `None` to mean "not specified"
    must strip those keys themselves; `parse_args` does.
    """
    fields = dict(PRESETS[preset]) if preset else {}
    fields.update(overrides)
    fields["der_enabled"] = normalise_der(fields.get("der_enabled"))
    cfg = Config(**fields)
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Fail fast on any inconsistent knob, before a run costs time."""
    assert cfg.conductor in C.CONDUCTOR_OPTIONS, f"bad conductor: {cfg.conductor}"
    assert cfg.control_mode in {"droop", "cosphi"}, f"bad control_mode: {cfg.control_mode}"
    assert cfg.pv_control_mode in {None, "droop", "cosphi"}, "bad pv_control_mode"
    assert cfg.cosphi_sign in {"absorb", "inject"}, f"bad cosphi_sign: {cfg.cosphi_sign}"
    assert cfg.dlr_mode in {0, 1, 2}, f"bad dlr_mode: {cfg.dlr_mode}"
    assert cfg.export_cap_basis in {"net", "gross"}, "bad export_cap_basis"
    assert cfg.droop_measurement in {"local", "pilot_tap_w", "pilot_sub_a"}, "bad droop_measurement"
    assert cfg.wf_trafo_rating in {"onan", "onaf"}, "bad wf_trafo_rating"
    assert cfg.wf_trafo_units in {1, 2}, "wf_trafo_units must be 1 or 2"
    assert set(cfg.der_enabled) == set(DER_NAMES), "der_enabled must cover every DER"
    assert cfg.storage_connection in {"tie", "direct"}, "bad storage_connection"
    assert cfg.storage_q_mode in {"droop", "cosphi", "fixed", "unity"}, "bad storage_q_mode"
    assert cfg.storage_charge_source in {"surplus_then_grid", "surplus_only", "grid_only"}, \
        "bad storage_charge_source"
    assert 0.0 <= cfg.storage_soc_init <= 1.0, "storage_soc_init must be in [0, 1]"
    assert cfg.storage_p_mw > 0 and cfg.storage_e_mwh > 0, "storage ratings must be positive"
    assert 0.0 <= cfg.storage_reserve_mw <= cfg.storage_p_mw, "reserve exceeds rating"
    assert 0.0 <= cfg.storage_contract_mw <= cfg.storage_p_mw, "contract exceeds rating"
    assert cfg.roughness_m > 0, "roughness_m must be positive"
    assert cfg.conductor_height_m - cfg.displacement_m > cfg.roughness_m, \
        "conductor height above displacement must exceed the roughness length"
    assert cfg.end_ts > cfg.start_ts, "end must be after start"


def parse_args(argv=None):
    """Parse the command line into (Config, make_plots)."""
    p = argparse.ArgumentParser(
        prog="corridor-sim",
        description="Quasi-static AC power-flow study of a DLR-enabled export corridor")
    p.add_argument("--preset", choices=sorted(PRESETS), help="named scenario")
    p.add_argument("--conductor", choices=sorted(C.CONDUCTOR_OPTIONS))
    p.add_argument("--dlr", dest="dlr_mode", type=int, choices=[0, 1, 2],
                   help="0 static, 1 ambient-adjusted, 2 full weather")
    p.add_argument("--control", dest="control_mode", choices=["droop", "cosphi"])
    p.add_argument("--der", help="comma-separated DER to connect, e.g. WF_1,WF_2,PV_1")
    p.add_argument("--storage", dest="storage_enabled", action="store_true", default=None)
    p.add_argument("--no-curtailment", dest="curtailment", action="store_false", default=None)
    p.add_argument("--export-cap", dest="export_cap_mw", type=float)
    p.add_argument("--start")
    p.add_argument("--days", type=int, help="window length in days (default 30)")
    p.add_argument("--end", help="explicit end date; overrides --days")
    p.add_argument("--data-dir", dest="data_dir", type=Path)
    p.add_argument("--out", dest="out_dir", type=Path)
    p.add_argument("--label", help="output filename stem")
    p.add_argument("--no-plots", dest="plots", action="store_false", default=True)
    a = p.parse_args(argv)

    # An unset flag means "do not override", so None values are dropped here
    # rather than in build_config.
    over = {k: v for k, v in vars(a).items()
            if k not in {"preset", "der", "plots"} and v is not None}
    if a.der is not None:
        over["der_enabled"] = [s.strip() for s in a.der.split(",") if s.strip()]
    if a.end is not None:
        over["days"] = None
    return build_config(a.preset, **over), a.plots
