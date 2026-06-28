"""Persisted day-ahead curtailment plan.

Day-ahead prices are fixed once published (~13:00 CET, visible by 16:00), so we fetch and
build the whole plan **once per day** and persist it. The control loop then runs entirely
from this cached plan — no network in the hot path — and looks up the action for the
current slot via :meth:`Schedule.action_at`.

Resilience: a cached plan keeps working if ENTSO-E is unreachable later in the day. If the
plan does not cover "now" (a fetch has been failing too long), ``action_at`` returns ``None``
and the control loop must fail safe to NORMAL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from smart_home.economics import Action, Slot
from smart_home.prices import BRUSSELS, build_schedule, fetch_raw_prices

DEFAULT_PATH = Path.home() / ".smart_home" / "schedule.json"


def _parse(start: str) -> datetime:
    return datetime.fromisoformat(start)


@dataclass
class Schedule:
    """An ordered list of priced + decided :class:`Slot`s, with disk persistence."""

    slots: list[Slot]

    def _step(self) -> timedelta:
        """Slot duration, inferred from the first two starts (default 1h)."""
        if len(self.slots) >= 2:
            return _parse(self.slots[1].start) - _parse(self.slots[0].start)
        return timedelta(hours=1)

    def action_at(self, when: datetime) -> Slot | None:
        """The slot covering ``when`` (tz-aware), or ``None`` if not covered."""
        if not self.slots:
            return None
        when = when.astimezone(BRUSSELS)
        step = self._step()
        for s in self.slots:
            start = _parse(s.start)
            if start <= when < start + step:
                return s
        return None

    def covers(self, when: datetime) -> bool:
        return self.action_at(when) is not None

    def covers_tomorrow(self, now: datetime) -> bool:
        """True if the plan has any slot dated tomorrow (Brussels) — i.e. tomorrow's day-ahead
        prices have actually been fetched (not just today's)."""
        tomorrow = (now.astimezone(BRUSSELS) + timedelta(days=1)).date()
        return any(_parse(s.start).date() == tomorrow for s in self.slots)

    # --- persistence ------------------------------------------------------

    def to_json(self) -> str:
        rows = [{**asdict(s), "action": s.action.value} for s in self.slots]
        return json.dumps(rows, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Schedule":
        return cls(
            [
                Slot(
                    start=d["start"],
                    belpex=d["belpex"],
                    night=d["night"],
                    consume_price=d["consume_price"],
                    feedin_price=d["feedin_price"],
                    action=Action(d["action"]),
                )
                for d in json.loads(text)
            ]
        )

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(path)  # atomic swap — never leave a half-written plan

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Schedule":
        return cls.from_json(Path(path).read_text())


def refresh(token: str, path: Path = DEFAULT_PATH, *, days_ahead: int = 1) -> Schedule:
    """Fetch today..today+days_ahead, build the plan, and persist it atomically."""
    today = date.today()
    raw = fetch_raw_prices(token, today, today + timedelta(days=days_ahead))
    schedule = Schedule(build_schedule(raw))
    schedule.save(path)
    return schedule


DEFAULT_DEADLINE_HOUR = 17
FIRST_WINDOW = timedelta(hours=1)        # dense retries for the first hour after start
FIRST_INTERVAL = timedelta(minutes=5)
LATER_INTERVAL = timedelta(hours=1)


def refresh_until_available(
    token: str,
    path: Path = DEFAULT_PATH,
    *,
    now_fn,
    sleep_fn,
    deadline_hour: int = DEFAULT_DEADLINE_HOUR,
    on_event=None,
) -> bool:
    """Refresh repeatedly until **tomorrow's** prices are cached, or today's deadline passes.

    Cadence: retry every 5 min for the first hour after start, then hourly, stopping once the
    next attempt would fall at/after ``deadline_hour`` (local). Transient errors and
    not-yet-published prices both just trigger a retry. ``now_fn``/``sleep_fn`` are injected so
    the policy is testable. Returns True if tomorrow's prices were obtained, else False.

    ``on_event(kind, detail)`` (optional) is called with kind in
    {"attempt","success","retry","give_up","error"} for logging / alerting.
    """
    start = now_fn()
    deadline = start.replace(hour=deadline_hour, minute=0, second=0, microsecond=0)
    while True:
        now = now_fn()
        if on_event:
            on_event("attempt", f"{now:%H:%M} fetching day-ahead plan")
        try:
            schedule = refresh(token, path)
            available = schedule.covers_tomorrow(now)
        except Exception as exc:  # noqa: BLE001 — network/no-data: treat as not-yet-available
            available = False
            if on_event:
                on_event("error", f"{type(exc).__name__}: {exc}")
        if available:
            if on_event:
                on_event("success", "tomorrow's prices cached")
            return True
        interval = FIRST_INTERVAL if (now - start) < FIRST_WINDOW else LATER_INTERVAL
        nxt = now + interval
        if nxt >= deadline:
            if on_event:
                on_event("give_up", f"no day-ahead prices for tomorrow by {deadline:%H:%M}")
            return False
        if on_event:
            on_event("retry", f"not available yet; next attempt {nxt:%H:%M}")
        sleep_fn((nxt - now).total_seconds())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage the day-ahead curtailment plan.")
    parser.add_argument("command", choices=["refresh", "now"], help="refresh the plan, or show the current slot")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="plan file location")
    parser.add_argument("--wait-tomorrow", action="store_true",
                        help="keep retrying (5 min for 1h, then hourly) until tomorrow's prices "
                             "are cached or the deadline passes; alert in HA on give-up")
    parser.add_argument("--deadline-hour", type=int, default=DEFAULT_DEADLINE_HOUR,
                        help="local hour to stop retrying (default 17)")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST"),
                        help="MQTT broker for the HA 'day-ahead missing' alert (default $MQTT_HOST)")
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    parser.add_argument("--node-id", default=os.environ.get("NODE_ID", "solarpi"))
    args = parser.parse_args(argv)

    if args.command == "refresh":
        token = os.environ.get("ENTSOE_API_TOKEN")
        if not token:
            print("Set ENTSOE_API_TOKEN", file=sys.stderr)
            sys.exit(1)

        if args.wait_tomorrow:
            import time as _time  # noqa: PLC0415
            ok = refresh_until_available(
                token, args.path,
                now_fn=lambda: datetime.now(BRUSSELS), sleep_fn=_time.sleep,
                deadline_hour=args.deadline_hour,
                on_event=lambda kind, detail: print(f"[{kind}] {detail}", flush=True),
            )
            if args.mqtt_host:
                try:
                    from smart_home.mqtt import publish_dayahead_alert  # noqa: PLC0415
                    publish_dayahead_alert(
                        args.mqtt_host, args.mqtt_port, args.mqtt_user, os.environ.get("MQTT_PW"),
                        node_id=args.node_id, problem=not ok,
                    )
                except Exception as exc:  # noqa: BLE001 — alerting must never crash the refresh
                    print(f"[warn] could not publish HA alert: {exc}", file=sys.stderr)
            sys.exit(0 if ok else 1)

        schedule = refresh(token, args.path)
        if not schedule.slots:
            print("No prices returned (tomorrow's may not be published yet).")
            return
        print(f"Saved {len(schedule.slots)} slots to {args.path}")
        print(f"Covers {schedule.slots[0].start[:16]} .. {schedule.slots[-1].start[:16]} (Brussels)")
        return

    # "now"
    schedule = Schedule.load(args.path)
    now = datetime.now(BRUSSELS)
    slot = schedule.action_at(now)
    if slot is None:
        print(f"{now:%Y-%m-%d %H:%M} — plan does NOT cover now -> fail safe to {Action.NORMAL.value}")
        sys.exit(2)
    print(f"{now:%Y-%m-%d %H:%M} -> {slot.action.value}  (BELPEX {slot.belpex:.1f}, "
          f"consume {slot.consume_price:.2f}, feedin {slot.feedin_price:.2f})")


if __name__ == "__main__":
    main()
