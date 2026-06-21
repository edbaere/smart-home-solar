"""Tests for the pure economic decision engine."""

import math

import pytest

from smart_home.economics import (
    IMEWO,
    Action,
    Slot,
    consume_price,
    consume_zero_belpex,
    decide,
    feedin_price,
    feedin_zero_belpex,
)


# --- price formulas -------------------------------------------------------

def test_feedin_zero_crossing_at_11_5_eur_mwh():
    assert feedin_zero_belpex() == pytest.approx(11.5)
    assert feedin_price(11.5) == pytest.approx(0.0)


def test_feedin_negative_below_threshold():
    assert feedin_price(0.0) == pytest.approx(-1.150)
    assert feedin_price(50.0) == pytest.approx(3.85)


def test_consume_matches_vreg_expected_value():
    # Card's expected energy-only value for Sept 2024 was 11.9839 ct/kWh at ~91.8 EUR/MWh.
    energy_only = (0.1068 * 91.8 + 1.5) * 1.06
    assert energy_only == pytest.approx(11.9839, abs=0.05)


def test_consume_zero_crossing_is_deeply_negative():
    threshold = consume_zero_belpex()
    assert threshold == pytest.approx(-115.6, abs=0.5)
    assert consume_price(threshold) == pytest.approx(0.0, abs=1e-6)


def test_night_grid_rate_lowers_consume_price():
    assert consume_price(50.0, night=True) < consume_price(50.0, night=False)
    # difference equals the grid day/night spread
    diff = consume_price(50.0, night=False) - consume_price(50.0, night=True)
    assert diff == pytest.approx(IMEWO.grid_day - IMEWO.grid_night)


# --- decision logic -------------------------------------------------------

@pytest.mark.parametrize(
    "belpex, expected",
    [
        (90.0, Action.NORMAL),        # typical daytime price -> export surplus
        (11.6, Action.NORMAL),        # just above feed-in threshold
        (11.5, Action.NORMAL),        # exactly zero feed-in is not < 0
        (11.4, Action.ZERO_EXPORT),   # just below -> clip export
        (0.0, Action.ZERO_EXPORT),    # low price, still pay taxes to consume
        (-115.0, Action.ZERO_EXPORT), # consume still costs a hair -> not full curtail
        (-116.0, Action.FULL_CURTAIL),# grid pays us to consume -> kill production
        (-200.0, Action.FULL_CURTAIL),
    ],
)
def test_decide(belpex, expected):
    assert decide(belpex) == expected


def test_full_curtail_takes_precedence_over_zero_export():
    # At deeply negative prices both feed-in and consume are negative; full curtail wins.
    belpex = -200.0
    assert feedin_price(belpex) < 0
    assert consume_price(belpex) < 0
    assert decide(belpex) == Action.FULL_CURTAIL


# --- Slot helper ----------------------------------------------------------

def test_slot_from_belpex_packs_decision_and_prices():
    slot = Slot.from_belpex("2026-06-21T13:00:00+02:00", 5.0)
    assert slot.action == Action.ZERO_EXPORT
    assert slot.feedin_price == pytest.approx(feedin_price(5.0), abs=1e-4)
    assert slot.consume_price == pytest.approx(consume_price(5.0), abs=1e-4)
    assert not slot.night
