"""Tests for the derating setpoint logic (pure)."""

import pytest

from smart_home.control import (
    DEFAULT_MARGIN_W,
    Setpoint,
    compute_setpoint,
    injection_limit_percent,
)
from smart_home.economics import Action

PMAX = 5000.0


def _sp(action, prod, net, margin=DEFAULT_MARGIN_W):
    return compute_setpoint(
        action, inverter_active_power_w=prod, p1_net_w=net, p_max_w=PMAX, margin_w=margin
    )


def test_normal_is_unlimited():
    s = _sp(Action.NORMAL, 3000, -500)
    assert s.derating_percent == 100.0
    assert s.target_w is None


def test_full_curtail_is_zero():
    s = _sp(Action.FULL_CURTAIL, 3000, -500)
    assert s.derating_percent == 0.0
    assert s.target_w == 0.0


def test_zero_export_caps_at_load_plus_margin_when_importing():
    # producing 2000, net +588 import -> load 2588, +200 margin -> 2788 W -> 55.8%
    s = _sp(Action.ZERO_EXPORT, 2000, 588, margin=200)
    assert s.target_w == 2788.0
    assert s.derating_percent == pytest.approx(55.8, abs=0.05)


def test_zero_export_caps_at_load_plus_margin_when_exporting():
    # producing 3000, net -500 export -> load 2500, +200 -> 2700 W -> 54.0%
    s = _sp(Action.ZERO_EXPORT, 3000, -500, margin=200)
    assert s.target_w == 2700.0
    assert s.derating_percent == pytest.approx(54.0, abs=0.05)


def test_margin_biases_toward_overproduction():
    # higher margin -> higher cap (more headroom against importing)
    low = _sp(Action.ZERO_EXPORT, 2000, 0, margin=100)
    high = _sp(Action.ZERO_EXPORT, 2000, 0, margin=500)
    assert high.target_w > low.target_w


def test_zero_export_clamps_to_100_when_load_exceeds_rated():
    s = _sp(Action.ZERO_EXPORT, 5000, 1000, margin=200)  # load 6000 > P_MAX
    assert s.derating_percent == 100.0


def test_zero_export_clamps_to_zero_when_target_negative():
    # pathological: production already below a large negative net -> never below 0
    s = _sp(Action.ZERO_EXPORT, 0, -1000, margin=0)  # load -1000 -> target 0
    assert s.target_w == 0.0
    assert s.derating_percent == 0.0


# --- injection (export) limit ---------------------------------------------

def _inj(target, prod, net):
    return injection_limit_percent(
        target, inverter_active_power_w=prod, p1_net_w=net, p_max_w=PMAX
    )


def test_injection_limit_caps_export_at_target():
    # prod 4000, net -3000 (export 3000) -> load 1000. Hold export at 1000:
    # inverter should produce load + 1000 = 2000 -> 40% of 5000.
    assert _inj(1000, 4000, -3000) == 40.0


def test_injection_limit_zero_target_matches_zero_export():
    # target 0 -> inverter = load -> net 0. load = 4000 + (-3000) = 1000 -> 20%.
    assert _inj(0, 4000, -3000) == 20.0


def test_injection_limit_clamps_to_100():
    # load already high, big target -> would exceed rated power -> capped at 100%.
    assert _inj(2000, 4900, 0) == 100.0
