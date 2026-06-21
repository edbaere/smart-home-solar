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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage the day-ahead curtailment plan.")
    parser.add_argument("command", choices=["refresh", "now"], help="refresh the plan, or show the current slot")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH, help="plan file location")
    args = parser.parse_args(argv)

    if args.command == "refresh":
        token = os.environ.get("ENTSOE_API_TOKEN")
        if not token:
            print("Set ENTSOE_API_TOKEN", file=sys.stderr)
            sys.exit(1)
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
