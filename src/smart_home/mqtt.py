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
    ("pv_yield_total", "PV yield total",     "kWh", "energy", "total_increasing"),
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
    # --- monitoring (write-minimisation + policy behaviour) ---
    ("export_power",  "Grid export",             "W",  "power", "measurement"),
    ("writes_today",  "Inverter adjustments today", "writes", None, "measurement"),
    ("writes_total",  "Inverter adjustments (lifetime)", "writes", None, "total_increasing"),
    ("window_low",    "Export band floor",       "W",  "power", "measurement"),
    ("window_high",   "Export band ceiling",     "W",  "power", "measurement"),
    ("window_target", "Export target",           "W",  "power", "measurement"),
    ("inj_cost_ratio", "Injection cost ratio",   None, None,    "measurement"),
]


def _r(v: float | None) -> float | None:
    """Round a value for publishing, preserving None (so HA shows a gap, not 0)."""
    return None if v is None else round(v)


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
    pv_yield_total_kwh: float | None = None,
    grid_net_w: float,
    l1_w: float | None = None,
    l2_w: float | None = None,
    l3_w: float | None = None,
    import_total_kwh: float | None = None,
    export_total_kwh: float | None = None,
    belpex: float | None = None,
    writes_today: int | None = None,
    writes_total: int | None = None,
    window_low: float | None = None,
    window_high: float | None = None,
    window_target: float | None = None,
    inj_cost_ratio: float | None = None,
) -> dict[str, Any]:
    """Build the shared JSON state. load = pv + grid_net (grid_net + = import).

    ``export_power`` is grid export (positive when exporting = −grid_net), published so the
    "window tracking" chart can show export against the [floor, ceiling] band on one axis. The
    window_* fields are only set during ZERO_EXPORT (None otherwise → HA shows a gap)."""
    return {
        "pv_power": round(pv_power_w),
        "pv_yield_total": pv_yield_total_kwh,
        "grid_power": round(grid_net_w),
        "load_power": round(pv_power_w + grid_net_w),
        "export_power": round(-grid_net_w),
        "l1_power": None if l1_w is None else round(l1_w),
        "l2_power": None if l2_w is None else round(l2_w),
        "l3_power": None if l3_w is None else round(l3_w),
        "import_total": import_total_kwh,
        "export_total": export_total_kwh,
        "derating": derating_pct,
        "target_derating": target_derating_pct,
        "belpex": belpex,
        "action": action,
        "writes_today": writes_today,
        "writes_total": writes_total,
        "window_low": _r(window_low),
        "window_high": _r(window_high),
        "window_target": _r(window_target),
        "inj_cost_ratio": None if inj_cost_ratio is None else round(inj_cost_ratio, 3),
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


# --- curtailment policy (static config, for the diagnostics card) ---------
#
# The tuned window/dwell/budget parameters don't change at runtime, so we publish them once
# (retained) as attributes on a diagnostic sensor. A markdown card renders them so the dashboard
# always shows what tuning is actually live (and auto-updates if it's ever re-tuned).

def policy_discovery_config(
    node_id: str, policy_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the policy sensor."""
    cfg = {
        "name": "Curtailment policy",
        "unique_id": f"smart_home_{node_id}_policy",
        "object_id": "solar_policy",  # -> sensor.solar_policy
        "state_topic": policy_topic,
        "value_template": "{{ value_json.summary }}",
        "json_attributes_topic": policy_topic,
        "icon": "mdi:tune-variant",
        "entity_category": "diagnostic",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/sensor/smart_home_{node_id}/policy/config": cfg}


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


# --- manual derating override ---------------------------------------------
#
# A "Manual override" switch + a "Manual derating %" number. When the switch is ON the
# controller writes the chosen % directly and ignores the plan (full precedence). The switch
# is never persisted — it reverts to OFF on restart so a reboot can't strand the inverter
# pinned at a manual value. The % is remembered for convenience.

def manual_override_discovery_config(
    node_id: str, command_topic: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the manual-override switch."""
    cfg = {
        "name": "Manual override",
        "unique_id": f"smart_home_{node_id}_manual_override",
        "object_id": "solar_manual_override",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:hand-back-right",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/switch/smart_home_{node_id}/manual_override/config": cfg}


def manual_number_discovery_config(
    node_id: str, command_topic: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the manual-derating number."""
    cfg = {
        "name": "Manual derating",
        "unique_id": f"smart_home_{node_id}_manual_derating",
        "object_id": "solar_manual_derating",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "min": 0,
        "max": 100,
        "step": 1,
        "unit_of_measurement": "%",
        "mode": "slider",
        "icon": "mdi:speedometer",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/number/smart_home_{node_id}/manual_derating/config": cfg}


# --- manual injection (export) limit --------------------------------------
#
# An "Injection limit" switch + an "Injection target (W)" number. When the switch is ON the
# controller closed-loops the inverter so grid export holds at the target watts (ignoring the
# plan). Mutually exclusive with the manual-derating override.

def injection_limit_discovery_config(
    node_id: str, command_topic: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the injection-limit switch."""
    cfg = {
        "name": "Injection limit",
        "unique_id": f"smart_home_{node_id}_injection_limit",
        "object_id": "solar_injection_limit",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:transmission-tower-import",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/switch/smart_home_{node_id}/injection_limit/config": cfg}


def injection_target_discovery_config(
    node_id: str, command_topic: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the injection-target number."""
    cfg = {
        "name": "Injection target",
        "unique_id": f"smart_home_{node_id}_injection_target",
        "object_id": "solar_injection_target",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "min": 0,
        "max": 5000,
        "step": 50,
        "unit_of_measurement": "W",
        "mode": "box",
        "icon": "mdi:transmission-tower-export",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/number/smart_home_{node_id}/injection_target/config": cfg}


# --- day-ahead "prices missing" alert -------------------------------------
#
# A HA problem binary_sensor the daily refresh sets if it couldn't fetch tomorrow's prices by the
# deadline (e.g. ENTSO-E never published / was unreachable all afternoon). Published by the refresh
# process, which is short-lived — hence the standalone connect/publish/disconnect helper below.

def alert_discovery_config(
    node_id: str, state_topic: str, discovery_prefix: str = "homeassistant"
) -> dict[str, dict[str, Any]]:
    """Return the {config_topic: payload} HA-discovery message for the day-ahead alert."""
    cfg = {
        "name": "Day-ahead prices missing",
        "unique_id": f"smart_home_{node_id}_dayahead_alert",
        "object_id": "solar_dayahead_alert",
        "state_topic": state_topic,
        "device_class": "problem",
        "payload_on": "PROBLEM",
        "payload_off": "OK",
        "icon": "mdi:cash-clock",
        "device": _device(node_id),
    }
    return {f"{discovery_prefix}/binary_sensor/smart_home_{node_id}/dayahead_alert/config": cfg}


def publish_dayahead_alert(
    host: str, port: int = 1883, username: str | None = None, password: str | None = None,
    *, node_id: str = "solarpi", problem: bool, discovery_prefix: str = "homeassistant",
) -> None:
    """One-shot: connect, publish the alert discovery + retained state (PROBLEM/OK), disconnect."""
    import paho.mqtt.client as mqtt  # noqa: PLC0415

    state_topic = f"smart_home/{node_id}/dayahead_alert/state"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smart_home-refresh-{node_id}")
    if username:
        client.username_pw_set(username, password)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    try:
        for topic, payload in alert_discovery_config(node_id, state_topic, discovery_prefix).items():
            client.publish(topic, json.dumps(payload), retain=True).wait_for_publish()
        client.publish(state_topic, "PROBLEM" if problem else "OK", retain=True).wait_for_publish()
    finally:
        client.loop_stop()
        client.disconnect()


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
        self._policy_topic = f"smart_home/{node_id}/policy"
        self._switch_state_topic = f"smart_home/{node_id}/curtail/state"
        self._switch_cmd_topic = f"smart_home/{node_id}/curtail/set"
        self._manual_sw_state_topic = f"smart_home/{node_id}/manual/state"
        self._manual_sw_cmd_topic = f"smart_home/{node_id}/manual/set"
        self._manual_num_state_topic = f"smart_home/{node_id}/manual_pct/state"
        self._manual_num_cmd_topic = f"smart_home/{node_id}/manual_pct/set"
        self._inj_sw_state_topic = f"smart_home/{node_id}/injection/state"
        self._inj_sw_cmd_topic = f"smart_home/{node_id}/injection/set"
        self._inj_num_state_topic = f"smart_home/{node_id}/injection_w/state"
        self._inj_num_cmd_topic = f"smart_home/{node_id}/injection_w/set"
        self._on_curtail_command = None
        self._on_manual_override = None
        self._on_manual_number = None
        self._on_injection_override = None
        self._on_injection_number = None
        self._client = None

    def connect(self, on_curtail_command=None, on_manual_override=None, on_manual_number=None,
                on_injection_override=None, on_injection_number=None) -> None:
        """Connect and publish HA discovery. Each callback, when given, exposes its control and
        is invoked on HA changes: curtail/manual_override with a bool, manual_number with a float."""
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        self._on_curtail_command = on_curtail_command
        self._on_manual_override = on_manual_override
        self._on_manual_number = on_manual_number
        self._on_injection_override = on_injection_override
        self._on_injection_number = on_injection_number
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
        for topic, payload in policy_discovery_config(self._node_id, self._policy_topic, self._discovery_prefix).items():
            self._client.publish(topic, json.dumps(payload), retain=True)
        if on_curtail_command is not None:
            self._publish_discovery(switch_discovery_config(
                self._node_id, self._switch_cmd_topic, self._switch_state_topic, self._discovery_prefix))
            self._client.subscribe(self._switch_cmd_topic)
        if on_manual_override is not None:
            self._publish_discovery(manual_override_discovery_config(
                self._node_id, self._manual_sw_cmd_topic, self._manual_sw_state_topic, self._discovery_prefix))
            self._client.subscribe(self._manual_sw_cmd_topic)
        if on_manual_number is not None:
            self._publish_discovery(manual_number_discovery_config(
                self._node_id, self._manual_num_cmd_topic, self._manual_num_state_topic, self._discovery_prefix))
            self._client.subscribe(self._manual_num_cmd_topic)
        if on_injection_override is not None:
            self._publish_discovery(injection_limit_discovery_config(
                self._node_id, self._inj_sw_cmd_topic, self._inj_sw_state_topic, self._discovery_prefix))
            self._client.subscribe(self._inj_sw_cmd_topic)
        if on_injection_number is not None:
            self._publish_discovery(injection_target_discovery_config(
                self._node_id, self._inj_num_cmd_topic, self._inj_num_state_topic, self._discovery_prefix))
            self._client.subscribe(self._inj_num_cmd_topic)

    def _publish_discovery(self, configs: dict[str, dict[str, Any]]) -> None:
        for topic, payload in configs.items():
            self._client.publish(topic, json.dumps(payload), retain=True)

    def _handle_message(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == self._switch_cmd_topic and self._on_curtail_command is not None:
            if payload.upper() in ("ON", "OFF"):
                self._on_curtail_command(payload.upper() == "ON")
        elif msg.topic == self._manual_sw_cmd_topic and self._on_manual_override is not None:
            if payload.upper() in ("ON", "OFF"):
                self._on_manual_override(payload.upper() == "ON")
        elif msg.topic == self._manual_num_cmd_topic and self._on_manual_number is not None:
            try:
                self._on_manual_number(float(payload))
            except ValueError:
                pass
        elif msg.topic == self._inj_sw_cmd_topic and self._on_injection_override is not None:
            if payload.upper() in ("ON", "OFF"):
                self._on_injection_override(payload.upper() == "ON")
        elif msg.topic == self._inj_num_cmd_topic and self._on_injection_number is not None:
            try:
                self._on_injection_number(float(payload))
            except ValueError:
                pass

    def publish_state(self, payload: dict[str, Any]) -> None:
        if self._client is not None:
            self._client.publish(self._state_topic, json.dumps(payload), retain=True)

    def publish_plan(self, payload: dict[str, Any]) -> None:
        """Publish the full day-ahead plan (retained) for the forecast chart."""
        if self._client is not None:
            self._client.publish(self._plan_topic, json.dumps(payload), retain=True)

    def publish_policy(self, payload: dict[str, Any]) -> None:
        """Publish the static curtailment-policy config (retained) for the diagnostics card."""
        if self._client is not None:
            self._client.publish(self._policy_topic, json.dumps(payload), retain=True)

    def publish_switch_state(self, enabled: bool) -> None:
        """Reflect the curtailment switch state back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._switch_state_topic, "ON" if enabled else "OFF", retain=True)

    def publish_manual_override_state(self, enabled: bool) -> None:
        """Reflect the manual-override switch state back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._manual_sw_state_topic, "ON" if enabled else "OFF", retain=True)

    def publish_manual_number_state(self, pct: float) -> None:
        """Reflect the manual-derating % back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._manual_num_state_topic, f"{pct:g}", retain=True)

    def publish_injection_override_state(self, enabled: bool) -> None:
        """Reflect the injection-limit switch state back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._inj_sw_state_topic, "ON" if enabled else "OFF", retain=True)

    def publish_injection_number_state(self, watts: float) -> None:
        """Reflect the injection target (W) back to HA (retained)."""
        if self._client is not None:
            self._client.publish(self._inj_num_state_topic, f"{watts:g}", retain=True)

    def close(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
