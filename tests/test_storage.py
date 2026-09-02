"""Storage dispatch, energy accounting and reserve."""
from __future__ import annotations

import pytest

from corridor_sim import constants as C
from corridor_sim import storage
from corridor_sim.config import build_config


@pytest.fixture
def cfg_storage():
    return build_config("dlr2_der4_bess", days=1)


def test_sign_convention_is_symmetric():
    assert storage.to_grid_frame(10.0) == -10.0      # charging draws from the grid
    assert storage.to_grid_frame(-10.0) == 10.0      # discharging feeds it


def test_round_trip_loses_exactly_the_stated_efficiency(cfg_storage):
    cfg = cfg_storage
    start = 0.5 * cfg.storage_e_mwh
    charged, _, _ = storage.update_soc(cfg, start, 10.0, apply=True)
    energy_in = 10.0 * C.DT_H
    energy_stored = charged - start
    back, _, _ = storage.update_soc(cfg, charged, -(energy_stored / C.DT_H) * C.BESS_ETA_DIS,
                                    apply=True)
    energy_out = (charged - back) / C.BESS_ETA_DIS * C.BESS_ETA_DIS
    delivered = (charged - back) * C.BESS_ETA_DIS
    assert energy_stored == pytest.approx(energy_in * C.BESS_ETA_CH)
    assert delivered / energy_in == pytest.approx(C.BESS_ETA_RT, rel=1e-6)
    assert energy_out > 0


def test_state_of_charge_is_frozen_on_a_failed_step(cfg_storage):
    cfg = cfg_storage
    start = 0.5 * cfg.storage_e_mwh
    after, clamped, realised = storage.update_soc(cfg, start, 25.0, apply=False)
    assert after == start and clamped == 0 and realised == 25.0


def test_clamping_keeps_power_and_energy_consistent(cfg_storage):
    """When a limit binds, realised power must match the actual energy change."""
    cfg = cfg_storage
    near_full = cfg.soc_max_mwh - 0.5
    after, clamped, realised = storage.update_soc(cfg, near_full, cfg.storage_p_mw, apply=True)
    assert clamped == 1
    assert after == pytest.approx(cfg.soc_max_mwh)
    assert realised * C.BESS_ETA_CH * C.DT_H == pytest.approx(after - near_full)
    assert realised < cfg.storage_p_mw

    near_empty = cfg.soc_min_mwh + 0.5
    after, clamped, realised = storage.update_soc(cfg, near_empty, -cfg.storage_p_mw, apply=True)
    assert clamped == 1
    assert after == pytest.approx(cfg.soc_min_mwh)
    assert realised / C.BESS_ETA_DIS * C.DT_H == pytest.approx(after - near_empty)


def test_power_limits_respect_the_energy_left(cfg_storage):
    cfg = cfg_storage
    assert storage.dischargeable_mw(cfg, cfg.soc_min_mwh) == 0.0
    assert storage.chargeable_mw(cfg, cfg.soc_max_mwh) == 0.0
    assert storage.dischargeable_mw(cfg, cfg.soc_max_mwh) == pytest.approx(cfg.storage_p_mw)
    assert storage.chargeable_mw(cfg, cfg.soc_min_mwh) == pytest.approx(cfg.storage_p_mw)


def test_reserve_shortfall_is_reported(cfg_storage):
    cfg = cfg_storage
    full, short = storage.reserve_availability(cfg, cfg.soc_max_mwh, 0.0)
    assert full == pytest.approx(cfg.storage_reserve_mw) and short == 0

    # Delivering energy consumes the headroom the reserve was booked against.
    partial, short = storage.reserve_availability(cfg, cfg.soc_max_mwh, -cfg.storage_p_mw)
    assert partial == 0.0 and short == 1

    empty, short = storage.reserve_availability(cfg, cfg.soc_min_mwh, 0.0)
    assert empty == 0.0 and short == 1


def test_disabled_storage_holds_no_reserve():
    cfg = build_config("dlr2_der4")
    assert storage.reserve_availability(cfg, 30.0, 0.0) == (0.0, 0)
