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

from smart_home.economics import (
    AIM_MAX_W,
    CEIL_CURVE_K,
    CEIL_MAX_W,
    FLOOR_W,
    Action,
    curtail_window,
)

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


def injection_limit_percent(
    injection_target_w: float,
    *,
    inverter_active_power_w: float,
    p1_net_w: float,
    p_max_w: float,
) -> float:
    """Derating % that caps grid export (injection) at ``injection_target_w`` watts.

    Closed loop: ``load = inverter_active_power_w + p1_net_w``; to hold export at the target we
    need inverter output ``load + injection_target_w`` (then net grid = -target = exporting
    ``target`` W). Below that production level no curtailment is needed (output stays at 100%).
    Recomputed each control tick from live measurements as load changes.
    """
    load_w = inverter_active_power_w + p1_net_w
    target_w = max(0.0, load_w + injection_target_w)
    return round(max(0.0, min(100.0, target_w / p_max_w * 100.0)), 1)


# --- windowed ZERO_EXPORT control (write only when export leaves [L, U]) ---

def window_breach(export_w: float, low_w: float, high_w: float) -> str | None:
    """Which edge of the acceptable-export window `[low_w, high_w]` the current export breaches.

    ``export_w`` is grid export in watts (positive = exporting; negative = importing). Returns
    ``"low"`` (drifting toward import — correct fast), ``"high"`` (over-exporting), or ``None``
    (inside the window — do nothing).
    """
    if export_w < low_w:
        return "low"
    if export_w > high_w:
        return "high"
    return None


def derating_for_target_export(target_export_w: float, load_w: float, p_max_w: float) -> float:
    """Derating % that makes grid export land at ``target_export_w`` for the given load.

    Production needed = ``load_w + target_export_w`` (then net grid = −target = that export).
    """
    cap_w = max(0.0, load_w + target_export_w)
    return round(max(0.0, min(100.0, cap_w / p_max_w * 100.0)), 1)


# --- stateful window controller (shared by the live daemon AND the backtest) ---
#
# Tuned defaults (see docs/curtailment-redesign.md, backtest on the Feb–Jul price library):
# ceil 1200 W, k=2, aim 400 W, floor 75 W, dwell_up 300 s, dwell_down 20 s, min-interval 120 s.
# These give ~5k writes/yr (vs ~36k for tight tracking) at ~83% of the ~€40/yr max saving.

DWELL_DOWN_S = 20.0        # import-side: react fast (importing is always expensive)
DWELL_UP_S = 300.0         # export-side: don't rush (over-export is cheap when injection is cheap)
MIN_WRITE_INTERVAL_S = 120.0  # hard floor between writes (protects the flash)
WRITE_EPS_PCT = 0.5        # treat a command within this of the last as a no-op (read-first)


@dataclass(frozen=True)
class WindowDecision:
    should_write: bool
    target_percent: float
    reason: str


class WindowController:
    """Price-scaled asymmetric-window ZERO_EXPORT control with write-minimisation.

    Writes to the inverter only when grid export leaves the ``[L, U]`` window (from
    :func:`~smart_home.economics.curtail_window`), the breach has been sustained past the
    (asymmetric) dwell, and it's been at least ``min_interval_s`` since the last write. NORMAL
    and FULL_CURTAIL write once on transition (only if the value actually changes). The 40125
    register is non-volatile, so every avoided write saves a flash cycle.

    Stateful and shared: the live controller and the offline backtest both drive this, so they
    run byte-identical decision logic. Seed :meth:`sync` from a fresh read so the first decision
    is read-first (no redundant write if the inverter already holds the target).
    """

    def __init__(
        self,
        *,
        floor_w: float = FLOOR_W,
        ceil_max_w: float = CEIL_MAX_W,
        aim_max_w: float = AIM_MAX_W,
        k: float = CEIL_CURVE_K,
        dwell_down_s: float = DWELL_DOWN_S,
        dwell_up_s: float = DWELL_UP_S,
        min_interval_s: float = MIN_WRITE_INTERVAL_S,
        write_eps_pct: float = WRITE_EPS_PCT,
    ):
        self.floor_w, self.ceil_max_w, self.aim_max_w, self.k = floor_w, ceil_max_w, aim_max_w, k
        self.dwell_down_s, self.dwell_up_s = dwell_down_s, dwell_up_s
        self.min_interval_s, self.write_eps_pct = min_interval_s, write_eps_pct
        self.last_command: float | None = None
        self.breach_side: str | None = None
        self.breach_start = 0.0
        self.last_write = -1e18

    def sync(self, current_pct: float) -> None:
        """Seed the last-commanded value from a fresh inverter read (enables read-first)."""
        self.last_command = current_pct

    def _commit(self, target: float, now: float, *, mark_write: bool, reason: str) -> WindowDecision:
        lc = 100.0 if self.last_command is None else self.last_command
        if abs(target - lc) >= self.write_eps_pct:
            self.last_command = target
            self.breach_side = None
            if mark_write:
                self.last_write = now
            return WindowDecision(True, target, reason)
        return WindowDecision(False, lc, reason + " (no change)")

    def decide(
        self,
        *,
        action: Action,
        belpex: float,
        night: bool,
        export_w: float,
        load_w: float,
        p_max_w: float,
        now: float,
    ) -> WindowDecision:
        """Return whether to write and the target derating % for this control tick."""
        if action is Action.NORMAL:
            self.breach_side = None
            return self._commit(100.0, now, mark_write=False, reason="normal")
        if action is Action.FULL_CURTAIL:
            self.breach_side = None
            return self._commit(0.0, now, mark_write=False, reason="full-curtail")

        # ZERO_EXPORT: windowed control
        low, high, target_export = curtail_window(
            belpex, night=night, floor_w=self.floor_w, ceil_max_w=self.ceil_max_w,
            aim_max_w=self.aim_max_w, k=self.k,
        )
        side = window_breach(export_w, low, high)
        lc = 100.0 if self.last_command is None else self.last_command
        if side is None:
            self.breach_side = None
            return WindowDecision(False, lc, f"in window [{low:.0f},{high:.0f}]W export={export_w:.0f}")
        if side != self.breach_side:
            self.breach_side, self.breach_start = side, now
        dwell = self.dwell_down_s if side == "low" else self.dwell_up_s
        if (now - self.breach_start) < dwell or (now - self.last_write) < self.min_interval_s:
            return WindowDecision(False, lc, f"{side} breach: waiting (dwell/interval)")
        target = derating_for_target_export(target_export, load_w, p_max_w)
        return self._commit(target, now, mark_write=True, reason=f"zero-export {side}->{target:.0f}%")
