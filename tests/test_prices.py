"""Tests for the ENTSO-E price adapter (pure transforms only — no network)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from smart_home.economics import Action
from smart_home.prices import (
    BRUSSELS,
    EntsoeError,
    RawPrice,
    build_schedule,
    default_is_night,
    parse_entsoe_xml,
)

UTC = ZoneInfo("UTC")

# Minimal A44 document: hourly, with position 3 omitted (must repeat position 2's value).
A44_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-06-21T22:00Z</start>
        <end>2026-06-22T02:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>90.00</price.amount></Point>
      <Point><position>2</position><price.amount>5.00</price.amount></Point>
      <Point><position>4</position><price.amount>-200.00</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""

ACK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:8:0">
  <Reason><code>999</code><text>No matching data found</text></Reason>
</Acknowledgement_MarketDocument>"""

PT15_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <TimeSeries><Period>
    <timeInterval><start>2026-06-21T22:00Z</start><end>2026-06-21T23:00Z</end></timeInterval>
    <resolution>PT15M</resolution>
    <Point><position>1</position><price.amount>10.0</price.amount></Point>
    <Point><position>2</position><price.amount>20.0</price.amount></Point>
  </Period></TimeSeries>
</Publication_MarketDocument>"""


# --- parsing --------------------------------------------------------------

def test_parse_prices_are_eur_mwh_no_conversion():
    prices = parse_entsoe_xml(A44_XML)
    assert prices[0].belpex_eur_mwh == 90.0  # taken verbatim, no x1000


def test_parse_timestamps_are_utc_and_hourly():
    prices = parse_entsoe_xml(A44_XML)
    assert prices[0].start == datetime(2026, 6, 21, 22, tzinfo=UTC)
    assert prices[1].start == datetime(2026, 6, 21, 23, tzinfo=UTC)


def test_parse_fills_missing_position_with_previous_value():
    prices = parse_entsoe_xml(A44_XML)
    assert len(prices) == 4
    assert prices[2].belpex_eur_mwh == 5.0     # position 3 omitted -> repeats position 2
    assert prices[3].belpex_eur_mwh == -200.0


def test_parse_pt15m_resolution_steps_15_minutes():
    prices = parse_entsoe_xml(PT15_XML)
    assert (prices[1].start - prices[0].start).total_seconds() == 15 * 60


def test_acknowledgement_raises_with_reason():
    with pytest.raises(EntsoeError, match="No matching data found"):
        parse_entsoe_xml(ACK_XML)


# --- timezone & night -----------------------------------------------------

def test_start_local_converts_to_brussels():
    rp = RawPrice(start=datetime(2026, 6, 21, 11, tzinfo=UTC), belpex_eur_mwh=50.0)
    assert rp.start_local.hour == 13  # CEST = UTC+2 in summer
    assert rp.start_local.tzinfo == BRUSSELS


def test_default_is_night():
    assert default_is_night(datetime(2026, 6, 20, 12))      # Saturday -> night
    assert not default_is_night(datetime(2026, 6, 22, 12))  # Monday noon -> day
    assert default_is_night(datetime(2026, 6, 22, 23))      # Monday 23:00 -> night
    assert not default_is_night(datetime(2026, 6, 22, 7))   # Monday 07:00 -> day


# --- schedule -------------------------------------------------------------

def test_build_schedule_maps_prices_to_actions_and_sorts():
    schedule = build_schedule(parse_entsoe_xml(A44_XML))
    assert [s.action for s in schedule] == [
        Action.NORMAL,        # 90
        Action.ZERO_EXPORT,   # 5
        Action.ZERO_EXPORT,   # 5 (filled)
        Action.FULL_CURTAIL,  # -200
    ]
    assert all(schedule[i].start <= schedule[i + 1].start for i in range(len(schedule) - 1))
