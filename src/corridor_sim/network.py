"""pandapower model of the study corridor.

Topology, west to east:

    WF_3 ──┐
           SUB_A ══ TAP_PV ══ TAP_W ══ SUB_C ══ TAP_B ══ PCC ── SUB_E ── SUB_B
            │         │         │                │        │
          20 kV      PV_1   WF_2 ── WF_1        BESS    grid

The double line is the constrained export corridor: everything the wind and
solar plants generate reaches the grid through it. Rating zone Z1 covers
SUB_A to SUB_C, zone Z2 covers SUB_C to the PCC.

Every element is built on every run. Disabling a plant zeroes its power rather
than removing it, so all scenarios share one topology and stay comparable.
"""
from __future__ import annotations

import inspect
import math

import pandapower as pp

from . import constants as C

# Corridor segments carrying export current, grouped by rating zone.
CORRIDOR_ZONES = {
    "Z1": ["CORR_A_PV", "CORR_PV_W", "CORR_W_C"],
    "Z2": ["CORR_C_B", "CORR_B_PCC", "LINK_PCC_E", "SPUR_B_E"],
}
CORRIDOR_LINES = [n for lines in CORRIDOR_ZONES.values() for n in lines]
EXPORT_PATH_LINES = ["CORR_A_PV", "CORR_PV_W", "CORR_W_C", "CORR_C_B", "CORR_B_PCC"]

DSO_TRAFO_NAMES = list(C.DSO_TRAFOS)
PLANT_LINES = ["LAT_WF1_WF2", "LAT_WF2_TAP", "LAT_WF3_A", "CAB_PV", "LAT_BESS"]
WF_TRAFO_BASES = ["T_WF1", "T_WF2", "T_WF3"]
PLANT_TRAFOS = ["T_PV", "T_BESS"] + [f"{b}_{k}" for b in WF_TRAFO_BASES for k in (1, 2)]

HV_BUSES = ["PCC", "SUB_C", "TAP_W", "TAP_PV", "SUB_A", "SUB_B", "SUB_E", "TAP_B"]

SGEN_BUS = {"WF_1": "WF_1_MV", "WF_2": "WF_2_MV", "WF_3": "WF_3_MV", "PV_1": "PV_ARRAY"}

# Bus each plant regulates against under the "local" droop policy.
DROOP_LOCAL_BUS = {"WF_1": "TAP_W", "WF_2": "TAP_W", "WF_3": "SUB_A", "PV_1": "TAP_PV"}

_TRAFO_KWARGS = inspect.signature(pp.create_transformer_from_parameters).parameters
_HAS_TAP_TYPE = "tap_changer_type" in _TRAFO_KWARGS


def _tap_kwargs(controllable: bool) -> dict:
    """Tap-changer arguments, tolerating both pandapower 2.x and 3.x."""
    kw = dict(C.OLTC_TAP) if controllable else dict(
        tap_side="hv", tap_neutral=0, tap_min=-2, tap_max=2,
        tap_step_percent=2.5, tap_pos=0)
    if _HAS_TAP_TYPE:
        kw["tap_changer_type"] = "Ratio"
    return kw


def build(cfg):
    """Build the network. Returns (net, buses) where `buses` maps name to index."""
    line_par = cfg.line
    net = pp.create_empty_network(f_hz=C.F_HZ, sn_mva=C.S_BASE_MVA)

    hv = ["GRID", "PCC", "TAP_B", "SUB_C", "TAP_W", "TAP_PV", "SUB_A", "SUB_B", "SUB_E",
          "WF_1_HV", "WF_2_HV", "WF_3_HV", "BESS_HV"]
    collector = ["WF_1_MV", "WF_2_MV", "WF_3_MV", "PV_SS", "PV_ARRAY", "BESS_MV"]
    mv = ["SUB_A_MV", "SUB_B_MV"]
    b = {n: pp.create_bus(net, vn_kv=C.V_HV_KV, name=n) for n in hv}
    b.update({n: pp.create_bus(net, vn_kv=C.V_COLLECTOR_KV, name=n) for n in collector})
    b.update({n: pp.create_bus(net, vn_kv=C.V_MV_KV, name=n) for n in mv})

    # ── External grid behind its Thevenin impedance ──────────────────────────
    pp.create_ext_grid(net, bus=b["GRID"], vm_pu=1.0, va_degree=0.0, name="GRID")
    z_base = C.V_HV_KV ** 2 / C.S_BASE_MVA
    pp.create_impedance(net, from_bus=b["GRID"], to_bus=b["PCC"],
                        rft_pu=C.SRC_R_OHM / z_base, xft_pu=C.SRC_X_OHM / z_base,
                        sn_mva=C.S_BASE_MVA, name="Z_GRID")

    def line(frm, to, km, par, name, parallel=1):
        pp.create_line_from_parameters(
            net, from_bus=b[frm], to_bus=b[to], length_km=km, parallel=parallel,
            name=name, in_service=True, type=par.get("type", "ol"),
            **{k: v for k, v in par.items() if k != "type"})

    corr = {k: line_par[k] for k in ("r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km", "max_i_ka")}
    line("SUB_A", "TAP_PV", C.LEN_CORR_A_PV, corr, "CORR_A_PV")
    line("TAP_PV", "TAP_W", C.LEN_CORR_PV_W, corr, "CORR_PV_W")
    line("TAP_W", "SUB_C", C.LEN_CORR_W_C, corr, "CORR_W_C")
    line("SUB_C", "TAP_B", C.LEN_CORR_C_B, corr, "CORR_C_B")
    line("TAP_B", "PCC", C.LEN_CORR_B_PCC, corr, "CORR_B_PCC")
    line("PCC", "SUB_E", C.LEN_LINK_PCC_E, corr, "LINK_PCC_E")
    line("SUB_B", "SUB_E", C.LEN_SPUR_B_E, corr, "SPUR_B_E")

    line("WF_1_HV", "WF_2_HV", C.LEN_LAT_WF1_WF2, C.LATERAL_OHL, "LAT_WF1_WF2")
    line("WF_2_HV", "TAP_W", C.LEN_LAT_WF2_TAP, C.LATERAL_OHL, "LAT_WF2_TAP")
    line("WF_3_HV", "SUB_A", C.LEN_LAT_WF3_A, C.LATERAL_OHL, "LAT_WF3_A")
    line("PV_SS", "PV_ARRAY", C.LEN_CAB_PV, C.COLLECTOR_CABLE, "CAB_PV",
         parallel=C.COLLECTOR_CABLE_PARALLEL)

    # ── Distribution transformers with on-load tap changers ──────────────────
    for name, (sn, vk, vkr) in C.DSO_TRAFOS.items():
        lv = "SUB_A_MV" if name.startswith("T_SUB_A") else "SUB_B_MV"
        pp.create_transformer_from_parameters(
            net, hv_bus=b[lv.replace("_MV", "")], lv_bus=b[lv], sn_mva=sn,
            vn_hv_kv=C.V_HV_KV, vn_lv_kv=C.V_MV_KV, vk_percent=vk, vkr_percent=vkr,
            pfe_kw=0, i0_percent=0, shift_degree=330, name=name,
            **_tap_kwargs(controllable=True))

    # ── Plant step-up transformers ───────────────────────────────────────────
    pp.create_transformer_from_parameters(
        net, hv_bus=b["TAP_PV"], lv_bus=b["PV_SS"], vn_hv_kv=C.V_HV_KV,
        vn_lv_kv=C.V_COLLECTOR_KV, pfe_kw=0, i0_percent=0, shift_degree=-30,
        name="T_PV", **C.PV_TRAFO, **_tap_kwargs(controllable=False))

    # Two parallel units per wind farm. Nameplate impedance is quoted on the
    # natural-cooling base, so it is re-referred to whichever rating is
    # enforced; the bank impedance in ohms is unchanged either way.
    sn_unit = (C.WF_TRAFO_SN_ONAF_MVA if cfg.wf_trafo_rating == "onaf"
               else C.WF_TRAFO_SN_ONAN_MVA)
    z_scale = sn_unit / C.WF_TRAFO_SN_ONAN_MVA
    for base, hv_bus, lv_bus in zip(WF_TRAFO_BASES,
                                    ["WF_1_HV", "WF_2_HV", "WF_3_HV"],
                                    ["WF_1_MV", "WF_2_MV", "WF_3_MV"], strict=True):
        for k in range(1, cfg.wf_trafo_units + 1):
            pp.create_transformer_from_parameters(
                net, hv_bus=b[hv_bus], lv_bus=b[lv_bus], sn_mva=sn_unit,
                vn_hv_kv=C.V_HV_KV, vn_lv_kv=C.V_COLLECTOR_KV,
                vk_percent=C.WF_TRAFO_VK_PCT * z_scale,
                vkr_percent=C.WF_TRAFO_VKR_PCT * z_scale,
                pfe_kw=0, i0_percent=0, shift_degree=-150, name=f"{base}_{k}",
                **_tap_kwargs(controllable=False))

    # ── Storage connection ───────────────────────────────────────────────────
    if cfg.storage_connection == "tie":
        line("TAP_B", "BESS_HV", C.LEN_LAT_BESS, corr, "LAT_BESS")
        pp.create_transformer_from_parameters(
            net, hv_bus=b["BESS_HV"], lv_bus=b["BESS_MV"], vn_hv_kv=C.V_HV_KV,
            vn_lv_kv=C.V_COLLECTOR_KV, pfe_kw=0, i0_percent=0, shift_degree=0,
            name="T_BESS", **C.BESS_TRAFO, **_tap_kwargs(controllable=False))
        storage_bus, storage_pcc = b["BESS_MV"], b["BESS_HV"]
    else:
        storage_bus = storage_pcc = b["TAP_B"]

    # ── Loads ────────────────────────────────────────────────────────────────
    pp.create_load(net, bus=b["SUB_A_MV"], p_mw=0, q_mvar=0, name="LOAD_SUB_A")
    pp.create_load(net, bus=b["SUB_B_MV"], p_mw=0, q_mvar=0, name="LOAD_SUB_B")
    # Downstream demand metered at the PCC busbar; it reduces net export but
    # not corridor current, because it sits beyond the constrained section.
    pp.create_load(net, bus=b["PCC"], p_mw=0, q_mvar=0, name="LOAD_AGG")

    # ── Shunt compensation ───────────────────────────────────────────────────
    for name, bus in [("RX_SUB_A", "SUB_A_MV"), ("RX_SUB_B", "SUB_B_MV")]:
        pp.create_shunt(net, bus=b[bus], p_mw=0,
                        q_mvar=C.REACTOR_STEPS_MVAR[C.REACTOR_STEP_INIT],
                        vn_kv=C.V_MV_KV, step=1, max_step=1, name=name)
    pp.create_shunt(net, bus=b["SUB_A_MV"], p_mw=0, q_mvar=0.0, vn_kv=C.V_MV_KV,
                    step=1, max_step=1, name="CAP_SUB_A")

    # ── Generation and storage ───────────────────────────────────────────────
    for name, bus in SGEN_BUS.items():
        pp.create_sgen(net, bus=b[bus], p_mw=0, q_mvar=0,
                       sn_mva=C.DER_RATING_MW[name] / C.COS_PHI, name=name)
    pp.create_storage(net, bus=storage_bus, p_mw=0.0, q_mvar=0.0,
                      max_e_mwh=cfg.storage_e_mwh, min_e_mwh=0.0,
                      soc_percent=100.0 * cfg.storage_soc_init,
                      sn_mva=math.hypot(cfg.storage_p_mw, cfg.q_limit_storage),
                      name=C.BESS_NAME, in_service=True)

    b["STORAGE_PCC"] = storage_pcc
    return net, b


def summary(cfg, net) -> str:
    """One-line description of what was built."""
    return (f"{len(net.bus)} buses, {len(net.line)} lines, {len(net.trafo)} transformers, "
            f"{len(net.sgen)} plants | conductor={cfg.conductor} "
            f"({cfg.bundle_n}x, static {cfg.static_rating_a:.0f} A) | "
            f"DER {cfg.n_der}/4 = {cfg.der_fleet_mw:.0f} MW | "
            f"storage {'on' if cfg.storage_enabled else 'off'}")
