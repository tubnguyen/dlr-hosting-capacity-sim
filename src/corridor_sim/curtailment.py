"""Minimal pro-rata curtailment.

Curtailment is parametrised by one scalar: the total megawatts removed,
allocated pro rata over the entry dispatch. Every actionable constraint is
relieved monotonically by injecting less, so the search brackets the
feasibility boundary and then interpolates on the signed margin, and applies
the smallest cut that clears the violation.

Marching down in fixed steps and keeping whatever was taken would answer "the
first grid point past the boundary" rather than the boundary itself, which
overstates the curtailment a constraint actually requires.

Two properties the search depends on:

* each trial evaluates the same state that will be applied, droop re-settle
  included, so the search and the reported result are the same function;
* each trial restarts from the entry reactive power, so a trial is a pure
  function of the cut and the bracket stays meaningful.
"""
from __future__ import annotations

from . import constants as C
from .constraints import cause_class, classify_residual, feasibility_margin, format_levels, scan
from .controls import droop_reference, measured_voltage, solve

CURTAILABLE = ("PV_1", "WF_2", "WF_1", "WF_3")
MAX_TRIALS = 40
RESOLUTION_MW = 0.5             # bracket width the search stops at
SETTLE_ITERS = 8


def default_record(cfg) -> dict:
    return {
        "curtailment_enabled": cfg.curtailment,
        "curtailed_WF_1_mw": 0.0, "curtailed_WF_2_mw": 0.0,
        "curtailed_WF_3_mw": 0.0, "curtailed_PV_1_mw": 0.0,
        "curtailed_total_mw": 0.0,
        "binding_before": "none", "binding_after": "none",
        "binding_all_before": "none", "binding_all_after": "none",
        "curtail_cause": "none", "curtail_exit": "not_needed",
        "curtail_trials": 0, "curtail_residual_mw": 0.0,
        "curtail_failures": 0, "feasible": True,
        "margin_after": 0.0, "residual_class": "none", "ok": True,
        "worst_corridor_pct": 0.0, "worst_corridor_elem": "none",
        "worst_dso_trafo_pct": 0.0, "worst_dso_trafo_elem": "none",
        "worst_plant_pct": 0.0, "worst_plant_elem": "none",
    }


def _record_scan(record: dict, detail: dict, when: str) -> None:
    record[f"binding_all_{when}"] = format_levels(detail)
    if when == "after":
        for group, prefix in (("corridor", "worst_corridor"),
                              ("dso_trafo", "worst_dso_trafo"),
                              ("plant", "worst_plant")):
            element, pct = detail["groups"][group]
            record[f"{prefix}_elem"], record[f"{prefix}_pct"] = element, pct


def settle_droop(net, cfg, idx, units) -> bool:
    """Re-settle plant reactive power after active power has been cut.

    Curtailment moves active power by tens of megawatts, so the droop
    reference genuinely moves with it and a single damped step does not reach
    it. Most steps sit inside the droop deadband, so this usually exits after
    one pass without a solve.
    """
    damping = dict.fromkeys(units, C.DROOP_Q_DAMP)
    last = dict.fromkeys(units, 0)
    for _ in range(SETTLE_ITERS):
        moved = False
        for unit in units:
            s_idx = idx.sgens[unit]
            p_now = float(net.sgen.at[s_idx, "p_mw"])
            q_now = float(net.sgen.at[s_idx, "q_mvar"])
            mode = cfg.pv_mode if unit == "PV_1" else cfg.control_mode
            if mode != "droop":
                q_new = cfg.cosphi_sign_factor * p_now * C.TAN_PHI
                if abs(q_new - q_now) > C.DROOP_Q_STEP_TOL_MVAR:
                    moved = True
                net.sgen.at[s_idx, "q_mvar"] = q_new
                continue
            v = measured_voltage(net, idx, cfg, unit)
            q_ref = droop_reference(v, p_now, C.Q_LIMIT_MVAR[unit],
                                    C.DER_RATING_MW[unit], cfg.droop_p_min_frac)
            delta = q_ref - q_now
            direction = (delta > 0) - (delta < 0)
            if direction and last[unit] and direction != last[unit]:
                damping[unit] = max(C.DROOP_Q_DAMP_MIN, damping[unit] * 0.5)
            if direction:
                last[unit] = direction
            q_new = q_now + damping[unit] * delta
            if abs(q_new - q_now) > C.DROOP_Q_STEP_TOL_MVAR:
                moved = True
            net.sgen.at[s_idx, "q_mvar"] = q_new
        if not moved:
            return True
        if not solve(net, "results"):
            return False
    return True


def uncurtailed_record(net, cfg, idx, limits_a) -> dict:
    """What binds when curtailment is switched off, recorded rather than blank."""
    record = default_record(cfg)
    level, element, magnitude, detail = scan(net, idx, limits_a, cfg)
    tag = f"L{level}:{element}" if level else "none"
    record.update({
        "binding_before": tag, "binding_after": tag,
        "margin_after": magnitude,
        "curtail_cause": cause_class(level, element, detail),
        "curtail_exit": "disabled",
        "feasible": level == 0,
    })
    _record_scan(record, detail, "before")
    _record_scan(record, detail, "after")
    return record


def curtail(net, cfg, idx, limits_a) -> dict:
    """Apply the smallest pro-rata cut that clears every actionable violation."""
    record = default_record(cfg)
    level, element, magnitude, detail = scan(net, idx, limits_a, cfg)
    record["binding_before"] = f"L{level}:{element}" if level else "none"
    record["curtail_cause"] = cause_class(level, element, detail)
    _record_scan(record, detail, "before")

    base_p = {u: float(net.sgen.at[idx.sgens[u], "p_mw"]) for u in CURTAILABLE}
    base_q = {u: float(net.sgen.at[idx.sgens[u], "q_mvar"]) for u in CURTAILABLE}
    total_p = sum(base_p.values())

    if level == 0:
        _record_scan(record, detail, "after")
        record["binding_after"] = record["binding_before"]
        return record

    if total_p <= 0.01:
        # A constraint binds with nothing left to curtail.
        record.update({"curtail_exit": "nothing_to_curtail", "feasible": False,
                       "binding_after": record["binding_before"],
                       "margin_after": magnitude,
                       "residual_class": classify_residual(level, False, True)})
        _record_scan(record, detail, "after")
        return record

    trials = 0

    def attempt(cut_mw: float):
        """Apply a pro-rata cut, re-settle the droop, and re-scan."""
        nonlocal trials
        fraction = min(1.0, cut_mw / total_p) if total_p > 0 else 0.0
        for unit in CURTAILABLE:
            net.sgen.at[idx.sgens[unit], "p_mw"] = base_p[unit] * (1.0 - fraction)
            net.sgen.at[idx.sgens[unit], "q_mvar"] = base_q[unit]
        trials += 1
        if not solve(net, "results"):
            record["curtail_failures"] += 1
            return None
        if fraction > 0.0 and not settle_droop(net, cfg, idx, CURTAILABLE):
            record["curtail_failures"] += 1
            return None
        return scan(net, idx, limits_a, cfg)

    # First guess from the linear structure of the violation: corridor current
    # and export both scale with injection, so the required cut is predictable
    # to within a factor rather than something to find by doubling from zero.
    margins = detail["margins"]
    guesses = []
    if margins[3] > 0:
        guesses.append(total_p * margins[3] / (100.0 + margins[3]))
    if margins[4] > 0:
        guesses.append(margins[4])
    if margins[1] > 0:
        guesses.append(total_p * 0.10)
    cut = min(total_p, max(RESOLUTION_MW, max(guesses) if guesses else 5.0))

    lo, g_lo = 0.0, feasibility_margin(detail)     # the entry state, violating
    hi, g_hi = None, None
    exit_reason = "trial_budget"

    while trials < MAX_TRIALS:
        result = attempt(cut)
        if result is None:
            # Treat a diverged trial as still violating: that pushes the search
            # toward more curtailment, which is the safe direction.
            lo, g_lo = cut, max(g_lo, 1.0)
        else:
            trial_level, _, _, trial_detail = result
            g = feasibility_margin(trial_detail)
            if trial_level == 0:
                hi, g_hi = cut, g
            else:
                lo, g_lo = cut, g

        if hi is None:
            if cut >= total_p:
                exit_reason = "nothing_to_curtail"
                break
            cut = min(total_p, cut * 2.0)
            continue

        if hi - lo <= RESOLUTION_MW:
            exit_reason = "cleared"
            break

        # Regula falsi with an Illinois-style guard: an interpolant landing too
        # close to either end is replaced by the midpoint, so the bracket always
        # shrinks and the search cannot stall.
        denominator = g_lo - g_hi
        span = hi - lo
        nxt = lo + span * (g_lo / denominator) if denominator > 1e-12 else lo + 0.5 * span
        if not (lo + 0.05 * span < nxt < hi - 0.05 * span):
            nxt = lo + 0.5 * span
        cut = nxt

    applied = hi if hi is not None else total_p
    result = attempt(applied)
    if result is None:
        record.update({"ok": False, "curtail_exit": "solve_failure", "feasible": False,
                       "curtail_trials": trials})
        return record

    level, element, magnitude, detail = result
    fraction = min(1.0, applied / total_p) if total_p > 0 else 0.0
    for unit in CURTAILABLE:
        record[f"curtailed_{unit}_mw"] = base_p[unit] * fraction

    record.update({
        "curtail_trials": trials,
        "curtail_exit": exit_reason,
        "curtail_residual_mw": (hi - lo) if hi is not None else 0.0,
        "feasible": level == 0,
        "binding_after": f"L{level}:{element}" if level else "none",
        "margin_after": magnitude if level else 0.0,
        "curtailed_total_mw": sum(base_p[u] * fraction for u in CURTAILABLE),
    })
    _record_scan(record, detail, "after")
    fleet_off = all(float(net.sgen.at[idx.sgens[u], "p_mw"]) < 0.01 for u in CURTAILABLE)
    record["residual_class"] = classify_residual(level, record["feasible"], fleet_off)
    return record
