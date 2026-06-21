"""Phase 3 control daemon: apply the cached day-ahead plan to the inverter, fail-safe.

Each cycle:
  1. Resolve the current :class:`~smart_home.economics.Action` from the cached schedule
     (``action_at(now)``). If the plan doesn't cover *now*, **fail safe to NORMAL** (no
     curtailment) — we never leave the inverter stuck curtailed because of a stale plan.
  2. Read the inverter (AC output, P_MAX, current derating) and the P1 (net grid power).
  3. ``compute_setpoint`` -> target derating %. Write it only if it differs from the current
     value by more than a deadband (the inverter ramps slowly at ~0.277 %/s, so frequent
     tiny writes are pointless).
  4. Any error -> restore 100% (fail-safe) and keep looping.

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
from zoneinfo import ZoneInfo

from smart_home.control import DEFAULT_MARGIN_W, compute_setpoint
from smart_home.economics import Action
from smart_home.schedule import DEFAULT_PATH, Schedule

BRUSSELS = ZoneInfo("Europe/Brussels")
FULL_POWER_PCT = 100.0
DEFAULT_DEADBAND_PCT = 2.0
DEFAULT_INTERVAL_S = 30.0

log = logging.getLogger("smart_home.controller")


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
    deadband_pct: float = DEFAULT_DEADBAND_PCT,
    dry_run: bool = False,
    once: bool = False,
) -> None:
    from smart_home import p1 as p1_mod  # noqa: PLC0415

    inv = InverterClient(inverter_host, inverter_port, user, password)
    await inv.connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                schedule = Schedule.load(plan_path)
                action = resolve_action(schedule, datetime.now(BRUSSELS))
                reading = await inv.read()
                p1r = await asyncio.to_thread(p1_mod.read, p1_host)
                step = plan_step(
                    action,
                    inverter_active_power_w=reading["active_power"] or 0.0,
                    p1_net_w=p1r.active_power_w,
                    p_max_w=reading["p_max"] or 5000.0,
                    current_derating_pct=reading["derating"] if reading["derating"] is not None else FULL_POWER_PCT,
                    margin_w=margin_w,
                    deadband_pct=deadband_pct,
                )
                log.info(
                    "action=%s target=%.1f%% (cur=%.1f%%) write=%s prod=%sW net=%+dW :: %s",
                    step.action.value, step.target_percent, reading["derating"],
                    step.should_write, reading["active_power"], int(p1r.active_power_w), step.reason,
                )
                if step.should_write and not dry_run:
                    await inv.set_derating(step.target_percent)
                    log.info("wrote derating=%.1f%%", step.target_percent)
            except Exception:
                log.exception("cycle failed -> fail-safe to %.0f%%", FULL_POWER_PCT)
                if not dry_run:
                    try:
                        await inv.set_derating(FULL_POWER_PCT)
                    except Exception:
                        log.exception("FAIL-SAFE WRITE FAILED")
            if once:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
    finally:
        # leaving control -> restore full production so we never strand the inverter curtailed
        if not dry_run:
            log.info("shutting down -> restoring 100%%")
            try:
                await inv.set_derating(FULL_POWER_PCT)
            except Exception:
                log.exception("shutdown fail-safe write failed")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Apply the day-ahead plan to the inverter (fail-safe).")
    ap.add_argument("--plan-path", default=DEFAULT_PATH)
    ap.add_argument("--inverter-host", default="192.168.200.1")
    ap.add_argument("--inverter-port", type=int, default=6607)
    ap.add_argument("--p1-host", required=True)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_W)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    ap.add_argument("--deadband", type=float, default=DEFAULT_DEADBAND_PCT)
    ap.add_argument("--dry-run", action="store_true", help="compute + log, never write")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
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
        margin_w=args.margin, interval_s=args.interval, deadband_pct=args.deadband,
        dry_run=args.dry_run, once=args.once,
    ))


if __name__ == "__main__":
    main()
