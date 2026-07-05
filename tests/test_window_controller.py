"""Tests for the shared stateful WindowController (the shipping curtailment policy)."""

import pytest

from smart_home.control import WindowController, derating_for_target_export
from smart_home.economics import Action, curtail_window

MILD = 5.0   # BELPEX in the ZERO_EXPORT band, injection ~cheap -> wide window
PMAX = 5000.0


def mk(**kw):
    base = dict(dwell_down_s=10.0, dwell_up_s=60.0, min_interval_s=120.0)
    base.update(kw)
    wc = WindowController(**base)
    wc.sync(100.0)
    return wc


def ze(wc, export_w, load_w, now, belpex=MILD):
    return wc.decide(action=Action.ZERO_EXPORT, belpex=belpex, night=False,
                     export_w=export_w, load_w=load_w, p_max_w=PMAX, now=now)


# --- NORMAL / FULL_CURTAIL transitions ------------------------------------

def test_normal_no_write_when_already_full():
    wc = mk()
    d = wc.decide(action=Action.NORMAL, belpex=50.0, night=False,
                  export_w=0, load_w=0, p_max_w=PMAX, now=0)
    assert not d.should_write and d.target_percent == 100.0


def test_normal_restores_when_curtailed():
    wc = mk()
    wc.sync(40.0)                       # inverter currently curtailed
    d = wc.decide(action=Action.NORMAL, belpex=50.0, night=False,
                  export_w=0, load_w=0, p_max_w=PMAX, now=0)
    assert d.should_write and d.target_percent == 100.0


def test_full_curtail_writes_zero():
    wc = mk()
    d = wc.decide(action=Action.FULL_CURTAIL, belpex=-200.0, night=False,
                  export_w=0, load_w=0, p_max_w=PMAX, now=0)
    assert d.should_write and d.target_percent == 0.0


# --- ZERO_EXPORT window + dwell + min-interval ----------------------------

def test_in_window_never_writes():
    wc = mk()
    _, _, _ = curtail_window(MILD)                 # window ~ [71, 1197]
    assert not ze(wc, export_w=400, load_w=1000, now=0).should_write
    assert not ze(wc, export_w=400, load_w=1000, now=999).should_write


def test_high_breach_waits_for_dwell_then_writes():
    wc = mk()
    _, _, tset = curtail_window(MILD)
    assert not ze(wc, 1500, 1000, now=0).should_write     # breach starts, dwell_up=60 not met
    assert not ze(wc, 1500, 1000, now=30).should_write
    d = ze(wc, 1500, 1000, now=70)                         # dwell met
    assert d.should_write
    assert d.target_percent == pytest.approx(derating_for_target_export(tset, 1000, PMAX), abs=0.1)


def test_import_side_reacts_faster_than_export_side():
    # low breach uses dwell_down (10 s) — should fire sooner than the 60 s export side
    wc = mk()
    wc.sync(20.0)
    assert not ze(wc, export_w=-200, load_w=1200, now=0).should_write
    d = ze(wc, export_w=-200, load_w=1200, now=15)         # 15 s > dwell_down 10 s
    assert d.should_write and d.target_percent > 20.0      # raises production to fight import


def test_min_interval_blocks_rapid_rewrites():
    wc = mk()
    assert not ze(wc, 1500, 1000, now=0).should_write      # prime the breach
    assert ze(wc, 1500, 1000, now=70).should_write         # dwell met -> write (last_write=70)
    wc.sync(wc.last_command)
    assert not ze(wc, 1500, 3000, now=100).should_write    # re-prime breach (dwell not met)
    assert not ze(wc, 1500, 3000, now=165).should_write    # dwell met, but 165-70<120 -> interval blocks
    assert ze(wc, 1500, 3000, now=200).should_write        # 200-70>120 and target changed -> writes


def test_read_first_no_write_when_already_at_target():
    wc = mk()
    assert not ze(wc, 1500, 1000, now=0).should_write      # prime
    d1 = ze(wc, 1500, 1000, now=70)
    assert d1.should_write
    wc.sync(d1.target_percent)                             # inverter already at the target
    assert not ze(wc, 1500, 1000, now=200).should_write    # re-prime breach (dwell not met)
    # dwell + interval satisfied, but target unchanged -> no redundant write
    assert not ze(wc, 1500, 1000, now=265).should_write


# --- live window exposure (for monitoring) --------------------------------

def test_decide_exposes_live_window_in_zero_export():
    wc = mk()
    ze(wc, export_w=400, load_w=1000, now=0)
    assert wc.last_low is not None and wc.last_high is not None
    assert wc.last_low < wc.last_target < wc.last_high     # target sits inside the band
    assert 0.0 <= wc.last_r <= 1.0


def test_decide_clears_window_outside_zero_export():
    wc = mk()
    ze(wc, export_w=400, load_w=1000, now=0)               # populate
    wc.decide(action=Action.NORMAL, belpex=50.0, night=False,
              export_w=0, load_w=0, p_max_w=PMAX, now=1)
    assert wc.last_low is None and wc.last_target is None and wc.last_r is None
