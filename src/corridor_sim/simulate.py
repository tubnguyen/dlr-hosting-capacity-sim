"""Quasi-static 15-minute simulation loop.

Sequence per timestep:

    loads -> plant dispatch -> storage intent -> coordinated voltage control
    -> operative line rating -> storage reconciliation -> curtailment
    -> state of charge and reserve -> record

The rating is computed from weather alone, so it is valid whether or not the
power flow converged, and every row records which rating path produced it.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd

from . import constants as C
from . import curtailment, dlr, storage
from .constraints import corridor_losses_mw, measure_pcc_export, scan, violation_flags
from .controls import NetIndex, regulate, solve
from .network import CORRIDOR_ZONES, DSO_TRAFO_NAMES, EXPORT_PATH_LINES, HV_BUSES, PLANT_TRAFOS

DER_UNITS = ("WF_1", "WF_2", "WF_3", "PV_1")
REPORT_BUSES = HV_BUSES + ["SUB_A_MV", "SUB_B_MV"]


def _line_value(net, idx, name, field):
    line_idx = idx.lines.get(name)
    if line_idx is None or not bool(net.line.at[line_idx, "in_service"]):
        return float("nan")
    return float(net.res_line.at[line_idx, field])


def _trafo_loading(net, idx, name):
    t_idx = idx.trafos.get(name)
    return float(net.res_trafo.at[t_idx, "loading_percent"]) if t_idx is not None else float("nan")


def _zone_current(net, idx, zone):
    """Highest sub-conductor current in a rating zone [A]."""
    amps = [_line_value(net, idx, n, "i_from_ka") for n in CORRIDOR_ZONES[zone]]
    amps = [a for a in amps if math.isfinite(a)]
    return max(amps) * 1000.0 if amps else float("nan")


def collect_row(net, cfg, idx, reg: dict) -> dict:
    """Network state after a converged step."""
    row = {}
    for name in REPORT_BUSES:
        row[f"v_{name}_pu"] = float(net.res_bus.at[idx.buses[name], "vm_pu"])

    for unit in DER_UNITS:
        s_idx = idx.sgens[unit]
        row[f"p_{unit}_mw"] = float(net.res_sgen.at[s_idx, "p_mw"])
        row[f"q_{unit}_mvar"] = float(net.res_sgen.at[s_idx, "q_mvar"])
        row[f"q_ref_{unit}_mvar"] = reg["q_ref"].get(unit, 0.0)

    row["p_der_total_mw"] = sum(row[f"p_{u}_mw"] for u in DER_UNITS)
    row["q_der_total_mvar"] = sum(row[f"q_{u}_mvar"] for u in DER_UNITS)
    row["p_load_total_mw"] = float(net.res_load["p_mw"].sum())
    row["q_load_total_mvar"] = float(net.res_load["q_mvar"].sum())
    row["loss_line_mw"] = float(net.res_line["pl_mw"].sum())
    row["loss_trafo_mw"] = float(net.res_trafo["pl_mw"].sum())

    for name in EXPORT_PATH_LINES:
        row[f"i_{name}_a"] = _line_value(net, idx, name, "i_from_ka") * 1000.0
        row[f"p_{name}_mw"] = _line_value(net, idx, name, "p_from_mw")
    for name in DSO_TRAFO_NAMES + PLANT_TRAFOS:
        row[f"loading_{name}_pct"] = _trafo_loading(net, idx, name)

    pcc = measure_pcc_export(net, idx.buses)
    row["pcc_p_mw"] = pcc["p_mw"]
    row["pcc_q_mvar"] = pcc["q_mvar"]
    row["pcc_s_mva"] = pcc["s_mva"]
    # Gross export: what generation delivers to the interface, counting only
    # the losses on the path it actually travels.
    row["pcc_p_gross_mw"] = (float(net.res_sgen["p_mw"].sum())
                             - corridor_losses_mw(net, idx.lines))
    window = cfg.q_window_mvar
    row["q_window_mvar"] = window
    row["q_within_window"] = int(abs(pcc["q_mvar"]) <= window)
    row["q_exceedance_mvar"] = max(0.0, abs(pcc["q_mvar"]) - window)

    taps = reg.get("taps", {})
    for name in DSO_TRAFO_NAMES:
        row[f"tap_{name}"] = taps.get(name, 0)
    for name in idx.reactors:
        step = reg["reactor_state"].get(name, C.REACTOR_STEP_INIT)
        row[f"reactor_step_{name}"] = step
        row[f"reactor_q_{name}_mvar"] = C.REACTOR_STEPS_MVAR[step]
    return row


def _rating_columns(cfg, limits, diag, source, net, idx, converged):
    """Rating, headroom and realised conductor temperature per zone."""
    cols = {"dlr_mode": cfg.dlr_mode, "rating_source": source}
    for zone in CORRIDOR_ZONES:
        terms = diag[zone]
        limit = limits[zone]
        cols[f"rating_{zone}_a"] = limit
        cols[f"rating_{zone}_ratio"] = limit / cfg.static_rating_a
        cols[f"wind_{zone}_ms"] = terms["wind_ms"]
        cols[f"phi_{zone}_deg"] = terms["phi_deg"]
        cols[f"qc_{zone}_wm"] = terms["qc_wm"]
        cols[f"qr_{zone}_wm"] = terms["qr_wm"]
        cols[f"qs_{zone}_wm"] = terms["qs_wm"]

        amps = _zone_current(net, idx, zone) if converged else float("nan")
        cols[f"i_{zone}_a"] = amps
        cols[f"loading_{zone}_pct"] = 100.0 * amps / limit if limit > 0 else float("nan")
        if converged and source == "weather" and math.isfinite(amps):
            cols[f"t_cond_{zone}_c"] = dlr.conductor_temperature(
                amps / cfg.bundle_n, terms["t_air_c"], terms["wind_ms"],
                terms["phi_deg"], terms["ghi_wm2"], cfg.site_elevation_m)
        else:
            cols[f"t_cond_{zone}_c"] = float("nan")
    cols["rating_governing_a"] = min(limits.values())
    cols["t_air_c"] = diag["Z1"]["t_air_c"]
    cols["ghi_wm2"] = diag["Z1"]["ghi_wm2"]
    return cols


def _storage_columns(cfg, phase, soc_mwh, p_realised, clamped, reserve, shortfall,
                     activation, converged, flag):
    if not cfg.storage_enabled:
        zero = 0.0 if converged else float("nan")
        return {"storage_enabled": 0, "storage_mode": "disabled",
                "storage_p_grid_mw": zero, "storage_p_plant_mw": zero,
                "storage_q_mvar": zero, "storage_soc_mwh": float("nan"),
                "storage_soc_pct": float("nan"), "storage_soc_clamped": 0,
                "storage_charge_surplus_mw": zero, "storage_charge_grid_mw": zero,
                "activation_signal_mw": activation, "storage_headroom_clamped": 0,
                "storage_shortfall_mw": zero, "storage_reserve_available_mw": zero,
                "storage_reserve_short": 0, "storage_flag": flag}
    nan = float("nan")
    return {
        "storage_enabled": 1,
        "storage_mode": phase["mode"],
        "storage_p_grid_mw": storage.to_grid_frame(p_realised) if converged else nan,
        "storage_p_plant_mw": p_realised if converged else nan,
        "storage_q_mvar": phase.get("q_mvar", nan),
        "storage_soc_mwh": soc_mwh if converged else nan,
        "storage_soc_pct": 100.0 * soc_mwh / cfg.storage_e_mwh if converged else nan,
        "storage_soc_clamped": clamped,
        "storage_charge_surplus_mw": phase["charge_surplus_mw"] if converged else nan,
        "storage_charge_grid_mw": phase["charge_grid_mw"] if converged else nan,
        "activation_signal_mw": activation,
        "storage_headroom_clamped": phase["headroom_clamped"],
        "storage_shortfall_mw": phase["shortfall_mw"] if converged else nan,
        "storage_reserve_available_mw": reserve if converged else nan,
        "storage_reserve_short": shortfall,
        "storage_flag": flag,
    }


def run(cfg, net, buses, inputs, progress=True) -> pd.DataFrame:
    """Run the full simulation and return one row per timestep."""
    idx = NetIndex.build(net, buses)
    index = inputs["index"]
    rating_weather = inputs["rating_weather"]
    if cfg.dlr_mode > 0 and rating_weather is None:
        raise RuntimeError(f"dlr_mode={cfg.dlr_mode} requires weather data")

    load = inputs["load"]
    wind = inputs["wind"]
    pv = inputs["pv"].to_numpy()
    activation = inputs["reserve"].to_numpy()

    p_a = load["p_sub_a_mw"].to_numpy()
    q_a = load["q_sub_a_mvar"].to_numpy()
    p_b = load["p_sub_b_mw"].to_numpy()
    q_b = load["q_sub_b_mvar"].to_numpy()
    p_agg = load["p_agg_mw"].to_numpy()
    q_agg = load["q_agg_mvar"].to_numpy()
    q_cap = load["q_shunt_a_mvar"].to_numpy()
    available = {u: (wind[u].to_numpy() if u != "PV_1" else pv) for u in DER_UNITS}

    reactor_state = dict.fromkeys(idx.reactors, C.REACTOR_STEP_INIT)
    soc_mwh = cfg.storage_soc_init * cfg.storage_e_mwh
    previous_ok = False

    rows = []
    counters = {"not_converged": 0, "curtail_failures": 0, "storage_resolve_failed": 0,
                "soc_clamped": 0, "headroom_clamped": 0, "reserve_short": 0,
                "over_temperature": 0}
    t0 = time.time()

    for i, ts in enumerate(index):
        net.load.at[idx.loads["LOAD_SUB_A"], "p_mw"] = p_a[i]
        net.load.at[idx.loads["LOAD_SUB_A"], "q_mvar"] = q_a[i]
        net.load.at[idx.loads["LOAD_SUB_B"], "p_mw"] = p_b[i]
        net.load.at[idx.loads["LOAD_SUB_B"], "q_mvar"] = q_b[i]
        net.load.at[idx.loads["LOAD_AGG"], "p_mw"] = p_agg[i]
        net.load.at[idx.loads["LOAD_AGG"], "q_mvar"] = q_agg[i]
        net.shunt.at[idx.shunts["CAP_SUB_A"], "q_mvar"] = -q_cap[i]

        dispatch = {}
        for unit in DER_UNITS:
            value = float(available[unit][i]) if cfg.der_enabled[unit] else 0.0
            dispatch[unit] = value if math.isfinite(value) else 0.0
            net.sgen.at[idx.sgens[unit], "p_mw"] = dispatch[unit]
            mode = cfg.pv_mode if unit == "PV_1" else cfg.control_mode
            net.sgen.at[idx.sgens[unit], "q_mvar"] = (
                cfg.cosphi_sign_factor * dispatch[unit] * C.TAN_PHI
                if mode == "cosphi" else 0.0)

        phase1 = storage.phase1_intent(net, cfg, idx, float(activation[i]), soc_mwh)
        reg = regulate(net, cfg, idx, dispatch, reactor_state, warm_start=previous_ok)
        reactor_state = reg["reactor_state"]
        ok = reg["ok"]

        limits, diag, source = dlr.operative_limits(cfg, rating_weather, ts)

        resolve_failed = False
        if cfg.storage_enabled and ok:
            phase2 = storage.phase2_reconcile(net, cfg, idx, phase1, soc_mwh)
            if not solve(net, "results", deep=True):
                ok = False
                resolve_failed = True
        else:
            phase2 = {"mode": phase1["mode"], "p_plant_mw": 0.0, "headroom_clamped": 0,
                      "shortfall_mw": 0.0, "charge_surplus_mw": 0.0, "charge_grid_mw": 0.0}

        if ok and cfg.curtailment:
            curtail_record = curtailment.curtail(net, cfg, idx, limits)
            ok = ok and curtail_record["ok"]
        elif ok:
            curtail_record = curtailment.uncurtailed_record(net, cfg, idx, limits)
        else:
            curtail_record = curtailment.default_record(cfg)
        previous_ok = ok

        p_realised = phase2["p_plant_mw"] if cfg.storage_enabled else 0.0
        soc_mwh, soc_clamped, p_realised = storage.update_soc(
            cfg, soc_mwh, p_realised, apply=ok and cfg.storage_enabled)
        reserve, short = storage.reserve_availability(cfg, soc_mwh, p_realised)

        if not cfg.storage_enabled:
            flag = "ok" if ok else "not_converged"
        elif resolve_failed:
            flag = "resolve_failed"
        elif not ok:
            flag = "not_converged"
        elif soc_clamped:
            flag = "soc_limit"
        elif phase2["headroom_clamped"]:
            flag = "headroom_clamped"
        else:
            flag = "ok"

        row = collect_row(net, cfg, idx, reg) if ok else {}
        row["converged"] = ok
        row["reg_converged"] = reg["converged"]
        row["reg_iterations"] = reg["iterations"]
        row["reg_backtracks"] = reg["backtracks"]
        row["q_saturated_units"] = reg["q_saturated"]
        row["droop_gated_units"] = reg["droop_gated"]
        row["q_tracking_error_mvar"] = reg["q_tracking_error_mvar"]
        row["min_damping"] = reg["min_damping"]
        row["reactor_flag"] = reg["reactor_flag"]
        row["q_flag"] = reg["q_flag"]
        row["oltc_moves"] = reg["oltc_moves_a"] + reg["oltc_moves_b"]
        row["reactor_moves"] = reg["reactor_moves"]
        row["actuators_frozen"] = reg["oltc_frozen"] + reg["reactor_frozen"]
        for unit in DER_UNITS:
            row[f"available_{unit}_mw"] = dispatch[unit]
        row["available_total_mw"] = sum(dispatch.values())
        row["control_mode"] = cfg.control_mode
        row["conductor"] = cfg.conductor

        if cfg.storage_enabled and ok:
            phase2["q_mvar"] = float(net.storage.at[idx.storage, "q_mvar"])
        row.update(_rating_columns(cfg, limits, diag, source, net, idx, ok))
        row.update(_storage_columns(cfg, phase2, soc_mwh, p_realised, soc_clamped,
                                    reserve, short, float(activation[i]), ok, flag))
        row.update({k: v for k, v in curtail_record.items() if k != "ok"})

        if ok:
            _, _, _, detail = scan(net, idx, limits, cfg)
            row.update(violation_flags(detail))
        else:
            row.update({f"viol_L{n}": 0 for n in (1, 2, 3, 4)})

        counters["not_converged"] += int(not ok)
        counters["curtail_failures"] += curtail_record["curtail_failures"]
        counters["storage_resolve_failed"] += int(resolve_failed)
        counters["soc_clamped"] += int(soc_clamped)
        counters["headroom_clamped"] += int(phase2["headroom_clamped"])
        counters["reserve_short"] += int(short)
        counters["over_temperature"] += int(any(
            row.get(f"t_cond_{z}_c", float("nan")) > cfg.t_cond_max_c
            for z in CORRIDOR_ZONES))
        rows.append(row)

        if progress and (i + 1) % 960 == 0:
            done = (i + 1) / len(index)
            print(f"    {done:5.0%}  {i + 1:6d}/{len(index)} steps  "
                  f"{time.time() - t0:5.0f} s", flush=True)

    result = pd.DataFrame(rows, index=index)
    result.index.name = "time"
    result.attrs["counters"] = counters
    result.attrs["runtime_s"] = time.time() - t0
    return result


def realised_generation(result: pd.DataFrame) -> pd.Series:
    """Active power actually injected by the fleet, in MW."""
    cols = [f"p_{u}_mw" for u in DER_UNITS if f"p_{u}_mw" in result.columns]
    return result[cols].sum(axis=1) if cols else pd.Series(np.nan, index=result.index)
