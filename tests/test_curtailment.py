"""Curtailment search."""
from __future__ import annotations

import pytest

from corridor_sim.constraints import scan
from corridor_sim.controls import solve
from corridor_sim.curtailment import CURTAILABLE, RESOLUTION_MW, curtail, uncurtailed_record


def _entry_dispatch(net, idx):
    return {u: float(net.sgen.at[idx.sgens[u], "p_mw"]) for u in CURTAILABLE}


def test_no_curtailment_when_nothing_binds(solved, cfg):
    net, idx = solved
    before = _entry_dispatch(net, idx)
    record = curtail(net, cfg, idx, {"Z1": 5000.0, "Z2": 5000.0})
    assert record["curtailed_total_mw"] == 0.0
    assert record["curtail_exit"] == "not_needed"
    assert record["feasible"]
    assert _entry_dispatch(net, idx) == pytest.approx(before)


def test_curtailment_clears_a_thermal_violation(solved, cfg):
    net, idx = solved
    limits = {"Z1": 300.0, "Z2": 300.0}
    assert scan(net, idx, limits, cfg)[0] == 3
    record = curtail(net, cfg, idx, limits)
    assert record["curtailed_total_mw"] > 0
    assert record["feasible"], record["binding_after"]
    assert record["binding_after"] == "none"
    assert scan(net, idx, limits, cfg)[0] == 0


def test_applied_cut_is_close_to_the_minimum_that_clears(solved, cfg):
    """Anything meaningfully smaller than the applied cut must still violate."""
    net, idx = solved
    limits = {"Z1": 300.0, "Z2": 300.0}
    entry = _entry_dispatch(net, idx)
    record = curtail(net, cfg, idx, limits)
    applied = record["curtailed_total_mw"]
    assert record["feasible"]
    assert record["curtail_residual_mw"] <= RESOLUTION_MW + 1e-9

    total = sum(entry.values())
    smaller = applied - 2.0 * RESOLUTION_MW
    if smaller > 0:
        fraction = smaller / total
        for unit in CURTAILABLE:
            net.sgen.at[idx.sgens[unit], "p_mw"] = entry[unit] * (1.0 - fraction)
        assert solve(net, "results", deep=True)
        assert scan(net, idx, limits, cfg)[0] != 0, "a smaller cut should not have cleared"


def test_cut_is_shared_pro_rata(solved, cfg):
    net, idx = solved
    entry = _entry_dispatch(net, idx)
    record = curtail(net, cfg, idx, {"Z1": 300.0, "Z2": 300.0})
    total = sum(entry.values())
    fraction = record["curtailed_total_mw"] / total
    for unit in CURTAILABLE:
        assert record[f"curtailed_{unit}_mw"] == pytest.approx(entry[unit] * fraction, rel=1e-6)


def test_more_severe_limit_needs_more_curtailment(solved, cfg):
    net, idx = solved
    entry = _entry_dispatch(net, idx)
    mild = curtail(net, cfg, idx, {"Z1": 400.0, "Z2": 400.0})["curtailed_total_mw"]
    for unit, value in entry.items():
        net.sgen.at[idx.sgens[unit], "p_mw"] = value
        net.sgen.at[idx.sgens[unit], "q_mvar"] = 0.0
    assert solve(net, "dc", deep=True)
    severe = curtail(net, cfg, idx, {"Z1": 250.0, "Z2": 250.0})["curtailed_total_mw"]
    assert severe > mild


def test_export_cap_curtailment(solved, cfg):
    from dataclasses import replace
    net, idx = solved
    strict = replace(cfg, export_cap_mw=80.0)
    record = curtail(net, cfg=strict, idx=idx, limits_a={"Z1": 5000.0, "Z2": 5000.0})
    assert record["curtail_cause"] == "export_cap"
    assert record["curtailed_total_mw"] > 0
    assert record["feasible"]


def test_disabled_curtailment_still_records_what_binds(solved, cfg):
    net, idx = solved
    record = uncurtailed_record(net, cfg, idx, {"Z1": 300.0, "Z2": 300.0})
    assert record["curtail_exit"] == "disabled"
    assert record["binding_before"] != "none"
    assert record["binding_before"] == record["binding_after"]
    assert record["curtailed_total_mw"] == 0.0
    assert not record["feasible"]


def test_owner_groups_are_reported_after_remediation(solved, cfg):
    net, idx = solved
    record = curtail(net, cfg, idx, {"Z1": 300.0, "Z2": 300.0})
    for key in ("worst_corridor_pct", "worst_dso_trafo_pct", "worst_plant_pct"):
        assert record[key] >= 0.0
    assert record["worst_corridor_elem"] != "none"
