"""Publish readings + curtailment state to MQTT with Home Assistant auto-discovery.

The controller calls this each cycle (it's the sole inverter Modbus client, so publishing
must happen from there — not a separate process competing for the connection). HA picks up
the retained discovery messages and auto-creates the entities; state goes to one shared JSON
topic referenced by each sensor's ``value_template``.

The payload/discovery builders are pure (testable offline). ``paho-mqtt`` is imported lazily
inside :class:`Publisher`; install with ``pip install '.[mqtt]'``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# key, friendly name, HA unit, device_class, state_class  (None where N/A)
SENSORS: list[tuple[str, str, str | None, str | None, str | None]] = [
    ("pv_power",     "PV production",        "W",   "power",  "measurement"),
    ("grid_power",   "Grid net power",       "W",   "power",  "measurement"),
    ("load_power",   "Load",                 "W",   "power",  "measurement"),
    ("l1_power",     "Grid L1",              "W",   "power",  "measurement"),
    ("l2_power",     "Grid L2",              "W",   "power",  "measurement"),
    ("l3_power",     "Grid L3",              "W",   "power",  "measurement"),
    ("import_total", "Grid import total",    "kWh", "energy", "total_increasing"),
    ("export_total", "Grid export total",    "kWh", "energy", "total_increasing"),
    ("derating",        "Active power derating", "%", None,   "measurement"),
    ("target_derating", "Target derating",      "%",  None,   "measurement"),
    ("belpex",       "Day-ahead price",      "EUR/MWh", None, "measurement"),
    ("action",       "Curtailment action",   None,  None,     None),
]


def _device(node_id: str) -> dict[str, Any]:
    return {
        "identifiers": [f"smart_home_{node_id}"],
        "name": "Smart Home Curtailment",
        "manufacturer": "smart_home",
        "model": "Huawei SUN2000 + HomeWizard P1",
    }


def discovery_configs(
    node_id: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return {config_topic: payload} HA-discovery messages (publish retained)."""
    device = _device(node_id)
    out: dict[str, dict[str, Any]] = {}
    for key, name, unit, dev_class, state_class in SENSORS:
        cfg: dict[str, Any] = {
            "name": name,
            "unique_id": f"smart_home_{node_id}_{key}",
            "object_id": f"solar_{key}",  # -> predictable entity_id sensor.solar_<key>
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": device,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dev_class:
            cfg["device_class"] = dev_class
        if state_class:
            cfg["state_class"] = state_class
        out[f"{discovery_prefix}/sensor/smart_home_{node_id}/{key}/config"] = cfg
    return out


def state_payload(
    *,
    action: str,
    derating_pct: float | None,
    target_derating_pct: float | None = None,
    pv_power_w: float,
    grid_net_w: float,
    l1_w: float | None = None,
    l2_w: float | None = None,
    l3_w: float | None = None,
    import_total_kwh: float | None = None,
    export_total_kwh: float | None = None,
    belpex: float | None = None,
) -> dict[str, Any]:
    """Build the shared JSON state. load = pv + grid_net (grid_net + = import)."""
    return {
        "pv_power": round(pv_power_w),
        "grid_power": round(grid_net_w),
        "load_power": round(pv_power_w + grid_net_w),
        "l1_power": None if l1_w is None else round(l1_w),
        "l2_power": None if l2_w is None else round(l2_w),
        "l3_power": None if l3_w is None else round(l3_w),
        "import_total": import_total_kwh,
        "export_total": export_total_kwh,
        "derating": derating_pct,
        "target_derating": target_derating_pct,
        "belpex": belpex,
        "action": action,
    }


# --- day-ahead plan / forecast --------------------------------------------
#
# The streamed ``state`` topic only ever carries the *current* slot, so HA history can never
# show the future part of the curve. We additionally publish the whole cached plan to a retained
# ``plan`` topic as a single forecast sensor: its state is the slot count, and the full
# per-slot array (time, price, decided action) rides along as attributes for a chart card to
# read. We colour by the *decided action* — not a price cutoff — because the thresholds are
# derived from the tariff card (and differ night vs day), so the action is the source of truth.

def plan_discovery_config(
    node_id: str, plan_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the forecast sensor."""
    cfg = {
        "name": "Day-ahead forecast",
        "unique_id": f"smart_home_{node_id}_forecast",
        "object_id": "solar_forecast",  # -> sensor.solar_forecast
        "state_topic": plan_topic,
        "value_template": "{{ value_json.slot_count }}",
        "json_attributes_topic": plan_topic,
        "icon": "mdi:chart-timeline-variant",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/sensor/smart_home_{node_id}/forecast/config": cfg}


def plan_payload(slots: list[Any]) -> dict[str, Any]:
    """Build the retained forecast payload from the cached plan's slots.

    ``points`` is a list of ``{t, p, a}`` (epoch ms, BELPEX EUR/MWh, action string) — a chart
    card can split it into one series per action for colour-by-action without re-deriving
    thresholds. Slots carry an ISO-8601 ``start`` with a Brussels offset.
    """
    points = [
        {
            "t": int(datetime.fromisoformat(s.start).timestamp() * 1000),
            "p": round(s.belpex, 2),
            "a": s.action.value,
        }
        for s in slots
    ]
    return {
        "slot_count": len(slots),
        "covers_start": slots[0].start if slots else None,
        "covers_end": slots[-1].start if slots else None,
        "points": points,
    }


# --- curtailment enable switch --------------------------------------------
#
# A HA switch that gates whether the controller actually writes curtailment to the inverter.
# OFF (default, safe): the plan + decisions are still computed and published, but nothing is
# written — the inverter runs at full power. ON: the planned derating is executed.

def switch_discovery_config(
    node_id: str, command_topic: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the curtailment switch."""
    cfg = {
        "name": "Curtailment control",
        "unique_id": f"smart_home_{node_id}_curtail_enable",
        "object_id": "solar_curtail_enable",  # -> switch.solar_curtail_enable (fresh installs)
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:transmission-tower-export",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/switch/smart_home_{node_id}/curtail_enable/config": cfg}


class Publisher:
    """Thin MQTT wrapper: connect, publish HA discovery once, publish state per cycle."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        node_id: str = "solarpi",
        discovery_prefix: str = "homeassistant",
    ):
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._node_id = node_id
        self._discovery_prefix = discovery_prefix
        self._state_topic = f"smart_home/{node_id}/state"
        self._plan_topic = f"smart_home/{node_id}/plan"
        self._switch_state_topic = f"smart_home/{node_id}/curtail/state"
        self._switch_cmd_topic = f"smart_home/{node_id}/curtail/set"
        self._on_curtail_command = None
        self._client = None

    def connect(self, on_curtail_command=None) -> None:
        """Connect, publish HA discovery. If ``on_curtail_command`` is given, also expose the
        curtailment switch and call it (with a bool) whenever HA toggles the switch."""
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        self._on_curtail_command = on_curtail_command
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smart_home-{self._node_id}")
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_message = self._handle_message
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()
        for topic, payload in discovery_configs(self._node_id, self._state_topic, self._discovery_prefix).items():
            self._client.publish(topic, json.dumps(payload), retain=True)
        for topic, payload in plan_discovery_config(self._node_id, self._plan_topic, self._discovery_prefix).items():
            self._client.publish(topic, json.dumps(payload), retain=True)
        if on_curtail_command is not None:
            for topic, payload in switch_discovery_config(
                self._node_id, self._switch_cmd_topic, self._switch_state_topic, self._discovery_prefix
            ).items():
                self._client.publish(topic, json.dumps(payload), retain=True)
            self._client.subscribe(self._switch_cmd_topic)

    def _handle_message(self, client, userdata, msg) -> None:
        if msg.topic == self._switch_cmd_topic and self._on_curtail_command is not None:
            payload = msg.payload.decode(errors="ignore").strip().upper()
            if payload in ("ON", "OFF"):
                self._on_curtail_command(payload == "ON")

    def publish_state(self, payload: dict[str, Any]) -> None:
        if self._client is not None:
            self._client.publish(self._state_topic, json.dumps(payload), retain=True)

    def publish_plan(self, payload: dict[str, Any]) -> None:
        """Publish the full day-ahead plan (retained) for the forecast chart."""
        if self._client is not None:
            self._client.publish(self._plan_topic, json.dumps(payload), retain=True)

    def publish_switch_state(self, enabled: bool) -> None:
        """Reflect the curtailment switch state back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._switch_state_topic, "ON" if enabled else "OFF", retain=True)

    def close(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
