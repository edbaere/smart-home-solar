"""Tests for the price-scaled asymmetric curtailment window."""

import pytest

from smart_home.control import derating_for_target_export, window_breach
from smart_home.economics import (
    curtail_window,
    injection_penalty,
    relative_injection_cost,
)


# --- relative injection cost r --------------------------------------------

def test_r_is_zero_at_feedin_breakeven():
    # feedin_price crosses 0 at BELPEX 11.5; just below it, injecting is ~free vs importing
    assert relative_injection_cost(11.0) < 0.01


def test_r_rises_as_price_falls():
    # lower BELPEX -> bigger injection penalty, smaller import cost -> more relatively expensive
    assert (relative_injection_cost(11.0)
            < relative_injection_cost(0.0)
            < relative_injection_cost(-40.0)
            <= relative_injection_cost(-80.0) == 1.0)


def test_r_clamped_to_unit_interval():
    for b in (50.0, 11.0, 0.0, -40.0, -80.0, -200.0):
        assert 0.0 <= relative_injection_cost(b) <= 1.0


def test_injection_penalty_positive_in_zero_export_band():
    assert injection_penalty(0.0) == pytest.approx(1.15, abs=1e-6)   # -(0.1*0 - 1.15)


# --- the window [L, U, Tset] ----------------------------------------------

def test_window_wide_and_high_floor_when_injection_cheap():
    L, U, Tset = curtail_window(11.0)   # r ~ 0
    assert L == pytest.approx(75.0, abs=1.0)
    assert U == pytest.approx(1200.0, abs=15.0)
    assert Tset == pytest.approx(400.0, abs=5.0)


def test_window_collapses_to_zero_when_injection_expensive():
    L, U, Tset = curtail_window(-80.0)  # r = 1
    assert L == pytest.approx(0.0, abs=1e-6)
    assert U == pytest.approx(0.0, abs=1e-6)
    assert Tset == pytest.approx(0.0, abs=1e-6)


def test_ceiling_shrinks_monotonically_as_price_falls():
    us = [curtail_window(b)[1] for b in (0.0, -20.0, -40.0, -60.0, -80.0)]
    assert us == sorted(us, reverse=True)   # strictly decreasing tolerance
    assert us[0] > us[-1]


def test_target_always_inside_window():
    for b in (11.0, 5.0, 0.0, -20.0, -40.0, -60.0):
        L, U, Tset = curtail_window(b)
        assert L <= Tset <= U


def test_k_shape_holds_ceiling_wider_than_linear():
    # k=2 keeps the ceiling wider than linear (k=1) at mid prices ("only when it matters")
    u_k2 = curtail_window(-30.0, k=2.0)[1]
    u_k1 = curtail_window(-30.0, k=1.0)[1]
    assert u_k2 > u_k1


# --- window breach + cap math ---------------------------------------------

def test_window_breach_edges():
    assert window_breach(50.0, 75.0, 1200.0) == "low"     # drifting toward import
    assert window_breach(1500.0, 75.0, 1200.0) == "high"  # over-exporting
    assert window_breach(400.0, 75.0, 1200.0) is None     # inside -> no write
    assert window_breach(-100.0, 0.0, 0.0) == "low"       # importing


def test_derating_for_target_export():
    assert derating_for_target_export(400.0, 1000.0, 5000.0) == pytest.approx(28.0)
    assert derating_for_target_export(0.0, 1000.0, 5000.0) == pytest.approx(20.0)
    assert derating_for_target_export(400.0, 5000.0, 5000.0) == pytest.approx(100.0)  # clamped
