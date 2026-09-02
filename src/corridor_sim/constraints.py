"""Four-level constraint hierarchy.

    L1  over-voltage    any 110 kV bus above the upper band
    L2  under-voltage   any 110 kV bus below the lower band
    L3  thermal         corridor lines against the operative rating,
                        distribution transformers, plant assets
    L4  export cap      active power at the point of common coupling

Every level is evaluated on every scan. Remediation acts on L1, L3 and L4 in
that order; L2 is recorded but never curtailed for, because reducing active
power deepens an under-voltage on an inductive corridor. Scanning all four
regardless means a non-actionable under-voltage can never mask a thermal or
export violation happening at the same instant.
"""
from __future__ import annotations

import math

from . import constants as C
from .network import (
    CORRIDOR_ZONES,
    DSO_TRAFO_NAMES,
    EXPORT_PATH_LINES,
    HV_BUSES,
    PLANT_LINES,
    PLANT_TRAFOS,
)

ACTIONABLE_LEVELS = (1, 3, 4)
L3_GROUPS = ("corridor", "dso_trafo", "plant")

# Per-level resolution, so a per-unit voltage margin, a percentage-point
# thermal margin and a megawatt export margin can be compared on one scale.
MARGIN_SCALE = {1: 1e-3, 3: 5e-2, 4: 5e-2}


def measure_pcc_export(net, buses) -> dict:
    """Power crossing the PCC toward the grid. Positive means net export.

    Measured at the ownership boundary, not at the slack: the Thevenin angle
    inflates slack reactive power well beyond what actually crosses the PCC.
    """
    imp = net.impedance
    idx = None
    for i, row in imp.iterrows():
        if row["from_bus"] == buses["GRID"] and row["to_bus"] == buses["PCC"]:
            idx = i
            break
    if idx is None or not len(net.res_impedance):
        nan = float("nan")
        return {"p_mw": nan, "q_mvar": nan, "s_mva": nan}
    p = float(net.res_impedance.at[idx, "p_to_mw"])
    q = float(net.res_impedance.at[idx, "q_to_mvar"])
    return {"p_mw": p, "q_mvar": q, "s_mva": math.hypot(p, q)}


def corridor_losses_mw(net, lines) -> float:
    """Active loss on the export path, used by the gross export basis."""
    total = 0.0
    for name in EXPORT_PATH_LINES:
        idx = lines.get(name)
        if idx is None or not bool(net.line.at[idx, "in_service"]):
            continue
        total += float(net.res_line.at[idx, "pl_mw"])
    return total


def _worst(current, candidate):
    return candidate if (current is None or candidate[1] > current[1]) else current


def scan(net, idx, limits_a: dict, cfg):
    """Evaluate all four levels.

    Returns (actionable_level, element, magnitude, detail) where detail holds
    the per-level result, the worst loading in each ownership group, and the
    signed distance to every limit whether or not it is breached.
    """
    levels = {1: None, 2: None, 3: None, 4: None}
    margins = dict.fromkeys((1, 2, 3, 4), -float("inf"))

    # ── L1 / L2 voltage band ─────────────────────────────────────────────────
    for name in HV_BUSES:
        v = float(net.res_bus.at[idx.buses[name], "vm_pu"])
        over, under = v - C.V_MAX_PU, C.V_MIN_PU - v
        margins[1] = max(margins[1], over)
        margins[2] = max(margins[2], under)
        if over > 0:
            levels[1] = _worst(levels[1], (name, over))
        if under > 0:
            levels[2] = _worst(levels[2], (name, under))

    # ── L3 thermal, split by who owns the asset ──────────────────────────────
    groups = {g: ("none", 0.0) for g in L3_GROUPS}

    for zone, names in CORRIDOR_ZONES.items():
        limit_a = limits_a[zone]
        for name in names:
            line_idx = idx.lines.get(name)
            if line_idx is None or not bool(net.line.at[line_idx, "in_service"]):
                continue
            amps = float(net.res_line.at[line_idx, "i_from_ka"]) * 1000.0
            pct = 100.0 * amps / limit_a if limit_a > 0 else float("inf")
            if pct > groups["corridor"][1]:
                groups["corridor"] = (name, pct)

    for name in DSO_TRAFO_NAMES:
        t_idx = idx.trafos.get(name)
        if t_idx is not None:
            pct = float(net.res_trafo.at[t_idx, "loading_percent"])
            if pct > groups["dso_trafo"][1]:
                groups["dso_trafo"] = (name, pct)

    for name in PLANT_LINES:
        line_idx = idx.lines.get(name)
        if line_idx is not None and bool(net.line.at[line_idx, "in_service"]):
            pct = float(net.res_line.at[line_idx, "loading_percent"])
            if pct > groups["plant"][1]:
                groups["plant"] = (name, pct)
    for name in PLANT_TRAFOS:
        t_idx = idx.trafos.get(name)
        if t_idx is not None:
            pct = float(net.res_trafo.at[t_idx, "loading_percent"])
            if pct > groups["plant"][1]:
                groups["plant"] = (name, pct)

    for group in L3_GROUPS:
        element, pct = groups[group]
        excess = pct - 100.0
        margins[3] = max(margins[3], excess)
        if excess > 0:
            levels[3] = _worst(levels[3], (element, excess))

    # ── L4 export cap ────────────────────────────────────────────────────────
    if cfg.export_cap_basis == "gross":
        gross = float(net.res_sgen["p_mw"].sum()) - corridor_losses_mw(net, idx.lines)
        margins[4] = gross - cfg.export_cap_mw
        if margins[4] > 0:
            levels[4] = ("PCC_gross", margins[4])
    else:
        p_net = measure_pcc_export(net, idx.buses)["p_mw"]
        if math.isfinite(p_net):
            margins[4] = p_net - cfg.export_cap_mw
            if margins[4] > 0:
                levels[4] = ("PCC_net", margins[4])

    detail = {"levels": levels, "groups": groups, "margins": margins}
    for level in ACTIONABLE_LEVELS:
        if levels[level] is not None:
            return level, levels[level][0], levels[level][1], detail
    return 0, "none", 0.0, detail


def violation_flags(detail: dict) -> dict:
    """Independent per-level flags, so coincident violations stay visible."""
    return {f"viol_L{i}": int(detail["levels"][i] is not None) for i in (1, 2, 3, 4)}


def format_levels(detail: dict) -> str:
    """Compact record of every violated level, e.g. 'L2:SUB_A=0.012|L3:T_WF2_1=32.2'."""
    parts = [f"L{i}:{hit[0]}={hit[1]:.4g}"
             for i in (1, 2, 3, 4) if (hit := detail["levels"][i]) is not None]
    return "|".join(parts) if parts else "none"


def feasibility_margin(detail: dict) -> float:
    """Signed normalised distance to the actionable feasibility boundary.

    Positive while some curtailable level is violated, zero or negative once
    the dispatch is admissible, and monotone decreasing in curtailment, which
    is what lets the curtailment search interpolate.
    """
    m = detail["margins"]
    return max(m[level] / MARGIN_SCALE[level] for level in ACTIONABLE_LEVELS)


def cause_class(level: int, element: str, detail: dict) -> str:
    """Attribute a curtailment to exactly one cause."""
    if level == 1:
        return "overvoltage"
    if level == 4:
        return "export_cap"
    if level == 3:
        for group in L3_GROUPS:
            if detail["groups"][group][0] == element:
                return group
        return "thermal_other"
    return "none"


def classify_residual(level_after: int, feasible: bool, fleet_off: bool) -> str:
    """What a converged row with a constraint still binding actually means."""
    if level_after == 0:
        return "none"
    if not feasible and fleet_off:
        return "capacity_exhausted"        # nothing left to curtail
    if level_after == 1:
        return "needs_reactive_support"    # cutting active power cannot clear it
    return "unresolved"
