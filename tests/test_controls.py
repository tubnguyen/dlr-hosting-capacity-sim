"""Voltage and reactive power control."""
from __future__ import annotations

import pytest

from corridor_sim import constants as C
from corridor_sim.controls import MoveBudget, droop_reference, measurement_bus, regulate, solve

Q_LIMIT = 30.0
P_RATED = 90.0


def test_droop_absorbs_on_high_voltage_and_injects_on_low():
    high = droop_reference(1.03, P_RATED, Q_LIMIT, P_RATED)
    low = droop_reference(0.97, P_RATED, Q_LIMIT, P_RATED)
    assert high < 0, "over-voltage must absorb reactive power"
    assert low > 0, "under-voltage must inject reactive power"
    assert high == pytest.approx(-low)


def test_droop_deadband_is_quiet():
    for v in (0.995, 1.000, 1.005, 1.010):
        assert droop_reference(v, P_RATED, Q_LIMIT, P_RATED) == pytest.approx(0.0, abs=1e-9)


def test_droop_saturates_at_the_capability_limit():
    assert droop_reference(1.40, P_RATED, Q_LIMIT, P_RATED) == pytest.approx(-Q_LIMIT)
    assert droop_reference(0.60, P_RATED, Q_LIMIT, P_RATED) == pytest.approx(Q_LIMIT)


def test_droop_slope_matches_the_specification():
    """One slope-width of error beyond the deadband uses the full range."""
    v = C.V_REF_PU + C.DROOP_DEADBAND_PU + C.DROOP_SLOPE_PCT / 100.0
    assert droop_reference(v, P_RATED, Q_LIMIT, P_RATED) == pytest.approx(-Q_LIMIT)


def test_droop_is_gated_at_low_output():
    assert droop_reference(1.03, 0.01 * P_RATED, Q_LIMIT, P_RATED, 0.05) == 0.0
    assert droop_reference(1.03, 0.50 * P_RATED, Q_LIMIT, P_RATED, 0.05) != 0.0


def test_move_budget_limits_operations():
    budget = MoveBudget(2)
    assert budget.allowed("a")
    budget.record("a", 1)
    budget.record("a", 1)
    assert not budget.allowed("a"), "budget must cap operations per step"


def test_move_budget_freezes_on_reversal():
    """An actuator that reverses within a step has bracketed its setpoint."""
    budget = MoveBudget(10)
    budget.record("a", 1)
    budget.record("a", -1)
    assert not budget.allowed("a")
    assert budget.n_frozen() == 1


def test_measurement_bus_policies(cfg):
    from dataclasses import replace
    assert measurement_bus(cfg, "WF_3") == "SUB_A"
    assert measurement_bus(cfg, "PV_1") == "TAP_PV"
    pilot = replace(cfg, droop_measurement="pilot_tap_w")
    assert all(measurement_bus(pilot, u) == "TAP_W"
               for u in ("WF_1", "WF_2", "WF_3", "PV_1"))


def test_regulation_delivers_the_reactive_power_it_reports(solved, cfg, reactor_state):
    """The recorded reactive power must be a solved value, not a bare command."""
    net, idx = solved
    dispatch = {u: float(net.sgen.at[idx.sgens[u], "p_mw"])
                for u in ("WF_1", "WF_2", "WF_3", "PV_1")}
    result = regulate(net, cfg, idx, dispatch, reactor_state, warm_start=False)
    assert result["ok"]
    assert result["q_tracking_error_mvar"] <= C.DROOP_Q_ERR_TOL_MVAR + 1e-6
    for unit, q_ref in result["q_ref"].items():
        if unit == C.BESS_NAME:
            continue
        delivered = float(net.res_sgen.at[idx.sgens[unit], "q_mvar"])
        assert delivered == pytest.approx(q_ref, abs=C.DROOP_Q_ERR_TOL_MVAR + 1e-6)


def test_regulation_respects_the_move_budgets(solved, cfg, reactor_state):
    net, idx = solved
    dispatch = {u: float(net.sgen.at[idx.sgens[u], "p_mw"])
                for u in ("WF_1", "WF_2", "WF_3", "PV_1")}
    result = regulate(net, cfg, idx, dispatch, reactor_state, warm_start=False)
    assert result["oltc_moves_a"] <= C.OLTC_MOVE_BUDGET * len(idx.oltc_a)
    assert result["reactor_moves"] <= C.REACTOR_MOVE_BUDGET * len(idx.reactors)


def test_regulation_always_leaves_a_solved_network(solved, cfg, reactor_state):
    """Even a stressed step must end solved, so curtailment can act on it."""
    net, idx = solved
    for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
        net.sgen.at[idx.sgens[unit], "p_mw"] = C.DER_RATING_MW[unit]
        net.sgen.at[idx.sgens[unit], "q_mvar"] = 0.0
    dispatch = dict(C.DER_RATING_MW)
    result = regulate(net, cfg, idx, dispatch, reactor_state, warm_start=False)
    assert result["ok"]
    assert net.res_bus["vm_pu"].notna().all()


def test_solve_reports_failure_without_the_deep_path(solved):
    net, _ = solved
    assert solve(net, "results")
