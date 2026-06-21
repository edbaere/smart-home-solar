"""Compute the inverter active-power setpoint from a price decision + live measurements.

Pure and hardware-free: given the current :class:`~smart_home.economics.Action` for the slot
plus live readings (inverter AC output and P1 net grid power), produce the percentage to write
to ``ACTIVE_POWER_PERCENTAGE_DERATING`` (register 40125). The actuation daemon (Phase 3) does
the login + write; this module only decides the number.

Sign / metering conventions (3-phase connection, single-phase PV on L1; BE digital meter nets
the three phases, so we work entirely with the *net* total):
  * ``p1_net_w`` : net grid power, **+ = importing, − = exporting** (sum across phases).
  * ``inverter_active_power_w`` : current AC output (PV production).
  * ``load_w = inverter_active_power_w + p1_net_w`` (production + net import).

Zero-export target keeps a small **over-production margin**: erring toward slight export
(costs the feed-in penalty, ~1 ct/kWh) is ~10× cheaper than erring toward import (costs the
full ~12 ct/kWh consume price), so we cap at ``load + margin`` rather than exactly ``load``.
"""

from __future__ import annotations

from dataclasses import dataclass

from smart_home.economics import Action

DEFAULT_MARGIN_W = 200.0  # over-production buffer: we'd rather slightly export than import


@dataclass(frozen=True)
class Setpoint:
    """The derating to apply for the current slot."""

    action: Action
    derating_percent: float       # write to ACTIVE_POWER_PERCENTAGE_DERATING (40125); 100 = no cap
    target_w: float | None        # production cap in W (None = unlimited)
    reason: str


def compute_setpoint(
    action: Action,
    *,
    inverter_active_power_w: float,
    p1_net_w: float,
    p_max_w: float,
    margin_w: float = DEFAULT_MARGIN_W,
) -> Setpoint:
    """Derive the derating setpoint for ``action`` given live measurements.

    - NORMAL       -> 100% (no cap), export surplus freely.
    - FULL_CURTAIL -> 0% (stop production); grid pays us to consume.
    - ZERO_EXPORT  -> cap production at ``load + margin`` (slight over-production), as a
      percentage of rated power.
    """
    if action is Action.NORMAL:
        return Setpoint(action, 100.0, None, "no curtailment")

    if action is Action.FULL_CURTAIL:
        return Setpoint(action, 0.0, 0.0, "consume price < 0: stop all production")

    # ZERO_EXPORT
    load_w = inverter_active_power_w + p1_net_w
    target_w = max(0.0, load_w + margin_w)
    pct = max(0.0, min(100.0, target_w / p_max_w * 100.0))
    return Setpoint(action, round(pct, 1), target_w, "zero-export: cap at load + margin")
