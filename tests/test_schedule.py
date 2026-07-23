"""Tests for the persisted curtailment plan (no network)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import smart_home.schedule as sched
from smart_home.economics import Action, Slot
from smart_home.schedule import Schedule, refresh_until_available

BRUSSELS = ZoneInfo("Europe/Brussels")


def _slots() -> list[Slot]:
    # two 15-min slots starting 10:00 Brussels (CEST, +02:00)
    return [
        Slot.from_belpex("2026-06-21T10:00:00+02:00", 5.0),   # ZERO_EXPORT
        Slot.from_belpex("2026-06-21T10:15:00+02:00", 90.0),  # NORMAL
    ]


def test_action_at_within_first_slot():
    s = Schedule(_slots())
    slot = s.action_at(datetime(2026, 6, 21, 10, 7, tzinfo=BRUSSELS))
    assert slot is not None and slot.action == Action.ZERO_EXPORT


def test_action_at_picks_correct_slot():
    s = Schedule(_slots())
    assert s.action_at(datetime(2026, 6, 21, 10, 20, tzinfo=BRUSSELS)).action == Action.NORMAL


def test_action_at_before_and_after_coverage_is_none():
    s = Schedule(_slots())
    assert s.action_at(datetime(2026, 6, 21, 9, 0, tzinfo=BRUSSELS)) is None
    # last slot covers 10:15..10:30; 10:30 is past the end
    assert s.action_at(datetime(2026, 6, 21, 10, 30, tzinfo=BRUSSELS)) is None
    assert not s.covers(datetime(2026, 6, 21, 10, 30, tzinfo=BRUSSELS))


def test_action_at_handles_other_timezone_input():
    s = Schedule(_slots())
    # 08:07 UTC == 10:07 Brussels -> inside the first slot
    slot = s.action_at(datetime(2026, 6, 21, 8, 7, tzinfo=ZoneInfo("UTC")))
    assert slot is not None and slot.action == Action.ZERO_EXPORT


def test_json_roundtrip():
    s = Schedule(_slots())
    s2 = Schedule.from_json(s.to_json())
    assert [x.action for x in s2.slots] == [x.action for x in s.slots]
    assert s2.slots[0].belpex == 5.0
    assert s2.slots[1].start == "2026-06-21T10:15:00+02:00"


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "schedule.json"
    Schedule(_slots()).save(path)
    loaded = Schedule.load(path)
    assert loaded.action_at(datetime(2026, 6, 21, 10, 7, tzinfo=BRUSSELS)).action == Action.ZERO_EXPORT


def test_empty_schedule_action_is_none():
    assert Schedule([]).action_at(datetime(2026, 6, 21, 10, 7, tzinfo=BRUSSELS)) is None


# --- dropping incomplete (not-yet-published) days --------------------------

def _full_day(day: str, n: int = 96) -> list[Slot]:
    """``n`` quarter-hour slots starting at ``day`` 00:00 Brussels (CEST, +02:00)."""
    return [
        Slot.from_belpex(f"{day}T{(i * 15) // 60:02d}:{(i * 15) % 60:02d}:00+02:00", 50.0)
        for i in range(n)
    ]


def test_drop_incomplete_days_drops_stray_stub():
    # A real published day (96 quarter-hour slots) plus a single stray stub slot for the next
    # day (ENTSO-E's not-yet-published placeholder) — the stub must be dropped entirely.
    slots = _full_day("2026-06-21") + [Slot.from_belpex("2026-06-22T00:00:00+02:00", 0.0)]
    kept = sched._drop_incomplete_days(slots)
    assert len(kept) == 96
    assert all(sched._parse(s.start).date().isoformat() == "2026-06-21" for s in kept)


def test_drop_incomplete_days_keeps_two_full_days():
    slots = _full_day("2026-06-21") + _full_day("2026-06-22")
    kept = sched._drop_incomplete_days(slots)
    assert len(kept) == 192


# --- retry-until-available policy -----------------------------------------

def _today_only():
    return Schedule([Slot.from_belpex("2026-06-21T10:00:00+02:00", 50.0)])


def _with_tomorrow():
    return Schedule([
        Slot.from_belpex("2026-06-21T10:00:00+02:00", 50.0),
        Slot.from_belpex("2026-06-22T10:00:00+02:00", 50.0),
    ])


def test_covers_tomorrow():
    now = datetime(2026, 6, 21, 12, 30, tzinfo=BRUSSELS)
    assert _with_tomorrow().covers_tomorrow(now) is True
    assert _today_only().covers_tomorrow(now) is False


class _Clock:
    def __init__(self, start):
        self.t = start
    def now(self):
        return self.t
    def sleep(self, secs):
        self.t += timedelta(seconds=secs)


def test_retry_succeeds_when_prices_appear(monkeypatch):
    clock = _Clock(datetime(2026, 6, 21, 12, 15, tzinfo=BRUSSELS))
    results = [_today_only(), _today_only(), _with_tomorrow()]  # available on the 3rd try
    calls = []
    monkeypatch.setattr(sched, "refresh", lambda *a, **k: (calls.append(1), results.pop(0))[1])

    ok = refresh_until_available("tok", now_fn=clock.now, sleep_fn=clock.sleep)

    assert ok is True
    assert len(calls) == 3
    assert clock.t == datetime(2026, 6, 21, 12, 25, tzinfo=BRUSSELS)  # two 5-min waits


def test_retry_gives_up_at_deadline_and_signals(monkeypatch):
    clock = _Clock(datetime(2026, 6, 21, 12, 15, tzinfo=BRUSSELS))
    monkeypatch.setattr(sched, "refresh", lambda *a, **k: _today_only())  # never has tomorrow
    events = []

    ok = refresh_until_available(
        "tok", now_fn=clock.now, sleep_fn=clock.sleep,
        on_event=lambda kind, detail: events.append(kind),
    )

    assert ok is False
    assert "give_up" in events
    assert clock.t < datetime(2026, 6, 21, 17, 0, tzinfo=BRUSSELS)   # stopped before the deadline
    assert events.count("attempt") >= 13   # many dense attempts before the 17:00 deadline


def test_retry_cadence_escalates_by_clock(monkeypatch):
    clock = _Clock(datetime(2026, 6, 21, 12, 15, tzinfo=BRUSSELS))
    times = []
    monkeypatch.setattr(sched, "refresh",
                        lambda *a, **k: (times.append(clock.now()), _today_only())[1])

    refresh_until_available("tok", now_fn=clock.now, sleep_fn=clock.sleep)

    def gaps(lo, hi):  # minute-gaps between consecutive attempts whose hour is in [lo, hi)
        seg = [t for t in times if lo <= t.hour < hi]
        return {round((b - a).total_seconds() / 60) for a, b in zip(seg, seg[1:])}

    assert gaps(12, 15) == {5}    # dense 5-min through the ~13–15h publish window
    assert gaps(15, 16) == {15}   # then back off to 15-min
    assert gaps(16, 17) == {30}   # then 30-min until the deadline
    # last attempt is 16:30; the next would be 17:00 == deadline, so it gives up instead
    assert times[-1] == datetime(2026, 6, 21, 16, 30, tzinfo=BRUSSELS)
