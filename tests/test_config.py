"""Configuration, presets and validation."""
from __future__ import annotations

import pytest

from corridor_sim import constants as C
from corridor_sim.config import DER_NAMES, PRESETS, build_config, normalise_der


def test_every_preset_is_valid():
    for name in PRESETS:
        cfg = build_config(name)
        assert cfg.n_der == len(cfg.active_der)


def test_matrix_covers_every_rating_method_and_build():
    modes = {(build_config(p).conductor, build_config(p).dlr_mode)
             for p in PRESETS if p != "baseline"}
    assert ("single", 0) in modes and ("single", 1) in modes
    assert ("single", 2) in modes and ("twin", 0) in modes


def test_der_list_is_expanded_to_a_full_map():
    mapping = normalise_der(["WF_1", "PV_1"])
    assert set(mapping) == set(DER_NAMES)
    assert mapping["WF_1"] and mapping["PV_1"]
    assert not mapping["WF_2"] and not mapping["WF_3"]


def test_unknown_plant_name_is_rejected():
    with pytest.raises(ValueError, match="unknown DER"):
        normalise_der(["WF_9"])


@pytest.mark.parametrize("override", [
    {"conductor": "triple"},
    {"control_mode": "pid"},
    {"dlr_mode": 3},
    {"storage_soc_init": 1.5},
    {"wf_trafo_units": 3},
    {"roughness_m": 40.0},
    {"export_cap_basis": "guess"},
])
def test_bad_settings_fail_fast(override):
    with pytest.raises(AssertionError):
        build_config(**override)


def test_derived_quantities():
    cfg = build_config("dlr2_der4_bess", days=10)
    assert cfg.der_fleet_mw == pytest.approx(C.FLEET_MW)
    assert cfg.n_steps == 10 * 96
    assert cfg.static_rating_a == 800.0
    assert cfg.bundle_n == 1
    assert cfg.storage_droop_active


def test_twin_conductor_doubles_the_rating():
    assert build_config(conductor="twin").static_rating_a == pytest.approx(
        2 * build_config(conductor="single").static_rating_a)


def test_recharge_target_covers_the_reserve_obligation():
    cfg = build_config("dlr2_der4_bess")
    deliverable = (cfg.soc_recharge_target_mwh - cfg.soc_min_mwh) * C.BESS_ETA_DIS
    assert deliverable == pytest.approx(cfg.storage_contract_mw * 1.0, rel=1e-6)
