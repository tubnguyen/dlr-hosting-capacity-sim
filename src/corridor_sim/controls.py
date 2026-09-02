"""Voltage and reactive power control for one timestep.

Three actuators share the corridor:

* a stepped MV shunt reactor, the fine and primary responder,
* an on-load tap changer, the coarse backup, tested only against the voltage
  the reactor has already settled,
* voltage-reactive power droop at each plant connection point.

Both switched actuators carry a per-step move budget and freeze after they
reverse direction, because an actuator that has bracketed its setpoint within
one 15-minute step is hunting rather than controlling.

The droop loop re-solves immediately after every reactive update, so the
reactive power recorded for a step is always a solved value rather than a
command that was never reflected in the network state. Convergence requires
both a settled voltage and a closed reactive tracking error: under-relaxation
can drive the increment below tolerance while a unit is still far from its
reference, and that must not be reported as converged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandapower as pp

from . import constants as C
from .constraints import measure_pcc_export
from .network import DROOP_LOCAL_BUS

DROOP_UNITS = ("WF_1", "WF_2", "WF_3", "PV_1", C.BESS_NAME)


@dataclass
class NetIndex:
    """Name-to-index lookups built once per run."""

    buses: dict
    sgens: dict
    trafos: dict
    shunts: dict
    lines: dict
    loads: dict
    storage: int = -1
    oltc_a: list = field(default_factory=lambda: ["T_SUB_A1", "T_SUB_A2"])
    oltc_b: list = field(default_factory=lambda: ["T_SUB_B"])
    reactors: list = field(default_factory=lambda: ["RX_SUB_A", "RX_SUB_B"])

    @classmethod
    def build(cls, net, buses):
        by_name = lambda table: {r["name"]: i for i, r in table.iterrows()}  # noqa: E731
        return cls(buses=buses, sgens=by_name(net.sgen), trafos=by_name(net.trafo),
                   shunts=by_name(net.shunt), lines=by_name(net.line),
                   loads=by_name(net.load),
                   storage=int(net.storage.index[net.storage.name == C.BESS_NAME][0]))

    @property
    def reactor_buses(self) -> list:
        return ["SUB_A_MV", "SUB_B_MV"]


# ── Power flow ───────────────────────────────────────────────────────────────
try:                                    # numba speeds the power flow up several fold
    import numba as _numba  # noqa: F401
    _NUMBA = True
except ImportError:
    _NUMBA = False


def _runpp(net, init) -> bool:
    try:
        pp.runpp(net, algorithm="nr", init=init, calculate_voltage_angles=True,
                 enforce_q_lims=False, max_iteration=50, tolerance_mva=1e-6,
                 numba=_NUMBA)
    except Exception:
        return False
    return not net.res_bus["vm_pu"].isna().any()


def solve(net, init="dc", floor_pu=0.75, deep=False) -> bool:
    """Solve the power flow, falling back through progressively harder starts.

    `deep` adds a flat start and then a continuation that ramps injection in
    from a level that does converge. That matters for the first solve of a
    timestep at high wind, where a cold start with zero reactive power can
    diverge at a dispatch that is perfectly solvable once the plants support
    voltage. It is deliberately off inside the control and curtailment loops,
    where a cheap failure is informative and a deep retry would cost dozens of
    power flows per step.

    `floor_pu` rejects the spurious low-voltage root; it is not an operating
    limit.
    """
    for start in ((init, "dc", "flat") if deep else (init, "dc")):
        if _runpp(net, start) and float(net.res_bus["vm_pu"].min()) >= floor_pu:
            return True
    return _continuation(net, floor_pu) if deep else False


def _continuation(net, floor_pu) -> bool:
    """Ramp generation and storage in from a solvable level."""
    p_sgen = net.sgen["p_mw"].copy()
    q_sgen = net.sgen["q_mvar"].copy()
    p_st = net.storage["p_mw"].copy()
    q_st = net.storage["q_mvar"].copy()

    def apply(scale):
        net.sgen["p_mw"] = p_sgen * scale
        net.sgen["q_mvar"] = q_sgen * scale
        net.storage["p_mw"] = p_st * scale
        net.storage["q_mvar"] = q_st * scale

    anchor = 0.0
    for scale in (0.25, 0.10):
        apply(scale)
        if _runpp(net, "dc") and float(net.res_bus["vm_pu"].min()) >= floor_pu:
            anchor = scale
            break
    if anchor == 0.0:
        apply(1.0)
        return False

    scale, step = anchor, 0.25
    for _ in range(40):
        if scale >= 1.0:
            break
        trial = min(1.0, scale + step)
        apply(trial)
        if _runpp(net, "results") and float(net.res_bus["vm_pu"].min()) >= floor_pu:
            scale = trial
        else:
            step *= 0.5
            if step < 0.02:
                break
            apply(scale)
            _runpp(net, "results")
    apply(1.0)
    return _runpp(net, "results") and float(net.res_bus["vm_pu"].min()) >= floor_pu


# ── Switched actuators ───────────────────────────────────────────────────────
class MoveBudget:
    """Per-step reversal lock and move-rate limit for one actuator group."""

    def __init__(self, budget: int):
        self.budget = budget
        self._direction: dict = {}
        self._moves: dict = {}
        self._frozen: set = set()

    def allowed(self, name: str) -> bool:
        return name not in self._frozen and self._moves.get(name, 0) < self.budget

    def record(self, name: str, direction: int) -> None:
        previous = self._direction.get(name)
        if previous and direction and direction != previous:
            self._frozen.add(name)
        self._direction[name] = direction
        self._moves[name] = self._moves.get(name, 0) + 1

    def moves(self, name: str) -> int:
        return self._moves.get(name, 0)

    def n_frozen(self) -> int:
        return len(self._frozen)


def oltc_step(net, idx, trafos, bus_name, budget: MoveBudget):
    """One tap operation toward the MV setpoint.

    With the tap changer on the HV winding, raising tap_pos raises the HV turns
    and therefore lowers the LV voltage.
    """
    target = C.OLTC_TARGET_KV / C.V_MV_KV
    deadband = C.OLTC_DEADBAND_KV / C.V_MV_KV
    v = float(net.res_bus.at[idx.buses[bus_name], "vm_pu"])
    error = v - target
    if abs(error) <= deadband:
        return False, {n: int(net.trafo.at[idx.trafos[n], "tap_pos"]) for n in trafos}

    direction = 1 if error > 0 else -1
    moved = False
    for name in trafos:
        t_idx = idx.trafos.get(name)
        if t_idx is None or not budget.allowed(name):
            continue
        tap = int(net.trafo.at[t_idx, "tap_pos"])
        if direction > 0 and tap < int(net.trafo.at[t_idx, "tap_max"]):
            net.trafo.at[t_idx, "tap_pos"] = tap + 1
            moved = True
            budget.record(name, 1)
        elif direction < 0 and tap > int(net.trafo.at[t_idx, "tap_min"]):
            net.trafo.at[t_idx, "tap_pos"] = tap - 1
            moved = True
            budget.record(name, -1)
    return moved, {n: int(net.trafo.at[idx.trafos[n], "tap_pos"]) for n in trafos}


def reactor_step(net, idx, state: dict, budget: MoveBudget):
    """One step of the MV shunt reactors: more absorption lowers voltage."""
    high = (C.V_MV_KV + C.REACTOR_DEADBAND_KV) / C.V_MV_KV
    low = (C.V_MV_KV - C.REACTOR_DEADBAND_KV) / C.V_MV_KV
    new = dict(state)
    moved = False
    for name, bus in zip(idx.reactors, idx.reactor_buses, strict=True):
        s_idx = idx.shunts.get(name)
        if s_idx is None or not budget.allowed(name):
            continue
        v = float(net.res_bus.at[idx.buses[bus], "vm_pu"])
        step = new.get(name, C.REACTOR_STEP_INIT)
        if v > high and step < len(C.REACTOR_STEPS_MVAR) - 1:
            step += 1
            budget.record(name, 1)
            moved = True
        elif v < low and step > 0:
            step -= 1
            budget.record(name, -1)
            moved = True
        new[name] = step
        net.shunt.at[s_idx, "q_mvar"] = C.REACTOR_STEPS_MVAR[step]
    return moved, new


def release_reactors(net, idx, state: dict, budget: MoveBudget):
    """Stage the reactors down one step at a time until reactive import at the
    PCC falls back under the guard threshold. Real reactors step with a time
    delay; dumping every stage at once is both unphysical and a reliable way to
    produce a singular Jacobian."""
    new = dict(state)
    released = False
    for _ in range(C.REACTOR_MOVE_BUDGET):
        if measure_pcc_export(net, idx.buses)["q_mvar"] <= C.Q_GUARD_MVAR:
            break
        moved = False
        for name in idx.reactors:
            if new[name] > 0:
                new[name] -= 1
                net.shunt.at[idx.shunts[name], "q_mvar"] = C.REACTOR_STEPS_MVAR[new[name]]
                moved = released = True
        if not moved or not solve(net, "results"):
            break
    return new, released


# ── Droop ────────────────────────────────────────────────────────────────────
def droop_reference(v_pu, p_mw, q_limit, p_rated, min_frac=C.DROOP_P_MIN_FRAC) -> float:
    """Voltage-reactive power droop characteristic.

    Above the deadband the unit absorbs (negative Q) to pull voltage down;
    below it the unit injects to hold voltage up. Reactive support is gated off
    below `min_frac` of rated active power, where the converter has little
    headroom.
    """
    if p_mw < min_frac * p_rated:
        return 0.0
    error = v_pu - C.V_REF_PU
    if abs(error) <= C.DROOP_DEADBAND_PU:
        return 0.0
    effective = error - math.copysign(C.DROOP_DEADBAND_PU, error)
    q_ref = -(q_limit / (C.DROOP_SLOPE_PCT / 100.0)) * effective
    return max(-q_limit, min(q_limit, q_ref))


def measurement_bus(cfg, unit: str) -> str:
    """Bus a unit's droop regulates against, under the configured policy."""
    if cfg.droop_measurement == "pilot_tap_w":
        return "TAP_W"
    if cfg.droop_measurement == "pilot_sub_a":
        return "SUB_A"
    return DROOP_LOCAL_BUS[unit]


def measured_voltage(net, idx, cfg, unit: str) -> float:
    return float(net.res_bus.at[idx.buses[measurement_bus(cfg, unit)], "vm_pu"])


class _Relaxation:
    """Per-unit under-relaxation that halves whenever a step reverses.

    The droop characteristic is steep, so a fixed damping factor sits close to
    the stability boundary and the margin moves with loading. Halving on
    reversal squeezes an oscillating unit onto its fixed point instead of
    letting it ring until the iteration budget runs out.
    """

    def __init__(self, keys):
        self.damping = dict.fromkeys(keys, C.DROOP_Q_DAMP)
        self._last = dict.fromkeys(keys, 0)
        self.error = {}

    def step(self, key, q_now, q_ref) -> float:
        delta = q_ref - q_now
        self.error[key] = abs(delta)
        direction = (delta > 0) - (delta < 0)
        if direction and self._last[key] and direction != self._last[key]:
            self.damping[key] = max(C.DROOP_Q_DAMP_MIN, self.damping[key] * 0.5)
        if direction:
            self._last[key] = direction
        return q_now + self.damping[key] * delta

    def shrink_all(self) -> None:
        """Back off every unit after a step the power flow could not follow."""
        for key in self.damping:
            self.damping[key] = max(C.DROOP_Q_DAMP_MIN, self.damping[key] * 0.5)

    @property
    def max_error(self) -> float:
        return max(self.error.values()) if self.error else 0.0

    @property
    def min_damping(self) -> float:
        return min(self.damping.values()) if self.damping else C.DROOP_Q_DAMP


def _entry_solve(net, cfg, idx, dispatch: dict, warm_start: bool) -> bool:
    """First solve of the timestep, with a reactive seed for stressed starts.

    A heavily loaded inductive corridor absorbs tens of megavars, so at high
    output there is no solution with the plants at zero reactive power even
    though one exists once they support voltage. Seeding the droop units at
    their capability limit in the supporting direction gives the iteration a
    feasible starting point; the converged answer is still set by the droop
    characteristic, which walks the reactive power back down from there.
    """
    if solve(net, "results" if warm_start else "dc", deep=True):
        return True
    for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
        mode = cfg.pv_mode if unit == "PV_1" else cfg.control_mode
        if mode == "droop":
            net.sgen.at[idx.sgens[unit], "q_mvar"] = min(
                C.Q_LIMIT_MVAR[unit], dispatch[unit] * C.TAN_PHI)
    if cfg.storage_droop_active:
        net.storage.at[idx.storage, "q_mvar"] = cfg.q_limit_storage
    return solve(net, "dc")


MAX_BACKTRACKS = 6


def _snapshot(net):
    """Capture the controllable state so a failed step can be undone."""
    return (net.sgen["q_mvar"].to_numpy(copy=True),
            net.storage["q_mvar"].to_numpy(copy=True),
            net.trafo["tap_pos"].to_numpy(copy=True),
            net.shunt["q_mvar"].to_numpy(copy=True))


def _restore(net, snap) -> None:
    net.sgen["q_mvar"] = snap[0]
    net.storage["q_mvar"] = snap[1]
    net.trafo["tap_pos"] = snap[2]
    net.shunt["q_mvar"] = snap[3]


def _rollback(net, snap, reactor_state: dict) -> dict:
    """Undo the last actuator move and re-solve the state it came from."""
    _restore(net, snap)
    solve(net, "results")
    return dict(reactor_state)


def _read_taps(net, idx):
    """Tap positions as they now stand on the network."""
    return ({n: int(net.trafo.at[idx.trafos[n], "tap_pos"]) for n in idx.oltc_a},
            {n: int(net.trafo.at[idx.trafos[n], "tap_pos"]) for n in idx.oltc_b})


def regulate(net, cfg, idx, dispatch: dict, reactor_state: dict, warm_start=True) -> dict:
    """Run the coordinated control loop for one timestep."""
    state = dict(reactor_state)
    for name in idx.reactors:
        net.shunt.at[idx.shunts[name], "q_mvar"] = C.REACTOR_STEPS_MVAR[state[name]]

    ok = _entry_solve(net, cfg, idx, dispatch, warm_start)
    result = {
        "ok": ok, "iterations": 0, "converged": False, "reactor_state": state,
        "reactor_flag": "not_converged", "q_flag": "not_converged",
        "oltc_moves_a": 0, "oltc_moves_b": 0, "reactor_moves": 0,
        "oltc_frozen": 0, "reactor_frozen": 0, "droop_gated": 0,
        "q_tracking_error_mvar": 0.0, "min_damping": C.DROOP_Q_DAMP,
        "backtracks": 0, "q_saturated": 0,
        "taps": {}, "q_ref": dict.fromkeys(DROOP_UNITS, 0.0),
    }

    # Count units whose droop is suppressed by the low-output gate, so withheld
    # reactive support is visible rather than silent.
    gated = 0
    for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
        mode = cfg.pv_mode if unit == "PV_1" else cfg.control_mode
        if mode == "droop" and dispatch[unit] < cfg.droop_p_min_frac * C.DER_RATING_MW[unit]:
            gated += 1
    result["droop_gated"] = gated
    if not ok:
        return result

    oltc_budget = MoveBudget(C.OLTC_MOVE_BUDGET)
    reactor_budget = MoveBudget(C.REACTOR_MOVE_BUDGET)
    relax = _Relaxation(DROOP_UNITS)
    taps_a, taps_b = _read_taps(net, idx)

    storage_droop = cfg.storage_droop_active
    droop_active = (cfg.control_mode == "droop" or cfg.pv_mode == "droop" or storage_droop)
    previous_v: dict = {}
    n_iter = 0
    failed = False
    backtracks = 0
    last_good = _snapshot(net)

    for outer in range(cfg.max_outer_iter):
        n_iter = outer + 1
        moved = q_moved = False
        current_v: dict = {}

        for unit in ("WF_1", "WF_2", "WF_3", "PV_1"):
            mode = cfg.pv_mode if unit == "PV_1" else cfg.control_mode
            if mode != "droop":
                continue
            v = measured_voltage(net, idx, cfg, unit)
            current_v[unit] = v
            q_ref = droop_reference(v, dispatch[unit], C.Q_LIMIT_MVAR[unit],
                                    C.DER_RATING_MW[unit], cfg.droop_p_min_frac)
            s_idx = idx.sgens[unit]
            q_now = float(net.sgen.at[s_idx, "q_mvar"])
            q_new = relax.step(unit, q_now, q_ref)
            if abs(q_new - q_now) > C.DROOP_Q_STEP_TOL_MVAR:
                q_moved = True
            net.sgen.at[s_idx, "q_mvar"] = q_new
            result["q_ref"][unit] = q_ref

        if storage_droop:
            # Grid-forming storage supplies reactive power at zero active
            # power, so the low-output gate is bypassed by passing the rating.
            v = float(net.res_bus.at[idx.buses["STORAGE_PCC"], "vm_pu"])
            current_v[C.BESS_NAME] = v
            q_ref = droop_reference(v, cfg.storage_p_mw, cfg.q_limit_storage, cfg.storage_p_mw)
            q_now = float(net.storage.at[idx.storage, "q_mvar"])
            q_new = relax.step(C.BESS_NAME, q_now, q_ref)
            if abs(q_new - q_now) > C.DROOP_Q_STEP_TOL_MVAR:
                q_moved = True
            net.storage.at[idx.storage, "q_mvar"] = q_new
            result["q_ref"][C.BESS_NAME] = q_ref

        # Re-solve before anything is judged on the reactive power just written.
        # A step the power flow cannot follow is undone rather than abandoned:
        # the loop backs off its damping and retries from the last solved
        # state, so a timestep always ends on a solved network even when the
        # droop cannot settle.
        if q_moved:
            moved = True
            if not solve(net, "results"):
                _restore(net, last_good)
                if not solve(net, "results"):
                    failed = True
                    break
                relax.shrink_all()
                backtracks += 1
                if backtracks >= MAX_BACKTRACKS:
                    break
                continue
            last_good = _snapshot(net)

        if droop_active and previous_v:
            stable = (not moved) and outer > 1
            for unit, v in current_v.items():
                if unit in previous_v:
                    stable = stable and abs(v - previous_v[unit]) < C.DROOP_V_TOL_PU
            if stable and relax.max_error > C.DROOP_Q_ERR_TOL_MVAR:
                stable = False       # settled iteration, but the target is not tracked
            if stable:
                result["converged"] = True
                break
        previous_v = current_v

        # Reactor first, re-solve, then test the tap changer on the fresh voltage.
        # A rollback must undo the bookkeeping as well as the network, or the
        # row would report a reactor step and tap positions that the solved
        # network never had, and the stale reactor state would carry into the
        # next timestep.
        state_before = dict(state)
        reactor_moved, state = reactor_step(net, idx, state, reactor_budget)
        if reactor_moved and not solve(net, "results"):
            state = _rollback(net, last_good, state_before)
            taps_a, taps_b = _read_taps(net, idx)
            break

        moved_a, taps_a = oltc_step(net, idx, idx.oltc_a, "SUB_A_MV", oltc_budget)
        moved_b, taps_b = oltc_step(net, idx, idx.oltc_b, "SUB_B_MV", oltc_budget)
        if (moved_a or moved_b) and not solve(net, "results"):
            state = _rollback(net, last_good, state_before)
            taps_a, taps_b = _read_taps(net, idx)
            break
        last_good = _snapshot(net)
        moved = moved or reactor_moved or moved_a or moved_b

        if not droop_active and not moved and outer >= 1:
            result["converged"] = True
            break

    saturated = sum(
        abs(float(net.sgen.at[idx.sgens[u], "q_mvar"])) >= 0.99 * C.Q_LIMIT_MVAR[u]
        for u in ("WF_1", "WF_2", "WF_3", "PV_1"))
    result.update({
        "iterations": n_iter,
        "backtracks": backtracks,
        "q_saturated": int(saturated),
        "q_tracking_error_mvar": relax.max_error,
        "min_damping": relax.min_damping,
        "oltc_moves_a": sum(oltc_budget.moves(n) for n in idx.oltc_a),
        "oltc_moves_b": sum(oltc_budget.moves(n) for n in idx.oltc_b),
        "reactor_moves": sum(reactor_budget.moves(n) for n in idx.reactors),
        "oltc_frozen": oltc_budget.n_frozen(),
        "reactor_frozen": reactor_budget.n_frozen(),
        "reactor_state": state,
    })
    if failed:
        result["ok"] = False
        return result

    # Reactive import guard at the PCC.
    q_pcc = measure_pcc_export(net, idx.buses)["q_mvar"]
    if q_pcc > C.Q_GUARD_MVAR:
        state, _ = release_reactors(net, idx, state, reactor_budget)
        result["reactor_state"] = state
        if not solve(net, "results"):
            result["ok"] = False
            result["reactor_flag"] = "release_failed"
            return result
        _, taps_a = oltc_step(net, idx, idx.oltc_a, "SUB_A_MV", oltc_budget)
        _, taps_b = oltc_step(net, idx, idx.oltc_b, "SUB_B_MV", oltc_budget)
        if not solve(net, "results"):
            result["ok"] = False
            result["reactor_flag"] = "release_failed"
            return result
        result["reactor_flag"] = "released"
    else:
        result["reactor_flag"] = "normal"

    q_after = measure_pcc_export(net, idx.buses)["q_mvar"]
    result["q_flag"] = ("high" if q_after > C.Q_MONITOR_MVAR
                        else "elevated" if q_after > C.Q_GUARD_MVAR else "ok")
    result["taps"] = {**taps_a, **taps_b}
    return result
