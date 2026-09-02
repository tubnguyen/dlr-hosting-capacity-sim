"""Physical and network constants for the generic study corridor.

All values are representative of a European 110 kV sub-transmission corridor
and are taken from public standards, typical datasheets or round design
figures. Nothing here is measured or operator-specific.
"""
from __future__ import annotations

import math

# ── System ───────────────────────────────────────────────────────────────────
F_HZ = 50.0
V_HV_KV = 110.0                 # corridor nominal voltage
V_MV_KV = 20.0                  # distribution substation nominal voltage
V_COLLECTOR_KV = 33.0           # plant collector nominal voltage
S_BASE_MVA = 100.0
DT_H = 0.25                     # 15-minute simulation step

# Operational voltage band, ±5 % of nominal (EN 50160 style).
V_MAX_PU = 1.05
V_MIN_PU = 0.95

# ── Generation fleet ─────────────────────────────────────────────────────────
P_TURBINE_MW = 6.0
HUB_HEIGHT_M = 170.0
N_TURBINES = {"WF_1": 15, "WF_2": 18, "WF_3": 12}

DER_RATING_MW = {
    "WF_1": N_TURBINES["WF_1"] * P_TURBINE_MW,   #  90 MW
    "WF_2": N_TURBINES["WF_2"] * P_TURBINE_MW,   # 108 MW
    "WF_3": N_TURBINES["WF_3"] * P_TURBINE_MW,   #  72 MW
    "PV_1": 60.0,
}
FLEET_MW = sum(DER_RATING_MW.values())           # 330 MW

# Grid-code minimum reactive capability (cos phi 0.95 at rated active power).
COS_PHI = 0.95
TAN_PHI = math.tan(math.acos(COS_PHI))           # 0.3287
Q_LIMIT_MVAR = {k: v * TAN_PHI for k, v in DER_RATING_MW.items()}

# ── Corridor conductor ───────────────────────────────────────────────────────
# ACSR ~305 mm2 aluminium / 39 mm2 steel, 24 mm outside diameter.
COND_DIAMETER_M = 0.024
COND_ABSORPTIVITY = 0.8
COND_EMISSIVITY = 0.8
COND_R20_OHM_PER_KM = 0.080     # DC resistance at 20 C
ALPHA_AL = 0.00403              # aluminium temperature coefficient [1/C]
T_COND_MAX_C = 80.0             # design maximum conductor temperature


def conductor_resistance(t_c: float) -> float:
    """Sub-conductor DC resistance [ohm/m] at conductor temperature `t_c` [C]."""
    return COND_R20_OHM_PER_KM * 1e-3 * (1.0 + ALPHA_AL * (t_c - 20.0))


_R50 = COND_R20_OHM_PER_KM * (1.0 + ALPHA_AL * 30.0)   # 0.0897 ohm/km at 50 C

# Two build options for the same corridor: as-built single conductor, and a
# twin-bundle reconductoring. Bundling roughly doubles ampacity, halves
# resistance and lowers series reactance.
CONDUCTOR_OPTIONS = {
    "single": dict(r_ohm_per_km=round(_R50, 4), x_ohm_per_km=0.400,
                   c_nf_per_km=9.2, max_i_ka=0.800, bundle_n=1),
    "twin": dict(r_ohm_per_km=round(_R50 / 2, 4), x_ohm_per_km=0.290,
                 c_nf_per_km=12.6, max_i_ka=1.600, bundle_n=2),
}

# Plant collector lines: a heavier 110 kV overhead lateral and a 33 kV cable.
LATERAL_OHL = dict(r_ohm_per_km=0.047, x_ohm_per_km=0.349, c_nf_per_km=12.6, max_i_ka=1.540)
COLLECTOR_CABLE = dict(r_ohm_per_km=0.063, x_ohm_per_km=0.110, c_nf_per_km=340.0,
                       max_i_ka=0.720, type="cs")
COLLECTOR_CABLE_PARALLEL = 2

# ── Line lengths [km] ────────────────────────────────────────────────────────
# The constrained corridor runs SUB_A -> TAP_PV -> TAP_W -> SUB_C -> TAP_B -> PCC.
LEN_CORR_A_PV = 6.0
LEN_CORR_PV_W = 8.0
LEN_CORR_W_C = 2.5
LEN_CORR_C_B = 16.5
LEN_CORR_B_PCC = 0.7
CORRIDOR_LENGTH_KM = (LEN_CORR_A_PV + LEN_CORR_PV_W + LEN_CORR_W_C
                      + LEN_CORR_C_B + LEN_CORR_B_PCC)          # 33.7 km
LEN_LINK_PCC_E = 15.0
LEN_SPUR_B_E = 11.0
LEN_LAT_WF1_WF2 = 14.0
LEN_LAT_WF2_TAP = 16.0
LEN_LAT_WF3_A = 21.0
LEN_CAB_PV = 7.5
LEN_LAT_BESS = 0.7

# ── External grid Thevenin equivalent at the PCC ──────────────────────────────
SRC_R_OHM = 2.0
SRC_X_OHM = 10.0

# ── Transformers ─────────────────────────────────────────────────────────────
# Distribution 110/20 kV, on-load tap changer on the HV winding.
DSO_TRAFOS = {                  # name: (sn_mva, vk_%, vkr_%)
    "T_SUB_A1": (25.0, 10.4, 0.36),
    "T_SUB_A2": (16.0, 10.2, 0.48),
    "T_SUB_B": (25.0, 9.7, 0.30),
}
OLTC_TAP = dict(tap_side="hv", tap_neutral=0, tap_min=-9, tap_max=9,
                tap_step_percent=1.67, tap_pos=0)

# Wind-farm step-up banks: two parallel units per farm, each rated 50 MVA with
# natural cooling (ONAN) and 63 MVA with forced cooling (ONAF). Nameplate
# impedance is quoted on the ONAN base and re-referred to whichever rating is
# enforced, so the bank impedance is unchanged by the rating choice.
WF_TRAFO_UNITS = 2
WF_TRAFO_SN_ONAN_MVA = 50.0
WF_TRAFO_SN_ONAF_MVA = 63.0
WF_TRAFO_VK_PCT = 12.5
WF_TRAFO_VKR_PCT = 0.35

PV_TRAFO = dict(sn_mva=75.0, vk_percent=12.0, vkr_percent=0.40)
BESS_TRAFO = dict(sn_mva=40.0, vk_percent=12.0, vkr_percent=0.40)

# ── Voltage control ──────────────────────────────────────────────────────────
OLTC_TARGET_KV = 20.5           # MV setpoint, allows for feeder voltage drop
OLTC_DEADBAND_KV = 0.50         # coarse/backup responder
REACTOR_DEADBAND_KV = 0.18      # fine/primary responder, acts first
OLTC_MOVE_BUDGET = 3            # tap operations one 15-min step may issue
REACTOR_MOVE_BUDGET = 4

# Stepped MV shunt reactor, 11 positions.
REACTOR_STEPS_MVAR = [0.50, 0.75, 1.00, 1.25, 1.50, 1.75,
                      2.00, 2.25, 2.50, 2.75, 3.00]
REACTOR_STEP_INIT = 5

# Voltage-reactive power droop at the plant connection point.
V_REF_PU = 1.00
DROOP_DEADBAND_PU = 0.010
DROOP_SLOPE_PCT = 4.0           # full reactive range over 4 % voltage error
DROOP_V_TOL_PU = 5e-4
DROOP_Q_DAMP = 0.60             # initial under-relaxation factor
DROOP_Q_DAMP_MIN = 0.05
# The step tolerance only decides when a unit has stopped moving; accuracy is
# enforced separately by the residual tolerance, which is what the convergence
# test actually asserts. Setting the step tolerance far below the residual
# tolerance buys no accuracy and costs iterations.
DROOP_Q_STEP_TOL_MVAR = 0.20    # step below this counts as settled
DROOP_Q_ERR_TOL_MVAR = 0.50     # residual above this blocks convergence
DROOP_MAX_ITER = 20
COSPHI_MAX_ITER = 3
DROOP_P_MIN_FRAC = 0.05         # below this loading, droop reactive power is gated off

# ── Interface limits ─────────────────────────────────────────────────────────
EXPORT_CAP_MW = 250.0           # contracted active export limit at the PCC
Q_WINDOW_FRAC = 0.10            # reactive exchange window as a fraction of fleet MW
Q_WINDOW_CAP_MVAR = 50.0
Q_GUARD_MVAR = 20.0             # reactive import level that releases the reactors
Q_MONITOR_MVAR = 30.0

# ── Storage ──────────────────────────────────────────────────────────────────
BESS_NAME = "BESS"
BESS_P_MW = 30.0
BESS_E_MWH = 60.0
BESS_ETA_RT = 0.88
BESS_ETA_CH = math.sqrt(BESS_ETA_RT)
BESS_ETA_DIS = math.sqrt(BESS_ETA_RT)
BESS_SOC_MIN = 0.10
BESS_SOC_MAX = 0.95
BESS_SOC_RESERVE_FLOOR = 0.20   # keeps the contracted up-reserve deliverable
BESS_ACTIVATION_TRIGGER_MW = 1.0

# ── Dynamic line rating ──────────────────────────────────────────────────────
# The corridor is split into two rating zones with different mean bearings, so
# the wind angle of attack differs between them.
DLR_ZONE_AZIMUTH_DEG = {"Z1": 110.0, "Z2": 95.0}
DLR_SITE_ELEVATION_M = 100.0
DLR_CONDUCTOR_HEIGHT_M = 15.0   # conductor height above ground at mid-span
DLR_ROUGHNESS_M = 0.30          # surface roughness length for wind extrapolation
DLR_DISPLACEMENT_M = 0.0
DLR_LOW_WIND_MS = 0.6           # conservative fixed wind speed used by mode 1
DLR_REF_HEIGHT_M = 100.0        # height of the reference wind field

# Reference conditions the static (mode 0) ampacity is declared at: a hot day,
# light perpendicular wind, full sun. Evaluating the IEEE 738 model at these
# conditions reproduces the nameplate rating, so mode 0 and modes 1-2 are the
# same physics under different weather assumptions rather than two models.
STATIC_REF_CONDITIONS = dict(t_air_c=32.0, wind_ms=0.6, phi_deg=90.0, ghi_wm2=1000.0)
