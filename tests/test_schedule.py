"""Tests for the persisted curtailment plan (no network)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from smart_home.economics import Action, Slot
from smart_home.schedule import Schedule

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
