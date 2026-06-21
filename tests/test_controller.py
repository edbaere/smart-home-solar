"""Tests for the control daemon's pure decision logic (no lib, no hardware)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from smart_home.controller import resolve_action, plan_step, Step
from smart_home.economics import Action, Slot
from smart_home.schedule import Schedule

BRUSSELS = ZoneInfo("Europe/Brussels")
PMAX = 5000.0


def _schedule():
    return Schedule([
        Slot.from_belpex("2026-06-21T10:00:00+02:00", 5.0),    # ZERO_EXPORT
        Slot.from_belpex("2026-06-21T10:15:00+02:00", 90.0),   # NORMAL
    ])


# --- resolve_action -------------------------------------------------------

def test_resolve_action_within_plan():
    s = _schedule()
    assert resolve_action(s, datetime(2026, 6, 21, 10, 5, tzinfo=BRUSSELS)) == Action.ZERO_EXPORT
    assert resolve_action(s, datetime(2026, 6, 21, 10, 20, tzinfo=BRUSSELS)) == Action.NORMAL


def test_resolve_action_failsafe_when_not_covered():
    s = _schedule()
    # before and after the plan -> NORMAL (never stuck curtailed on a stale plan)
    assert resolve_action(s, datetime(2026, 6, 21, 9, 0, tzinfo=BRUSSELS)) == Action.NORMAL
    assert resolve_action(s, datetime(2026, 6, 21, 12, 0, tzinfo=BRUSSELS)) == Action.NORMAL


def test_resolve_action_empty_schedule_is_normal():
    assert resolve_action(Schedule([]), datetime(2026, 6, 21, 10, 5, tzinfo=BRUSSELS)) == Action.NORMAL


# --- plan_step ------------------------------------------------------------

def test_normal_no_write_when_already_full():
    step = plan_step(Action.NORMAL, inverter_active_power_w=3000, p1_net_w=-500,
                     p_max_w=PMAX, current_derating_pct=100.0)
    assert step.target_percent == 100.0
    assert step.should_write is False


def test_normal_writes_when_currently_curtailed():
    step = plan_step(Action.NORMAL, inverter_active_power_w=3000, p1_net_w=-500,
                     p_max_w=PMAX, current_derating_pct=40.0)
    assert step.target_percent == 100.0
    assert step.should_write is True


def test_zero_export_writes_when_far_from_current():
    # load 2588 + 200 -> 2788 W -> 55.8%; current 100 -> write
    step = plan_step(Action.ZERO_EXPORT, inverter_active_power_w=2000, p1_net_w=588,
                     p_max_w=PMAX, current_derating_pct=100.0, margin_w=200)
    assert round(step.target_percent, 1) == 55.8
    assert step.should_write is True


def test_deadband_suppresses_tiny_change():
    # target ~55.8%; current 55.0% -> within 2% deadband -> no write
    step = plan_step(Action.ZERO_EXPORT, inverter_active_power_w=2000, p1_net_w=588,
                     p_max_w=PMAX, current_derating_pct=55.0, margin_w=200, deadband_pct=2.0)
    assert step.should_write is False


def test_full_curtail_writes_to_zero():
    step = plan_step(Action.FULL_CURTAIL, inverter_active_power_w=3000, p1_net_w=-500,
                     p_max_w=PMAX, current_derating_pct=100.0)
    assert step.target_percent == 0.0
    assert step.should_write is True
