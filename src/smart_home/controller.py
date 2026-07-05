"""Phase 3 control daemon: apply the cached day-ahead plan to the inverter, fail-safe.

The loop runs at **two cadences** (the controller is the sole inverter Modbus client, so all
reads/publishes happen here — they can't be split into a competing process):

* **Telemetry tick** (``telemetry_interval_s``, default 1 s): read the P1 (net grid power) and
  the inverter's PV output, then publish ``pv_power``/``grid_power``/``load_power`` (+ phases,
  totals) to MQTT for Home Assistant. High-resolution monitoring, no decisions, no writes.
* **Control tick** (``interval_s``, default 30 s): the curtailment *decision*. Price slots are
  15 min and the inverter ramps at ~0.277 %/s, so the decision needs nothing faster.

Each control tick:
  1. Resolve the current :class:`~smart_home.economics.Action` from the cached schedule
     (``action_at(now)``). If the plan doesn't cover *now*, **fail safe to NORMAL** (no
     curtailment) — we never leave the inverter stuck curtailed because of a stale plan.
  2. Read the inverter (AC output, P_MAX, current derating) and the P1 (net grid power).
  3. ``compute_setpoint`` -> target derating %. Write it only if it differs from the current
     value by more than a deadband (the inverter ramps slowly at ~0.277 %/s, so frequent
     tiny writes are pointless).
  4. Any error -> restore 100% (fail-safe) and keep looping.

The cached action / derating / belpex from the last control tick ride along on every
telemetry publish, so the fast-changing power values stay at 1 Hz while the slow decision
fields update every control tick.

The inverter is the **sole Modbus client** here (single-connection constraint). Reads are
unauthenticated; a write triggers an installer ``login()`` (cached; re-login on permission
error). Confirmed on hardware: a plain ``set`` of the derating register persists without a
heartbeat, so no heartbeat loop is needed.

``huawei-solar`` is imported lazily so the pure helpers (and tests) need neither the lib nor
hardware. Install with ``pip install '.[hw]'``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import json

from smart_home.control import (
    DEFAULT_MARGIN_W,
    MIN_WRITE_INTERVAL_S,
    WindowController,
    compute_setpoint,
    injection_limit_percent,
)
from smart_home.economics import Action
from smart_home.schedule import DEFAULT_PATH, Schedule

BRUSSELS = ZoneInfo("Europe/Brussels")
FULL_POWER_PCT = 100.0
DEFAULT_DEADBAND_PCT = 2.0
DEFAULT_INTERVAL_S = 30.0           # control-decision cadence
DEFAULT_TELEMETRY_INTERVAL_S = 1.0  # MQTT publish / fast-read cadence
DEFAULT_CURTAIL_STATE_PATH = Path.home() / ".smart_home" / "curtail_enabled"
DEFAULT_MANUAL_STATE_PATH = Path.home() / ".smart_home" / "manual_derating"
DEFAULT_INJECTION_STATE_PATH = Path.home() / ".smart_home" / "injection_target"
DEFAULT_WRITE_COUNTER_PATH = Path.home() / ".smart_home" / "write_counter.json"
MAX_INJECTION_W = 5000.0
WRITE_BUDGET_DAY = 400   # backstop: register 40125 is non-volatile, so cap writes/day (a bug guard)


class WriteCounter:
    """Persisted count of inverter derating writes (40125 is non-volatile → flash wear).

    Tracks writes today + cumulative, resets daily, and enforces a per-day budget backstop so a
    bug can't storm the flash. Fail-safe writes bypass the budget (safety over wear).
    """

    def __init__(self, path: Path = DEFAULT_WRITE_COUNTER_PATH, budget: int = WRITE_BUDGET_DAY):
        self._path = Path(path)
        self.budget = budget
        d = self._load()
        self.date, self.today, self.total = d

    def _load(self) -> tuple[str, int, int]:
        try:
            d = json.loads(self._path.read_text())
            return d.get("date", ""), int(d.get("today", 0)), int(d.get("total", 0))
        except Exception:
            return "", 0, 0

    def _today(self) -> str:
        return datetime.now(BRUSSELS).strftime("%Y-%m-%d")

    def allowed(self) -> bool:
        if self._today() != self.date:
            return True
        if self.today >= self.budget:
            log.warning("daily write budget %d reached (total=%d) — holding setpoint", self.budget, self.total)
            return False
        return True

    def bump(self) -> None:
        t = self._today()
        if t != self.date:
            self.date, self.today = t, 0
        self.today += 1
        self.total += 1
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"date": self.date, "today": self.today, "total": self.total}))
        except Exception:
            log.exception("write-counter persist failed")

log = logging.getLogger("smart_home.controller")


class CurtailGate:
    """Persisted on/off gate for whether curtailment is actually written to the inverter.

    Defaults to OFF (safe: inverter runs at full power) when no state file exists. The state
    survives restarts so a reboot never silently re-enables (or disables) curtailment.
    """

    def __init__(self, path: Path = DEFAULT_CURTAIL_STATE_PATH):
        self._path = Path(path)
        self.enabled = self._load()

    def _load(self) -> bool:
        try:
            return self._path.read_text().strip() == "1"
        except FileNotFoundError:
            return False

    def set(self, enabled: bool) -> None:
        self.enabled = enabled
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("1" if enabled else "0")
        except Exception:
            log.exception("failed to persist curtail-enable state")


class ManualOverride:
    """Manual "set derating now" override. When ``enabled`` the controller writes ``pct``
    directly and ignores the plan (full precedence).

    ``enabled`` is deliberately NOT persisted — it reverts to OFF on restart so a reboot can
    never strand the inverter pinned at a manual value. ``pct`` is persisted for convenience.
    """

    def __init__(self, path: Path = DEFAULT_MANUAL_STATE_PATH):
        self._path = Path(path)
        self.enabled = False
        self.pct = self._load_pct()

    def _load_pct(self) -> float:
        try:
            return _clamp_pct(float(self._path.read_text().strip()))
        except (FileNotFoundError, ValueError):
            return FULL_POWER_PCT

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_pct(self, pct: float) -> None:
        self.pct = _clamp_pct(pct)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(f"{self.pct:g}")
        except Exception:
            log.exception("failed to persist manual-derating pct")


def _clamp_pct(pct: float) -> float:
    return max(0.0, min(100.0, pct))


class InjectionOverride:
    """Manual grid-export (injection) limit. When ``enabled`` the controller closed-loops the
    inverter so export holds at ``target_w`` watts, ignoring the plan.

    Like :class:`ManualOverride`, ``enabled`` is not persisted (reverts to OFF on restart);
    ``target_w`` is persisted for convenience.
    """

    def __init__(self, path: Path = DEFAULT_INJECTION_STATE_PATH):
        self._path = Path(path)
        self.enabled = False
        self.target_w = self._load_target()

    def _load_target(self) -> float:
        try:
            return _clamp_w(float(self._path.read_text().strip()))
        except (FileNotFoundError, ValueError):
            return 0.0

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_target(self, watts: float) -> None:
        self.target_w = _clamp_w(watts)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(f"{self.target_w:g}")
        except Exception:
            log.exception("failed to persist injection target")


def _clamp_w(watts: float) -> float:
    return max(0.0, min(MAX_INJECTION_W, watts))


# --- pure decision logic (no I/O) -----------------------------------------

@dataclass(frozen=True)
class Step:
    """The decision for one control cycle."""

    action: Action
    target_percent: float
    should_write: bool
    reason: str


def resolve_action(schedule: Schedule, now: datetime) -> Action:
    """Action for ``now`` from the plan, or NORMAL fail-safe if the plan doesn't cover it."""
    slot = schedule.action_at(now)
    return slot.action if slot is not None else Action.NORMAL


def control_every(interval_s: float, telemetry_interval_s: float) -> int:
    """How many telemetry ticks fall between control ticks (>= 1)."""
    if telemetry_interval_s <= 0:
        return 1
    return max(1, round(interval_s / telemetry_interval_s))


def plan_step(
    action: Action,
    *,
    inverter_active_power_w: float,
    p1_net_w: float,
    p_max_w: float,
    current_derating_pct: float,
    margin_w: float = DEFAULT_MARGIN_W,
    deadband_pct: float = DEFAULT_DEADBAND_PCT,
) -> Step:
    """Compute the target derating and whether it's worth writing (vs the current value)."""
    sp = compute_setpoint(
        action,
        inverter_active_power_w=inverter_active_power_w,
        p1_net_w=p1_net_w,
        p_max_w=p_max_w,
        margin_w=margin_w,
    )
    target = sp.derating_percent
    should_write = abs(target - current_derating_pct) >= deadband_pct
    return Step(action=action, target_percent=target, should_write=should_write, reason=sp.reason)


# --- inverter I/O (huawei-solar) ------------------------------------------

class InverterClient:
    """Owns the single inverter connection; reads, and writes derating with login."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self._host, self._port, self._user, self._password = host, port, user, password
        self._device = None
        self._logged_in = False

    async def connect(self) -> None:
        from huawei_solar import create_device_instance, create_tcp_client  # noqa: PLC0415
        client = create_tcp_client(host=self._host, port=self._port)
        self._device = await create_device_instance(client)
        self._logged_in = False

    async def read(self) -> dict:
        from huawei_solar import register_names as rn  # noqa: PLC0415
        names = {
            "active_power": rn.ACTIVE_POWER,
            "p_max": rn.P_MAX,
            "derating": rn.ACTIVE_POWER_PERCENTAGE_DERATING,
        }
        data = await self._device.batch_update(list(names.values()))
        return {k: getattr(data[r], "value", None) for k, r in names.items()}

    async def read_active_power(self) -> float | None:
        """Light read of just PV AC output (for the 1 Hz telemetry tick)."""
        from huawei_solar import register_names as rn  # noqa: PLC0415
        data = await self._device.batch_update([rn.ACTIVE_POWER])
        return getattr(data[rn.ACTIVE_POWER], "value", None)

    async def set_derating(self, pct: float) -> None:
        from huawei_solar import register_names as rn  # noqa: PLC0415
        try:
            if not self._logged_in:
                self._logged_in = await self._device.login(self._user, self._password)
            await self._device.set(rn.ACTIVE_POWER_PERCENTAGE_DERATING, pct)
        except Exception:
            # session may have expired; re-login once and retry
            self._logged_in = await self._device.login(self._user, self._password)
            await self._device.set(rn.ACTIVE_POWER_PERCENTAGE_DERATING, pct)


# --- the loop -------------------------------------------------------------

async def run(
    *,
    plan_path,
    inverter_host: str,
    inverter_port: int,
    p1_host: str,
    user: str,
    password: str,
    margin_w: float = DEFAULT_MARGIN_W,
    interval_s: float = DEFAULT_INTERVAL_S,
    telemetry_interval_s: float = DEFAULT_TELEMETRY_INTERVAL_S,
    deadband_pct: float = DEFAULT_DEADBAND_PCT,
    dry_run: bool = False,
    once: bool = False,
    mqtt_host: str | None = None,
    mqtt_port: int = 1883,
    mqtt_user: str | None = None,
    mqtt_password: str | None = None,
    node_id: str = "solarpi",
    curtail_state_path: Path = DEFAULT_CURTAIL_STATE_PATH,
    manual_state_path: Path = DEFAULT_MANUAL_STATE_PATH,
    injection_state_path: Path = DEFAULT_INJECTION_STATE_PATH,
) -> None:
    from smart_home import p1 as p1_mod  # noqa: PLC0415

    # Runtime gate: when disabled, behave like dry-run (compute + show, never curtail).
    # `--dry-run` is a hard override that disables writing regardless of the gate.
    gate = CurtailGate(curtail_state_path)
    manual = ManualOverride(manual_state_path)        # enabled starts OFF (reverts on restart)
    injection = InjectionOverride(injection_state_path)  # mutually exclusive with `manual`
    writes = WriteCounter()                           # flash-wear guard (40125 is non-volatile)
    wc = WindowController()                           # tuned defaults (docs/curtailment-redesign.md)

    inv = InverterClient(inverter_host, inverter_port, user, password)
    await inv.connect()
    pub = None
    if mqtt_host:
        from smart_home.mqtt import Publisher  # noqa: PLC0415
        pub = Publisher(mqtt_host, mqtt_port, mqtt_user, mqtt_password, node_id=node_id)

        # All run on the paho callback thread; the writes they touch are atomic.
        def _on_curtail_command(enabled: bool) -> None:
            gate.set(enabled)
            pub.publish_switch_state(enabled)
            log.info("curtailment %s via HA switch", "ENABLED" if enabled else "disabled")

        def _on_manual_override(enabled: bool) -> None:
            manual.set_enabled(enabled)
            if enabled and injection.enabled:  # manual modes are mutually exclusive
                injection.set_enabled(False)
                pub.publish_injection_override_state(False)
            pub.publish_manual_override_state(enabled)
            log.info("manual override %s (%.0f%%)", "ON" if enabled else "off", manual.pct)

        def _on_manual_number(pct: float) -> None:
            manual.set_pct(pct)
            pub.publish_manual_number_state(manual.pct)
            log.info("manual derating set to %.0f%%", manual.pct)

        def _on_injection_override(enabled: bool) -> None:
            injection.set_enabled(enabled)
            if enabled and manual.enabled:  # manual modes are mutually exclusive
                manual.set_enabled(False)
                pub.publish_manual_override_state(False)
            pub.publish_injection_override_state(enabled)
            log.info("injection limit %s (%.0fW)", "ON" if enabled else "off", injection.target_w)

        def _on_injection_number(watts: float) -> None:
            injection.set_target(watts)
            pub.publish_injection_number_state(injection.target_w)
            log.info("injection target set to %.0fW", injection.target_w)

        pub.connect(
            on_curtail_command=None if dry_run else _on_curtail_command,
            on_manual_override=None if dry_run else _on_manual_override,
            on_manual_number=None if dry_run else _on_manual_number,
            on_injection_override=None if dry_run else _on_injection_override,
            on_injection_number=None if dry_run else _on_injection_number,
        )
        if not dry_run:
            pub.publish_switch_state(gate.enabled)
            pub.publish_manual_override_state(manual.enabled)
            pub.publish_manual_number_state(manual.pct)
            pub.publish_injection_override_state(injection.enabled)
            pub.publish_injection_number_state(injection.target_w)
        log.info("MQTT publishing to %s:%d (curtailment %s)", mqtt_host, mqtt_port,
                 "dry-run" if dry_run else ("ENABLED" if gate.enabled else "disabled"))
        # Static policy config (retained) for the diagnostics card — publish once.
        pub.publish_policy({
            "summary": (f"ceil {int(wc.ceil_max_w)}W · aim {int(wc.aim_max_w)}W · "
                        f"floor {int(wc.floor_w)}W · k{wc.k:g} · dwell {int(wc.dwell_up_s)}/"
                        f"{int(wc.dwell_down_s)}s · min-int {int(wc.min_interval_s)}s"),
            "ceil_max_w": wc.ceil_max_w, "aim_max_w": wc.aim_max_w, "floor_w": wc.floor_w,
            "k": wc.k, "dwell_up_s": wc.dwell_up_s, "dwell_down_s": wc.dwell_down_s,
            "min_interval_s": wc.min_interval_s, "write_budget_day": writes.budget,
        })
    if pub is not None:
        from smart_home.mqtt import plan_payload, state_payload  # noqa: PLC0415
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    control_ratio = control_every(interval_s, telemetry_interval_s)
    # cached outputs of the last control tick, published on every (faster) telemetry tick
    action = Action.NORMAL
    action_label = Action.NORMAL.value  # what HA shows (becomes "MANUAL" under manual override)
    target_pct = FULL_POWER_PCT
    derating_pct: float | None = None
    belpex: float | None = None
    win_low = win_high = win_target = win_r = None  # live window (monitoring)
    last_plan_sig: tuple | None = None  # republish the forecast only when the plan changes
    last_inj_write = -1e18              # monotonic ts of the last injection-override write
    tick = 0

    try:
        while not stop.is_set():
            do_control = (tick % control_ratio == 0)
            tick += 1
            pv: float | None = None
            p1r = None

            # --- control cadence: decide + (maybe) write -----------------
            if do_control:
                try:
                    now = datetime.now(BRUSSELS)
                    schedule = Schedule.load(plan_path)
                    slot = schedule.action_at(now)
                    action = slot.action if slot is not None else Action.NORMAL  # fail-safe NORMAL
                    belpex = slot.belpex if slot is not None else None
                    # Publish the full plan for the HA forecast chart, but only when it changes
                    # (once per daily refresh) — not every control tick. Guarded so a publish
                    # hiccup never trips the control fail-safe.
                    if pub is not None and schedule.slots:
                        cur_sig = (
                            len(schedule.slots),
                            schedule.slots[0].start,
                            schedule.slots[-1].start,
                        )
                        if cur_sig != last_plan_sig:
                            try:
                                pub.publish_plan(plan_payload(schedule.slots))
                                last_plan_sig = cur_sig
                                log.info("published plan: %d slots %s..%s", *cur_sig)
                            except Exception:
                                log.debug("plan publish failed", exc_info=True)
                    reading = await inv.read()
                    p1r = await asyncio.to_thread(p1_mod.read, p1_host)
                    pv = reading["active_power"] or 0.0
                    p_max = reading["p_max"] or 5000.0
                    cur = reading["derating"] if reading["derating"] is not None else FULL_POWER_PCT
                    derating_pct = reading["derating"]
                    load_w = pv + p1r.active_power_w          # p1 net: + = importing
                    export_w = -p1r.active_power_w            # + = exporting
                    now_mono = loop.time()
                    # Keep the window controller aligned with the real (persistent) setpoint each
                    # tick: 40125 reads back the commanded value, so this is a read-first compare
                    # and also picks up any external change (manual mode, reboot, etc.).
                    wc.sync(cur)

                    # Precedence: manual derating > injection limit > plan (when curtailment
                    # enabled) > full power. The two manual modes are mutually exclusive (the HA
                    # callbacks enforce that). `--dry-run` is a hard override on every write.
                    manual_on = manual.enabled and not dry_run
                    injection_on = injection.enabled and not dry_run
                    curtail_on = gate.enabled and not dry_run
                    reason = ""
                    win_low = win_high = win_target = win_r = None  # set only in ZERO_EXPORT (plan) mode

                    if manual_on:
                        target_pct, action_label, mode = manual.pct, "MANUAL", "manual"
                        if abs(target_pct - cur) >= deadband_pct and writes.allowed():
                            await inv.set_derating(target_pct); writes.bump()
                            derating_pct = target_pct
                            log.info("manual wrote derating=%.1f%%", target_pct)
                    elif injection_on:
                        target_pct = injection_limit_percent(
                            injection.target_w, inverter_active_power_w=pv,
                            p1_net_w=p1r.active_power_w, p_max_w=p_max,
                        )
                        action_label, mode = "INJECTION", f"inj<={injection.target_w:.0f}W"
                        if (abs(target_pct - cur) >= deadband_pct
                                and now_mono - last_inj_write >= MIN_WRITE_INTERVAL_S
                                and writes.allowed()):
                            await inv.set_derating(target_pct); writes.bump()
                            derating_pct, last_inj_write = target_pct, now_mono
                            log.info("injection wrote derating=%.1f%%", target_pct)
                    else:
                        dec = wc.decide(
                            action=action, belpex=belpex or 0.0,
                            night=(slot.night if slot is not None else False),
                            export_w=export_w, load_w=load_w, p_max_w=p_max, now=now_mono,
                        )
                        target_pct, action_label, reason = dec.target_percent, action.value, dec.reason
                        win_low, win_high, win_target, win_r = (
                            wc.last_low, wc.last_high, wc.last_target, wc.last_r)
                        mode = "on" if curtail_on else ("dry-run" if dry_run else "off")
                        if curtail_on and dec.should_write and writes.allowed():
                            await inv.set_derating(dec.target_percent); writes.bump()
                            derating_pct = dec.target_percent
                            log.info("wrote derating=%.1f%% (%s)", dec.target_percent, dec.reason)
                        elif not curtail_on and not dry_run and cur < FULL_POWER_PCT - deadband_pct:
                            # gate off but inverter still curtailed -> restore once (read-first)
                            await inv.set_derating(FULL_POWER_PCT); writes.bump()
                            derating_pct = FULL_POWER_PCT
                            log.info("curtail off -> restored derating=%.0f%%", FULL_POWER_PCT)
                    log.info(
                        "action=%s target=%.1f%% (cur=%s%%) mode=%s prod=%.0fW net=%+dW "
                        "writes[today=%d total=%d] :: %s",
                        action_label, target_pct, reading["derating"], mode,
                        pv, int(p1r.active_power_w), writes.today, writes.total, reason,
                    )
                except Exception:
                    log.exception("control cycle failed -> fail-safe to %.0f%%", FULL_POWER_PCT)
                    action, action_label, target_pct = Action.NORMAL, Action.NORMAL.value, FULL_POWER_PCT
                    pv, p1r = None, None  # force a fresh light read below
                    # Read-first: only write 100 if we believe the inverter is curtailed. 40125 is
                    # non-volatile, so re-writing 100 every failed tick would needlessly wear flash.
                    if not dry_run and (derating_pct is None or derating_pct < FULL_POWER_PCT - 1):
                        try:
                            await inv.set_derating(FULL_POWER_PCT); writes.bump()
                            derating_pct = FULL_POWER_PCT
                        except Exception:
                            log.exception("FAIL-SAFE WRITE FAILED")

            # --- telemetry cadence (every tick): fast read + publish -----
            if pub is not None:
                try:
                    if pv is None:
                        pv = await inv.read_active_power()
                    if p1r is None:
                        p1r = await asyncio.to_thread(p1_mod.read, p1_host)
                    pub.publish_state(state_payload(
                        action=action_label, derating_pct=derating_pct,
                        target_derating_pct=target_pct,
                        pv_power_w=pv or 0.0, grid_net_w=p1r.active_power_w,
                        l1_w=p1r.active_power_l1_w, l2_w=p1r.active_power_l2_w, l3_w=p1r.active_power_l3_w,
                        import_total_kwh=p1r.total_import_kwh, export_total_kwh=p1r.total_export_kwh,
                        belpex=belpex,
                        writes_today=writes.today, writes_total=writes.total,
                        window_low=win_low, window_high=win_high, window_target=win_target,
                        inj_cost_ratio=win_r,
                    ))
                except Exception:
                    log.debug("telemetry tick failed", exc_info=True)

            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=telemetry_interval_s)
            except asyncio.TimeoutError:
                pass
    finally:
        if pub is not None:
            pub.close()
        # leaving control -> restore full production so we never strand the inverter curtailed.
        # Read-first on the last-known setpoint: skip the write if we're already at 100 (40125 is
        # non-volatile, and autodeploy restarts the service often — no need to re-write 100 each time).
        if not dry_run and (derating_pct is None or derating_pct < FULL_POWER_PCT - 1):
            log.info("shutting down -> restoring 100%%")
            try:
                await inv.set_derating(FULL_POWER_PCT); writes.bump()
            except Exception:
                log.exception("shutdown fail-safe write failed")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Apply the day-ahead plan to the inverter (fail-safe).")
    ap.add_argument("--plan-path", default=DEFAULT_PATH)
    ap.add_argument("--inverter-host", default="192.168.200.1")
    ap.add_argument("--inverter-port", type=int, default=6607)
    ap.add_argument("--p1-host", required=True)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_W)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                    help="control-decision cadence in seconds (default 30)")
    ap.add_argument("--telemetry-interval", type=float,
                    default=float(os.environ.get("TELEMETRY_INTERVAL", DEFAULT_TELEMETRY_INTERVAL_S)),
                    help="MQTT publish / fast-read cadence in seconds (default 1)")
    ap.add_argument("--deadband", type=float, default=DEFAULT_DEADBAND_PCT)
    ap.add_argument("--dry-run", action="store_true", help="compute + log, never write")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST"), help="MQTT broker (enables HA publishing)")
    ap.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--node-id", default=os.environ.get("NODE_ID", "solarpi"))
    ap.add_argument("--curtail-state-path", type=Path, default=DEFAULT_CURTAIL_STATE_PATH,
                    help="file persisting the HA curtailment-enable switch (default OFF)")
    ap.add_argument("--manual-state-path", type=Path, default=DEFAULT_MANUAL_STATE_PATH,
                    help="file persisting the manual-derating %% (override itself resets OFF on restart)")
    ap.add_argument("--injection-state-path", type=Path, default=DEFAULT_INJECTION_STATE_PATH,
                    help="file persisting the injection target W (override itself resets OFF on restart)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    user = os.environ.get("HUAWEI_USER", "installer")
    password = os.environ.get("HUAWEI_PW", "")
    if not args.dry_run and not password:
        print("Set HUAWEI_PW (installer password) or use --dry-run", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(
        plan_path=args.plan_path,
        inverter_host=args.inverter_host, inverter_port=args.inverter_port,
        p1_host=args.p1_host, user=user, password=password,
        margin_w=args.margin, interval_s=args.interval,
        telemetry_interval_s=args.telemetry_interval, deadband_pct=args.deadband,
        dry_run=args.dry_run, once=args.once,
        mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port,
        mqtt_user=args.mqtt_user, mqtt_password=os.environ.get("MQTT_PW"),
        node_id=args.node_id, curtail_state_path=args.curtail_state_path,
        manual_state_path=args.manual_state_path,
        injection_state_path=args.injection_state_path,
    ))


if __name__ == "__main__":
    main()
