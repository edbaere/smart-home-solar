"""Tests for the HomeWizard P1 parser (no network)."""

import pytest

from smart_home.p1 import parse_p1

# Representative v1 /api/v1/data payload, exporting 2 kW.
EXPORTING = {
    "wifi_ssid": "home",
    "active_power_w": -2000.0,
    "active_power_l1_w": -2000.0,
    "total_power_import_kwh": 1234.567,
    "total_power_export_kwh": 890.123,
}

# Older firmware: split per-tariff totals, importing.
SPLIT_TARIFF = {
    "active_power_w": 350.0,
    "total_power_import_t1_kwh": 100.0,
    "total_power_import_t2_kwh": 50.0,
    "total_power_export_t1_kwh": 5.0,
    "total_power_export_t2_kwh": 2.0,
}


def test_negative_power_is_exporting():
    r = parse_p1(EXPORTING)
    assert r.is_exporting
    assert r.exporting_w == 2000.0
    assert r.importing_w == 0.0


def test_positive_power_is_importing():
    r = parse_p1(SPLIT_TARIFF)
    assert not r.is_exporting
    assert r.importing_w == 350.0
    assert r.exporting_w == 0.0


def test_split_tariff_totals_are_summed():
    r = parse_p1(SPLIT_TARIFF)
    assert r.total_import_kwh == 150.0
    assert r.total_export_kwh == 7.0


def test_single_summed_totals_pass_through():
    r = parse_p1(EXPORTING)
    assert r.total_import_kwh == 1234.567
    assert r.total_export_kwh == 890.123


def test_missing_active_power_raises():
    with pytest.raises(ValueError, match="active_power_w"):
        parse_p1({"wifi_ssid": "home"})
