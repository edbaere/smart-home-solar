"""Tests for the status formatter (pure — no hardware, no library)."""

from smart_home.status import format_status


def test_zero_export_line_shows_load_plus_margin():
    # production 2816, net -1532 -> load 1284; +200 margin -> cap 1484 W
    out = format_status(production_w=2816, p1_net_w=-1532, p_max_w=5000, margin_w=200)
    assert "implied load        : 1284 W" in out
    assert "exporting" in out
    assert "cap     1484 W" in out      # ZERO_EXPORT cap
    assert "unlimited" in out            # NORMAL
    assert "cap        0 W" in out       # FULL_CURTAIL


def test_importing_label():
    out = format_status(production_w=1000, p1_net_w=588, p_max_w=5000)
    assert "importing" in out


def test_per_phase_optional():
    out = format_status(production_w=1000, p1_net_w=0, p_max_w=5000)
    assert "per phase" not in out
    out2 = format_status(production_w=1000, p1_net_w=0, p_max_w=5000, per_phase=(-2074, 12, 529))
    assert "L1=-2074" in out2
