"""Constraint hierarchy."""
from __future__ import annotations

import pytest

from corridor_sim.constants import DER_RATING_MW
from corridor_sim.constraints import (
    ACTIONABLE_LEVELS,
    cause_class,
    classify_residual,
    feasibility_margin,
    format_levels,
    measure_pcc_export,
    scan,
    violation_flags,
)
from corridor_sim.controls import solve


def test_export_is_positive_when_the_corridor_is_a_net_source(solved):
    net, idx = solved
    assert measure_pcc_export(net, idx.buses)["p_mw"] > 0


def test_clean_state_reports_no_violation(solved, cfg, static_limits):
    net, idx = solved
    generous = {z: 5000.0 for z in static_limits}
    level, element, magnitude, detail = scan(net, idx, generous, cfg)
    assert level == 0 and element == "none" and magnitude == 0.0
    assert format_levels(detail) == "none"
    assert all(v == 0 for v in violation_flags(detail).values())


def test_thermal_violation_is_detected_and_actionable(solved, cfg, static_limits):
    net, idx = solved
    tight = {z: 200.0 for z in static_limits}
    level, element, magnitude, detail = scan(net, idx, tight, cfg)
    assert level == 3 and magnitude > 0
    assert violation_flags(detail)["viol_L3"] == 1
    assert detail["groups"]["corridor"][1] > 100.0


def test_export_cap_violation_is_detected(solved, cfg, static_limits):
    from dataclasses import replace
    net, idx = solved
    strict = replace(cfg, export_cap_mw=10.0)
    level, element, magnitude, detail = scan(net, idx, {z: 5000.0 for z in static_limits}, strict)
    assert level == 4 and element == "PCC_net" and magnitude > 0


def _stress_without_reactive_support(net, idx, fraction=0.8):
    """Heavy export with the plants at zero reactive power.

    This is how an under-voltage actually arises on an inductive corridor: the
    reactive absorption of the line itself is not being met locally.
    """
    for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
        net.sgen.at[idx.sgens[unit], "p_mw"] = fraction * DER_RATING_MW[unit]
        net.sgen.at[idx.sgens[unit], "q_mvar"] = 0.0
    assert solve(net, "dc", deep=True)


def test_undervoltage_is_recorded_but_never_actioned(solved, cfg, static_limits):
    """Cutting active power deepens an under-voltage, so L2 must not be actionable."""
    assert 2 not in ACTIONABLE_LEVELS
    net, idx = solved
    _stress_without_reactive_support(net, idx)
    level, _, _, detail = scan(net, idx, {z: 5000.0 for z in static_limits}, cfg)
    assert detail["levels"][2] is not None, "under-voltage must be recorded"
    assert level != 2, "under-voltage must never be the actionable level"


def test_undervoltage_does_not_mask_a_thermal_violation(solved, cfg):
    """A single scan must report both levels, not stop at the first one."""
    net, idx = solved
    _stress_without_reactive_support(net, idx)
    level, _, _, detail = scan(net, idx, {"Z1": 800.0, "Z2": 800.0}, cfg)
    assert detail["levels"][2] is not None, "expected an under-voltage in this state"
    assert detail["levels"][3] is not None, "expected a coincident thermal overload"
    assert level == 3, "the actionable thermal level must survive an under-voltage"
    assert "L2:" in format_levels(detail) and "L3:" in format_levels(detail)


def test_feasibility_margin_decreases_with_curtailment(solved, cfg):
    net, idx = solved
    tight = {"Z1": 250.0, "Z2": 250.0}
    _, _, _, before = scan(net, idx, tight, cfg)
    margin_before = feasibility_margin(before)
    for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
        net.sgen.at[idx.sgens[unit], "p_mw"] *= 0.5
    assert solve(net, "results", deep=True)
    _, _, _, after = scan(net, idx, tight, cfg)
    assert feasibility_margin(after) < margin_before


@pytest.mark.parametrize("level,element,group,expected", [
    (1, "SUB_A", None, "overvoltage"),
    (4, "PCC_net", None, "export_cap"),
    (3, "CORR_W_C", "corridor", "corridor"),
    (3, "T_WF1_1", "plant", "plant"),
    (0, "none", None, "none"),
])
def test_every_curtailed_unit_lands_in_one_cause(level, element, group, expected):
    detail = {"groups": {"corridor": ("none", 0.0), "dso_trafo": ("none", 0.0),
                         "plant": ("none", 0.0)}}
    if group:
        detail["groups"][group] = (element, 130.0)
    assert cause_class(level, element, detail) == expected


def test_residual_classification():
    assert classify_residual(0, True, False) == "none"
    assert classify_residual(3, False, True) == "capacity_exhausted"
    assert classify_residual(1, False, False) == "needs_reactive_support"
    assert classify_residual(3, False, False) == "unresolved"
