"""Shared fixtures: a built network and a solved operating point."""
from __future__ import annotations

import pytest

from corridor_sim import constants as C
from corridor_sim import network as nw
from corridor_sim.config import build_config
from corridor_sim.controls import NetIndex, solve


@pytest.fixture(scope="session")
def cfg():
    return build_config("dlr2_der4", days=1)


@pytest.fixture
def solved(cfg):
    """A moderately loaded, solved network plus its index."""
    net, buses = nw.build(cfg)
    idx = NetIndex.build(net, buses)
    net.load.at[idx.loads["LOAD_SUB_A"], "p_mw"] = 15.0
    net.load.at[idx.loads["LOAD_SUB_A"], "q_mvar"] = 4.0
    net.load.at[idx.loads["LOAD_SUB_B"], "p_mw"] = 7.0
    net.load.at[idx.loads["LOAD_SUB_B"], "q_mvar"] = 2.0
    net.load.at[idx.loads["LOAD_AGG"], "p_mw"] = 25.0
    net.load.at[idx.loads["LOAD_AGG"], "q_mvar"] = 5.0
    for unit, p in (("WF_1", 55.0), ("WF_2", 66.0), ("WF_3", 44.0), ("PV_1", 30.0)):
        net.sgen.at[idx.sgens[unit], "p_mw"] = p
        net.sgen.at[idx.sgens[unit], "q_mvar"] = 0.0
    assert solve(net, "dc", deep=True)
    return net, idx


@pytest.fixture
def static_limits(cfg):
    return dict.fromkeys(nw.CORRIDOR_ZONES, cfg.static_rating_a)


@pytest.fixture
def reactor_state():
    return {"RX_SUB_A": C.REACTOR_STEP_INIT, "RX_SUB_B": C.REACTOR_STEP_INIT}
