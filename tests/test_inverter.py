"""Tests for the WLAN-6607 inverter reader's pure parts (no library, no hardware).

The ``huawei-solar`` dependency is imported lazily inside ``read()``, so importing this
module and exercising ``from_values``/``InverterReading`` needs neither the lib nor the
inverter.
"""

from smart_home.inverter import DEFAULT_HOST, DEFAULT_PORT, InverterReading, from_values

# A snapshot like the live read we captured (SUN2000-4.6KTL-L1).
SAMPLE = {
    "model_name": "SUN2000-4.6KTL-L1",
    "device_status": "On-grid",
    "input_power": 3167,
    "active_power": 3098,
    "grid_frequency": 50.01,
    "internal_temperature": 49.2,
    "active_power_control_mode": "Unlimited",
    "active_power_percentage_derating": 100.0,
    "active_power_fixed_value_derating": 50,
    "p_max": 5000,
    "default_active_power_change_gradient": 0.277,
}


def test_defaults_point_at_wlan_ap():
    assert DEFAULT_HOST == "192.168.200.1"
    assert DEFAULT_PORT == 6607


def test_from_values_maps_fields():
    r = from_values(SAMPLE)
    assert r.model_name == "SUN2000-4.6KTL-L1"
    assert r.active_power_w == 3098          # PV production
    assert r.input_power_w == 3167
    assert r.control_mode == "Unlimited"
    assert r.percentage_derating == 100.0
    assert r.p_max_w == 5000
    assert r.power_change_gradient_pct_s == 0.277


def test_from_values_keeps_raw_and_tolerates_missing():
    r = from_values({"active_power": 1000})
    assert r.active_power_w == 1000
    assert r.model_name is None              # missing -> None
    assert r.raw == {"active_power": 1000}


def test_reading_is_frozen():
    r = from_values(SAMPLE)
    import dataclasses
    try:
        r.active_power_w = 0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("InverterReading should be frozen")
