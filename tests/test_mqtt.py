"""Tests for the MQTT discovery + state builders (pure — no broker, no paho)."""

from smart_home.economics import Slot
from smart_home.mqtt import (
    SENSORS,
    discovery_configs,
    manual_number_discovery_config,
    manual_override_discovery_config,
    plan_discovery_config,
    plan_payload,
    state_payload,
    switch_discovery_config,
)


def test_discovery_has_a_config_per_sensor():
    cfgs = discovery_configs("solarpi", "smart_home/solarpi/state")
    assert len(cfgs) == len(SENSORS)
    # topics follow HA discovery convention
    assert "homeassistant/sensor/smart_home_solarpi/pv_power/config" in cfgs
    assert "homeassistant/sensor/smart_home_solarpi/target_derating/config" in cfgs


def test_discovery_payload_shape():
    cfgs = discovery_configs("solarpi", "smart_home/solarpi/state")
    pv = cfgs["homeassistant/sensor/smart_home_solarpi/pv_power/config"]
    assert pv["unique_id"] == "smart_home_solarpi_pv_power"
    assert pv["state_topic"] == "smart_home/solarpi/state"
    assert pv["value_template"] == "{{ value_json.pv_power }}"
    assert pv["unit_of_measurement"] == "W"
    assert pv["device_class"] == "power"
    assert pv["device"]["identifiers"] == ["smart_home_solarpi"]


def test_action_sensor_has_no_unit_or_class():
    cfgs = discovery_configs("solarpi", "s")
    action = cfgs["homeassistant/sensor/smart_home_solarpi/action/config"]
    assert "unit_of_measurement" not in action
    assert "device_class" not in action


def test_state_payload_computes_load_and_rounds():
    p = state_payload(
        action="ZERO_EXPORT", derating_pct=14.7,
        pv_power_w=2870.4, grid_net_w=-2335.2,
        l1_w=-2578, l2_w=12, l3_w=229,
        import_total_kwh=6854.7, export_total_kwh=7598.0, belpex=5.0,
    )
    assert p["pv_power"] == 2870
    assert p["grid_power"] == -2335
    assert p["load_power"] == round(2870.4 - 2335.2)   # pv + grid_net
    assert p["action"] == "ZERO_EXPORT"
    assert p["belpex"] == 5.0


def test_state_payload_includes_target_derating():
    p = state_payload(action="ZERO_EXPORT", derating_pct=100.0, target_derating_pct=29.3,
                      pv_power_w=3000, grid_net_w=-1700)
    assert p["derating"] == 100.0          # actual
    assert p["target_derating"] == 29.3    # what it would set


def test_state_payload_tolerates_missing_phases():
    p = state_payload(action="NORMAL", derating_pct=100.0, pv_power_w=1000, grid_net_w=-50)
    assert p["l1_power"] is None
    assert p["import_total"] is None


# --- forecast / plan ------------------------------------------------------

def _slot(start, belpex):
    return Slot.from_belpex(start, belpex)


def test_plan_discovery_uses_plan_topic_and_json_attributes():
    cfgs = plan_discovery_config("solarpi", "smart_home/solarpi/plan")
    cfg = cfgs["homeassistant/sensor/smart_home_solarpi/forecast/config"]
    assert cfg["object_id"] == "solar_forecast"
    assert cfg["state_topic"] == "smart_home/solarpi/plan"
    assert cfg["json_attributes_topic"] == "smart_home/solarpi/plan"
    assert cfg["value_template"] == "{{ value_json.slot_count }}"


def test_plan_payload_shape_and_epoch_ms():
    slots = [
        _slot("2026-06-27T00:00:00+02:00", 80.0),
        _slot("2026-06-27T00:15:00+02:00", -200.0),
    ]
    p = plan_payload(slots)
    assert p["slot_count"] == 2
    assert p["covers_start"] == "2026-06-27T00:00:00+02:00"
    assert p["covers_end"] == "2026-06-27T00:15:00+02:00"
    # 2026-06-27T00:00:00+02:00 == 2026-06-26T22:00:00Z
    assert p["points"][0]["t"] == 1782511200000
    assert p["points"][1]["t"] - p["points"][0]["t"] == 15 * 60 * 1000
    # each point carries the price and the *decided* action (source of truth for colour)
    assert p["points"][0]["p"] == 80.0
    assert p["points"][0]["a"] == slots[0].action.value
    assert p["points"][1]["a"] == slots[1].action.value


def test_plan_payload_empty():
    p = plan_payload([])
    assert p == {"slot_count": 0, "covers_start": None, "covers_end": None, "points": []}


# --- curtailment switch ---------------------------------------------------

def test_switch_discovery_is_a_switch_with_command_and_state():
    cfgs = switch_discovery_config(
        "solarpi", "smart_home/solarpi/curtail/set", "smart_home/solarpi/curtail/state"
    )
    cfg = cfgs["homeassistant/switch/smart_home_solarpi/curtail_enable/config"]
    assert cfg["command_topic"] == "smart_home/solarpi/curtail/set"
    assert cfg["state_topic"] == "smart_home/solarpi/curtail/state"
    assert cfg["payload_on"] == "ON"
    assert cfg["payload_off"] == "OFF"
    assert cfg["unique_id"] == "smart_home_solarpi_curtail_enable"


# --- manual derating override ---------------------------------------------

def test_manual_override_switch_discovery():
    cfgs = manual_override_discovery_config(
        "solarpi", "smart_home/solarpi/manual/set", "smart_home/solarpi/manual/state"
    )
    cfg = cfgs["homeassistant/switch/smart_home_solarpi/manual_override/config"]
    assert cfg["command_topic"] == "smart_home/solarpi/manual/set"
    assert cfg["unique_id"] == "smart_home_solarpi_manual_override"
    assert cfg["payload_on"] == "ON"


def test_manual_number_discovery_is_a_percent_number():
    cfgs = manual_number_discovery_config(
        "solarpi", "smart_home/solarpi/manual_pct/set", "smart_home/solarpi/manual_pct/state"
    )
    cfg = cfgs["homeassistant/number/smart_home_solarpi/manual_derating/config"]
    assert cfg["min"] == 0
    assert cfg["max"] == 100
    assert cfg["unit_of_measurement"] == "%"
    assert cfg["command_topic"] == "smart_home/solarpi/manual_pct/set"
