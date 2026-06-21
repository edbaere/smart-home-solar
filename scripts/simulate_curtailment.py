#!/usr/bin/env python3
"""Simulate ZERO_EXPORT curtailment dynamics: constant solar, swinging load.

Validates/tunes the control behaviour without hardware. The controller commands a derating
cap = load + margin each cycle, but the inverter only ramps its output toward that cap at the
configured gradient (default 0.277 %/s). production = min(solar, ramping_cap).

    python scripts/simulate_curtailment.py                  # default config + trace
    python scripts/simulate_curtailment.py --margin 400     # try a bigger over-production buffer
    python scripts/simulate_curtailment.py --gradient 2.0   # what a faster ramp would do

Use it to pick `margin` (the grid-safe lever) vs touching the inverter gradient.
"""
from __future__ import annotations

import argparse

P_MAX = 5000.0
DT = 1


def load_at(t: int) -> float:
    """Built-in scenario: ±1.5–2 kW steps around a 1 kW base."""
    if t < 60:   return 1000.0
    if t < 240:  return 3000.0   # +2 kW
    if t < 420:  return 1000.0   # -2 kW
    if t < 540:  return 2500.0   # +1.5 kW
    return 1000.0                 # -1.5 kW


def run(gradient_pct_s: float, margin: float, interval: int, solar: float, duration: int):
    cap_pct = (load_at(0) + margin) / P_MAX * 100   # start settled
    target_pct = cap_pct
    imp = exp = 0.0
    peak_imp = peak_exp = 0.0
    rows = []
    for t in range(0, duration + 1, DT):
        load = load_at(t)
        if t % interval == 0:
            target_pct = max(0.0, min(100.0, (load + margin) / P_MAX * 100))
        if cap_pct < target_pct:
            cap_pct = min(target_pct, cap_pct + gradient_pct_s * DT)
        else:
            cap_pct = max(target_pct, cap_pct - gradient_pct_s * DT)
        prod = min(solar, cap_pct / 100 * P_MAX)
        net = load - prod                       # + import / - export
        if net > 0: imp += net * DT / 3.6e6
        else:       exp += -net * DT / 3.6e6
        peak_imp = max(peak_imp, net); peak_exp = min(peak_exp, net)
        if t % 15 == 0:
            rows.append((t, load, cap_pct / 100 * P_MAX, prod, net))
    return dict(rows=rows, imp=imp, exp=exp, peak_imp=peak_imp, peak_exp=peak_exp)


def bar(net: float, scale: float = 150, w: int = 18) -> str:
    n = max(-w, min(w, int(round(net / scale))))
    return (" " * w + "|" + "#" * n) if n >= 0 else (" " * (w + n) + "#" * (-n) + "|")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Simulate ZERO_EXPORT curtailment dynamics.")
    ap.add_argument("--gradient", type=float, default=0.277, help="inverter ramp, %%/s")
    ap.add_argument("--margin", type=float, default=200.0, help="over-production buffer, W")
    ap.add_argument("--interval", type=int, default=30, help="controller cycle, s")
    ap.add_argument("--solar", type=float, default=3000.0, help="constant solar availability, W")
    ap.add_argument("--duration", type=int, default=600, help="sim length, s")
    a = ap.parse_args(argv)

    r = run(a.gradient, a.margin, a.interval, a.solar, a.duration)
    print(f"gradient {a.gradient} %/s ({a.gradient/100*P_MAX:.0f} W/s) | margin {a.margin:.0f} W "
          f"| cycle {a.interval}s | solar {a.solar:.0f} W")
    print(f"{'t':>4} {'load':>5} {'cap':>5} {'prod':>5} {'net':>6}   <-export | import->")
    for t, load, cap, prod, net in r["rows"]:
        print(f"{t:>4} {load:>5.0f} {cap:>5.0f} {prod:>5.0f} {net:>+6.0f}   {bar(net)}")
    print(f"\npeak: import {r['peak_imp']:+.0f} W, export {r['peak_exp']:+.0f} W")
    print(f"energy: import {r['imp']*1000:.0f} Wh ({r['imp']*12:.1f}c @12c/kWh), "
          f"export {r['exp']*1000:.0f} Wh ({r['exp']*1.15:.1f}c @1.15c/kWh penalty)")


if __name__ == "__main__":
    main()
