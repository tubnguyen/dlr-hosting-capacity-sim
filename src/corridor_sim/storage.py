"""Battery storage dispatch, state of charge and reserve accounting.

Sign convention, stated once because getting it wrong is silent:

    plant frame   pandapower storage.p_mw: charging positive, discharging
                  negative. State of charge integrates on this frame.
    grid frame    discharging positive, charging negative, matching the export
                  sign used everywhere else.

Both are written to every row so a sign error is auditable rather than hidden.

Reach is asymmetric, and the asymmetry runs both ways. The battery connects
near the receiving end, so it cannot relieve a thermal overload on the corridor
sections upstream of its tap - only curtailment can. It does share the final
section into the interface with generation, so discharging there competes with
export for that segment, and because the battery is not curtailable the plants
are cut instead. Both follow from where it is connected.

Dispatch runs in two phases. Phase one sets an intent before the network is
solved; phase two reconciles that intent against the export headroom the
solved network actually leaves.
"""
from __future__ import annotations

from . import constants as C
from .constraints import measure_pcc_export


def to_grid_frame(p_plant_mw: float) -> float:
    return -p_plant_mw


def dischargeable_mw(cfg, soc_mwh: float) -> float:
    """Power deliverable for one step without breaching the physical floor."""
    usable = max(0.0, soc_mwh - cfg.soc_min_mwh)
    return min(cfg.storage_p_mw, usable * C.BESS_ETA_DIS / C.DT_H)


def chargeable_mw(cfg, soc_mwh: float) -> float:
    """Power absorbable for one step without breaching the physical ceiling."""
    headroom = max(0.0, cfg.soc_max_mwh - soc_mwh)
    return min(cfg.storage_p_mw, headroom / C.BESS_ETA_CH / C.DT_H)


def _static_q(cfg, p_plant_mw: float) -> float:
    """Reactive setpoint for the non-droop modes."""
    if cfg.storage_q_mode == "fixed":
        return -0.5 * cfg.q_limit_storage
    if cfg.storage_q_mode == "cosphi":
        return cfg.cosphi_sign_factor * p_plant_mw * C.TAN_PHI
    return 0.0


def phase1_intent(net, cfg, idx, activation_mw: float, soc_mwh: float) -> dict:
    """Provisional setpoint, set before the network is solved.

    Discharge fires when the balancing signal clears the trigger and the state
    of charge is above the reserve floor, which keeps the contracted upward
    reserve deliverable instead of letting energy delivery drain it.
    """
    if not cfg.storage_enabled:
        net.storage.at[idx.storage, "p_mw"] = 0.0
        net.storage.at[idx.storage, "q_mvar"] = 0.0
        return {"mode": "disabled", "intent_mw": 0.0}

    if activation_mw > C.BESS_ACTIVATION_TRIGGER_MW and soc_mwh > cfg.soc_reserve_mwh:
        intent = min(cfg.storage_contract_mw, dischargeable_mw(cfg, soc_mwh))
        net.storage.at[idx.storage, "p_mw"] = -intent
        mode = "discharge"
    else:
        intent = 0.0
        net.storage.at[idx.storage, "p_mw"] = 0.0    # charging is resolved in phase two
        mode = "idle"

    net.storage.at[idx.storage, "q_mvar"] = (
        0.0 if cfg.storage_q_mode == "droop"
        else _static_q(cfg, float(net.storage.at[idx.storage, "p_mw"])))
    return {"mode": mode, "intent_mw": intent}


def phase2_reconcile(net, cfg, idx, intent: dict, soc_mwh: float) -> dict:
    """Clamp the discharge to the export headroom, or resolve the charge split.

    The export a solved network would show with the battery idle is obtained
    algebraically rather than by a second power flow: the battery sits one
    short tie-line behind the PCC, so its marginal contribution to export is
    within a fraction of a percent of one to one.
    """
    if not cfg.storage_enabled:
        return {"mode": "disabled", "p_plant_mw": 0.0, "headroom_clamped": 0,
                "shortfall_mw": 0.0, "charge_surplus_mw": 0.0, "charge_grid_mw": 0.0}

    p_plant = float(net.storage.at[idx.storage, "p_mw"])
    export_now = measure_pcc_export(net, idx.buses)["p_mw"]
    export_idle = export_now - to_grid_frame(p_plant)
    headroom = cfg.export_cap_mw - export_idle

    mode = intent["mode"]
    clamped = 0
    shortfall = charge_surplus = charge_grid = 0.0

    if mode == "discharge":
        wanted = intent["intent_mw"]
        if wanted > headroom and not cfg.storage_may_curtail_der:
            # Generation has priority: the battery delivers less than contracted
            # and the shortfall is recorded rather than absorbed.
            delivered = max(0.0, headroom)
            clamped = 1
            shortfall = wanted - delivered
        else:
            delivered = wanted
        net.storage.at[idx.storage, "p_mw"] = -delivered
    else:
        surplus = max(0.0, export_idle - cfg.export_cap_mw)
        capacity = chargeable_mw(cfg, soc_mwh)
        charge_surplus = min(surplus, capacity)
        if cfg.storage_charge_source == "grid_only":
            charge_surplus, charge_grid = 0.0, capacity
        elif (cfg.storage_charge_source == "surplus_then_grid"
              and soc_mwh < cfg.soc_recharge_target_mwh):
            charge_grid = max(0.0, capacity - charge_surplus)
        total_charge = charge_surplus + charge_grid
        if total_charge > 0.005:
            mode = ("charge_mixed" if charge_surplus > 0.005 and charge_grid > 0.005
                    else "charge_surplus" if charge_surplus > 0.005 else "charge_grid")
        net.storage.at[idx.storage, "p_mw"] = total_charge

    if cfg.storage_q_mode != "droop":
        net.storage.at[idx.storage, "q_mvar"] = _static_q(
            cfg, float(net.storage.at[idx.storage, "p_mw"]))

    return {"mode": mode, "p_plant_mw": float(net.storage.at[idx.storage, "p_mw"]),
            "headroom_clamped": clamped, "shortfall_mw": shortfall,
            "charge_surplus_mw": charge_surplus, "charge_grid_mw": charge_grid}


def update_soc(cfg, soc_mwh: float, p_plant_mw: float, apply: bool):
    """Integrate the state of charge on realised power only.

    On a step that did not converge, nothing is integrated: no energy
    bookkeeping is possible for power that was never delivered. When a limit
    clamps the result, the realised power is backed out of the actual state of
    charge change so the two stay consistent.
    """
    if not apply:
        return soc_mwh, 0, p_plant_mw
    delta = (C.BESS_ETA_CH * p_plant_mw * C.DT_H if p_plant_mw > 0
             else p_plant_mw / C.BESS_ETA_DIS * C.DT_H)
    new = soc_mwh + delta
    if new > cfg.soc_max_mwh:
        realised = (cfg.soc_max_mwh - soc_mwh) / (C.BESS_ETA_CH * C.DT_H)
        return cfg.soc_max_mwh, 1, realised
    if new < cfg.soc_min_mwh:
        realised = (cfg.soc_min_mwh - soc_mwh) * C.BESS_ETA_DIS / C.DT_H
        return cfg.soc_min_mwh, 1, realised
    return new, 0, p_plant_mw


def reserve_availability(cfg, soc_mwh: float, p_plant_mw: float):
    """Upward reserve still deliverable after this step, and whether it is short.

    A shortfall surfaces the conflict between holding a reserve and using the
    same asset to deliver energy, as a counted per-step result.
    """
    if not cfg.storage_enabled:
        return 0.0, 0
    discharging = max(0.0, -p_plant_mw)
    available = min(cfg.storage_reserve_mw,
                    dischargeable_mw(cfg, soc_mwh),
                    cfg.storage_p_mw - discharging)
    return available, int(available < cfg.storage_reserve_mw - 1e-9)
