"""Read-only status / pre-flight: live P1 + inverter readings and the derating that
``compute_setpoint`` would apply for each action. Never writes to the inverter.

    python -m smart_home.status --p1-host 192.168.3.74

Useful for monitoring and as a safe sanity check before deploying/enabling actuation.
The number formatting is a pure function (:func:`format_status`) so it is testable offline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from smart_home import control, inverter, p1
from smart_home.economics import Action


def format_status(
    *,
    production_w: float,
    p1_net_w: float,
    p_max_w: float,
    control_mode: str | None = None,
    derating_pct: float | None = None,
    per_phase: tuple[float | None, float | None, float | None] | None = None,
    margin_w: float = control.DEFAULT_MARGIN_W,
) -> str:
    """Render live measurements + the setpoint preview for each action."""
    load = production_w + p1_net_w
    lines = [
        "=== live (read-only) ===",
        f"inverter production : {production_w:.0f} W",
        f"P1 net              : {p1_net_w:+.0f} W   ({'importing' if p1_net_w >= 0 else 'exporting'})",
    ]
    if per_phase is not None:
        lines.append(f"  per phase         : L1={per_phase[0]} L2={per_phase[1]} L3={per_phase[2]}")
    lines += [
        f"implied load        : {load:.0f} W",
        f"P_MAX               : {p_max_w:.0f} W",
        f"control_mode        : {control_mode}   derating {derating_pct}%",
        "=== compute_setpoint per action (what WOULD be written) ===",
    ]
    for action in (Action.NORMAL, Action.ZERO_EXPORT, Action.FULL_CURTAIL):
        sp = control.compute_setpoint(
            action,
            inverter_active_power_w=production_w,
            p1_net_w=p1_net_w,
            p_max_w=p_max_w,
            margin_w=margin_w,
        )
        cap = f"{sp.target_w:.0f} W" if sp.target_w is not None else "unlimited"
        lines.append(f"  {action.value:<12} -> {sp.derating_percent:5.1f}%   cap {cap:>10}   ({sp.reason})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Read-only status / setpoint preview (no writes).")
    ap.add_argument("--inverter-host", default=inverter.DEFAULT_HOST)
    ap.add_argument("--inverter-port", type=int, default=inverter.DEFAULT_PORT)
    ap.add_argument("--p1-host", required=True, help="HomeWizard P1 IP/host")
    ap.add_argument("--margin", type=float, default=control.DEFAULT_MARGIN_W,
                    help="over-production margin in W (default %(default)s)")
    args = ap.parse_args(argv)

    try:
        inv = asyncio.run(inverter.read(args.inverter_host, args.inverter_port))
        p1r = p1.read(args.p1_host)
    except Exception as e:  # noqa: BLE001 — surface any read failure plainly
        print(f"read failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print(format_status(
        production_w=inv.active_power_w or 0.0,
        p1_net_w=p1r.active_power_w,
        p_max_w=inv.p_max_w or 5000.0,
        control_mode=inv.control_mode,
        derating_pct=inv.percentage_derating,
        per_phase=(p1r.active_power_l1_w, p1r.active_power_l2_w, p1r.active_power_l3_w),
        margin_w=args.margin,
    ))


if __name__ == "__main__":
    main()
