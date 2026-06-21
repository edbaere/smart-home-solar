"""Tests for the price adapter (pure transforms only — no network)."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from smart_home.economics import Action
from smart_home.prices import (
    BRUSSELS,
    RawPrice,
    build_schedule,
    default_is_night,
    to_raw_prices,
)

UTC = ZoneInfo("UTC")


# --- raw price extraction -------------------------------------------------

def test_market_price_kwh_converted_to_belpex_mwh():
    # 0.05 EUR/kWh wholesale == 50 EUR/MWh BELPEX
    p = SimpleNamespace(date_from=datetime(2026, 6, 21, 11, tzinfo=UTC), market_price=0.05)
    raw = to_raw_prices([p])
    assert raw[0].belpex_eur_mwh == 50.0


def test_naive_timestamp_is_treated_as_utc():
    p = SimpleNamespace(date_from=datetime(2026, 6, 21, 11), market_price=0.05)
    raw = to_raw_prices([p])
    assert raw[0].start.tzinfo is not None


def test_start_local_converts_to_brussels():
    # 11:00 UTC -> 13:00 in Brussels (CEST, summer)
    rp = RawPrice(start=datetime(2026, 6, 21, 11, tzinfo=UTC), belpex_eur_mwh=50.0)
    assert rp.start_local.hour == 13
    assert rp.start_local.tzinfo == BRUSSELS


# --- night detection ------------------------------------------------------

def test_default_is_night():
    assert default_is_night(datetime(2026, 6, 20, 12))      # Saturday -> night
    assert not default_is_night(datetime(2026, 6, 22, 12))  # Monday noon -> day
    assert default_is_night(datetime(2026, 6, 22, 23))      # Monday 23:00 -> night
    assert default_is_night(datetime(2026, 6, 22, 6))       # Monday 06:00 -> night
    assert not default_is_night(datetime(2026, 6, 22, 7))   # Monday 07:00 -> day


# --- schedule building ----------------------------------------------------

def test_build_schedule_maps_prices_to_actions():
    prices = [
        RawPrice(start=datetime(2026, 6, 21, 9, tzinfo=UTC), belpex_eur_mwh=90.0),   # NORMAL
        RawPrice(start=datetime(2026, 6, 21, 11, tzinfo=UTC), belpex_eur_mwh=5.0),   # ZERO_EXPORT
        RawPrice(start=datetime(2026, 6, 21, 13, tzinfo=UTC), belpex_eur_mwh=-200),  # FULL_CURTAIL
    ]
    schedule = build_schedule(prices)
    assert [s.action for s in schedule] == [
        Action.NORMAL,
        Action.ZERO_EXPORT,
        Action.FULL_CURTAIL,
    ]


def test_build_schedule_sorts_by_start():
    prices = [
        RawPrice(start=datetime(2026, 6, 21, 13, tzinfo=UTC), belpex_eur_mwh=50.0),
        RawPrice(start=datetime(2026, 6, 21, 9, tzinfo=UTC), belpex_eur_mwh=50.0),
    ]
    schedule = build_schedule(prices)
    assert schedule[0].start < schedule[1].start
