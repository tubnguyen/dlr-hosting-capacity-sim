"""IEEE 738 thermal model."""
from __future__ import annotations

import math

import pytest

from corridor_sim import constants as C
from corridor_sim import dlr
from corridor_sim.config import build_config

REF = C.STATIC_REF_CONDITIONS


def test_model_reproduces_the_static_rating():
    """Mode 0 and modes 1-2 must be the same physics under different weather."""
    for conductor in C.CONDUCTOR_OPTIONS:
        cfg = build_config(conductor=conductor)
        check = dlr.calibration(cfg)
        assert abs(check["deviation_pct"]) < 0.5, (conductor, check)


def test_ampacity_rises_with_wind():
    previous = 0.0
    for wind in (0.5, 1.0, 2.0, 5.0, 10.0):
        current, _ = dlr.ampacity(10.0, wind, 90.0, 0.0)
        assert current > previous
        previous = current


def test_ampacity_falls_with_air_temperature_and_sun():
    warm, _ = dlr.ampacity(30.0, 2.0, 90.0, 0.0)
    cold, _ = dlr.ampacity(-10.0, 2.0, 90.0, 0.0)
    assert cold > warm

    sunny, _ = dlr.ampacity(20.0, 2.0, 90.0, 1000.0)
    shaded, _ = dlr.ampacity(20.0, 2.0, 90.0, 0.0)
    assert shaded > sunny


def test_perpendicular_wind_cools_best():
    parallel, _ = dlr.ampacity(20.0, 5.0, 0.0, 0.0)
    perpendicular, _ = dlr.ampacity(20.0, 5.0, 90.0, 0.0)
    assert perpendicular > parallel


@pytest.mark.parametrize("phi,expected", [(0, 0), (90, 90), (180, 0), (270, 90),
                                          (135, 45), (-30, 30)])
def test_attack_angle_folds_onto_the_first_quadrant(phi, expected):
    assert dlr._normalise_attack_angle(phi) == pytest.approx(expected)


def test_forward_and_inverse_solves_agree():
    """The current a rating allows must heat the conductor to exactly the
    design temperature the rating was computed at."""
    for t_air, wind, phi, ghi in [(10.0, 2.0, 90.0, 0.0), (25.0, 0.6, 45.0, 800.0),
                                  (-15.0, 8.0, 70.0, 0.0)]:
        current, _ = dlr.ampacity(t_air, wind, phi, ghi, t_cond_max_c=C.T_COND_MAX_C)
        reached = dlr.conductor_temperature(current, t_air, wind, phi, ghi)
        assert reached == pytest.approx(C.T_COND_MAX_C, abs=0.2)


def test_conductor_temperature_never_below_ambient():
    assert dlr.conductor_temperature(0.0, 12.0, 3.0, 90.0, 0.0) == pytest.approx(12.0)


def test_adverse_weather_can_derate_below_static():
    """Hot, sunny, still air with the wind along the line is a real derating."""
    cfg = build_config(conductor="single")
    current, _ = dlr.bundle_ampacity(cfg, 35.0, 0.4, 0.0, 1000.0)
    assert current < cfg.static_rating_a


def test_bundle_scales_with_conductor_count():
    single = build_config(conductor="single")
    twin = build_config(conductor="twin")
    a, _ = dlr.bundle_ampacity(single, 5.0, 3.0, 90.0, 100.0)
    b, _ = dlr.bundle_ampacity(twin, 5.0, 3.0, 90.0, 100.0)
    assert b == pytest.approx(2.0 * a)


def test_log_law_reduces_speed_toward_the_ground():
    at_conductor = dlr.log_law(10.0, 100.0, 15.0, 0.30, 0.0)
    assert 0.0 < at_conductor < 10.0
    assert dlr.log_law(10.0, 100.0, 100.0, 0.30, 0.0) == pytest.approx(10.0)


def test_log_law_rejects_an_invalid_geometry():
    with pytest.raises(ValueError):
        dlr.log_law(10.0, 100.0, 15.0, roughness_m=20.0)


def test_static_mode_ignores_weather():
    cfg = build_config(conductor="single", dlr_mode=0)
    limits, diag, source = dlr.operative_limits(cfg, None, None)
    assert source == "static"
    assert all(v == cfg.static_rating_a for v in limits.values())
    assert all(math.isnan(diag[z]["qc_wm"]) for z in limits)
