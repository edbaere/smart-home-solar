"""Tests for the MQTT discovery + state builders (pure — no broker, no paho)."""

from smart_home.mqtt import SENSORS, discovery_configs, state_payload


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
