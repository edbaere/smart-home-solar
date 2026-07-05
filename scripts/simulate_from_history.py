"""Backtest curtailment policies on real recorded HA data (writes vs € tradeoff).

Replays historical *available* PV + load + day-ahead price through each policy, applying the
inverter's ramp, and accounts writes (flash-wear proxy) and € (injection penalty avoided minus
extra import caused). Imports the REAL `economics`/`control` functions so it tests the shipping
logic, not a reimplementation. See docs/curtailment-redesign.md.

Only *clean* steps are used — where the recorded derating was 100 % (or before the controller
started), so recorded PV = available PV. Curtailed/experiment periods are skipped.

    PYTHONPATH=src python3 scripts/simulate_from_history.py --csv /tmp/sim_raw.csv
    PYTHONPATH=src python3 scripts/simulate_from_history.py --csv /tmp/sim_raw.csv --sweep
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt

from smart_home.economics import (
    Action,
    consume_price,
    curtail_window,
    decide,
    feedin_price,
)
from smart_home.control import WindowController, derating_for_target_export, window_breach

PV = "sensor.smart_home_curtailment_pv_production"
LOAD = "sensor.smart_home_curtailment_load"
PRICE = "sensor.smart_home_curtailment_day_ahead_price"
DERATE = "sensor.smart_home_curtailment_active_power_derating"

P_MAX = 5000.0
GRADIENT_PCT_S = 0.277   # inverter ramp
DT = 10                  # sim grid, seconds


# --- data loading / alignment ---------------------------------------------

def load_csv(path: str) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {PV: [], LOAD: [], PRICE: [], DERATE: []}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            ent = row["entity_id"]
            if ent not in series:
                continue
            try:
                ts = dt.datetime.fromisoformat(row["ts_iso"]).timestamp()
                series[ent].append((ts, float(row["value"])))
            except ValueError:
                continue
    for s in series.values():
        s.sort()
    return series


def ffill(series: list[tuple[float, float]], grid: list[float]) -> list[float | None]:
    """Forward-fill a sorted (ts,val) series onto grid timestamps (None before first sample)."""
    out: list[float | None] = []
    i, n, cur = 0, len(series), None
    for t in grid:
        while i < n and series[i][0] <= t:
            cur = series[i][1]
            i += 1
        out.append(cur)
    return out


def night_at(ts: float) -> bool:
    h = dt.datetime.fromtimestamp(ts).hour
    return h >= 22 or h < 7


# --- accounting -----------------------------------------------------------

class Acc:
    def __init__(self) -> None:
        self.writes = 0
        self.exp_kwh = 0.0
        self.imp_kwh = 0.0
        self.penalty_eur = 0.0   # injection penalty paid on export
        self.import_eur = 0.0    # cost of imported energy

    def step(self, grid_net_w: float, price: float, night: bool) -> None:
        wh = abs(grid_net_w) * DT / 3600.0
        kwh = wh / 1000.0
        if grid_net_w < 0:                                  # exporting
            self.exp_kwh += kwh
            self.penalty_eur += kwh * max(0.0, -feedin_price(price)) / 100.0
        else:                                               # importing
            self.imp_kwh += kwh
            self.import_eur += kwh * consume_price(price, night=night) / 100.0

    @property
    def cost_eur(self) -> float:
        return self.penalty_eur + self.import_eur


# --- policies: given state + observation, return (new_command_pct, wrote) --

def _apply(commanded: float, new_cmd: float, eps: float = 0.5) -> tuple[float, bool]:
    """Write only if the command changes materially (read-first: skip no-op writes)."""
    if abs(new_cmd - commanded) >= eps:
        return new_cmd, True
    return commanded, False


def old_policy(st: dict, action: Action, export_w: float, load_w: float, t: float, price: float) -> None:
    """Tight tracking: every 30 s, cap = load+200 W; write if |Δ| ≥ 2 % (100 W)."""
    if t - st["last_decision"] < 30:
        return
    st["last_decision"] = t
    if action is Action.NORMAL:
        target = 100.0
    elif action is Action.FULL_CURTAIL:
        target = 0.0
    else:
        target = derating_for_target_export(200.0, load_w, P_MAX)
    new_cmd, wrote = _apply(st["cmd"], target, eps=2.0)
    st["cmd"] = new_cmd
    if wrote:
        st["writes"] += 1


# --- the replay -----------------------------------------------------------

def _make_window(p: dict) -> WindowController:
    """Build the SHIPPING WindowController from a sweep param dict (so the backtest and the live
    controller run identical logic)."""
    wc = WindowController(floor_w=p["floor"], ceil_max_w=p["ceil"], aim_max_w=p.get("aim", 400.0),
                          k=p["k"], dwell_down_s=p["dwell_down"], dwell_up_s=p["dwell_up"],
                          min_interval_s=p["min_interval"])
    wc.sync(100.0)   # sim days start uncurtailed
    return wc


def run(pv, load, price, clean, grid, arm: str, p: dict | None = None) -> Acc:
    acc = Acc()
    st = {"cmd": 100.0, "writes": 0, "last_decision": -1e9}
    wc = _make_window(p) if arm == "new" else None
    actual = 100.0
    for i, t in enumerate(grid):
        if not clean[i] or pv[i] is None or load[i] is None or price[i] is None:
            # not usable; keep inverter settled at 100 for continuity, no accounting
            actual = min(100.0, actual + GRADIENT_PCT_S * DT) if actual < 100 else 100.0
            st["cmd"] = 100.0
            continue
        pv_w, load_w, pr = pv[i], load[i], price[i]
        night = night_at(t)
        action = decide(pr, night=night)

        prod = min(pv_w, actual / 100.0 * P_MAX)
        export_w = prod - load_w                        # what P1 would read (>0 exporting)

        if arm == "baseline":
            pass                                        # no curtailment: command stays 100
        elif arm == "old":
            old_policy(st, action, export_w, load_w, t, pr)
        else:
            dec = wc.decide(action=action, belpex=pr, night=night, export_w=export_w,
                            load_w=load_w, p_max_w=P_MAX, now=t)
            if dec.should_write:
                st["cmd"] = dec.target_percent
                st["writes"] += 1

        commanded = 100.0 if arm == "baseline" else st["cmd"]
        if actual < commanded:
            actual = min(commanded, actual + GRADIENT_PCT_S * DT)
        else:
            actual = max(commanded, actual - GRADIENT_PCT_S * DT)

        prod2 = min(pv_w, actual / 100.0 * P_MAX)
        acc.step(load_w - prod2, pr, night)
    acc.writes = st["writes"]
    return acc


# --- main -----------------------------------------------------------------

def build_grid(series):
    lo = min(s[0][0] for s in series.values() if s)
    hi = max(s[-1][0] for s in series.values() if s)
    grid = [lo + k * DT for k in range(int((hi - lo) / DT) + 1)]
    cols = {k: ffill(v, grid) for k, v in series.items()}
    derate = cols[DERATE]
    clean = [(d is None) or (abs(d - 100.0) < 0.5) for d in derate]   # None = pre-controller = uncurtailed
    return grid, cols, clean


def fmt(name, acc, days, base_cost):
    saved = base_cost - acc.cost_eur
    wpd = acc.writes / days if days else 0
    return (f"{name:22} writes={acc.writes:>6} ({wpd:>5.0f}/day)  "
            f"exp={acc.exp_kwh:>6.1f}kWh imp={acc.imp_kwh:>6.1f}kWh  "
            f"penalty=€{acc.penalty_eur:>6.2f} import=€{acc.import_eur:>6.2f}  "
            f"cost=€{acc.cost_eur:>6.2f}  saved=€{saved:>6.2f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest curtailment policies on recorded HA data.")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--sweep", action="store_true", help="run the sensitivity sweep")
    a = ap.parse_args(argv)

    series = load_csv(a.csv)
    grid, cols, clean = build_grid(series)
    pv, load, price = cols[PV], cols[LOAD], cols[PRICE]
    clean_steps = sum(1 for i in range(len(grid)) if clean[i] and pv[i] is not None and price[i] is not None)
    days = clean_steps * DT / 86400.0
    ze = sum(1 for i in range(len(grid)) if clean[i] and price[i] is not None and decide(price[i], night=night_at(grid[i])) is not Action.NORMAL)
    print(f"clean usable: {clean_steps} steps = {days:.2f} days ; of which curtail-relevant "
          f"(ZERO_EXPORT/FULL): {ze*DT/86400.0:.2f} days\n")

    base = run(pv, load, price, clean, grid, "baseline")
    default = {"floor": 75.0, "ceil": 1200.0, "k": 2.0, "dwell_down": 20.0,
               "dwell_up": 150.0, "min_interval": 60.0}
    old = run(pv, load, price, clean, grid, "old")
    new = run(pv, load, price, clean, grid, "new", default)

    print(fmt("baseline (no curtail)", base, days, base.cost_eur))
    print(fmt("old (tight, 30s/2%)", old, days, base.cost_eur))
    print(fmt("new (default k=2)", new, days, base.cost_eur))

    if a.sweep:
        print("\n--- sensitivity sweep (new policy) ---")
        for ceil in (800.0, 1200.0, 1600.0):
            for k in (1.0, 2.0):
                for dwell_up in (60.0, 150.0, 300.0):
                    for floor in (50.0, 75.0, 150.0):
                        p = {"floor": floor, "ceil": ceil, "k": k, "dwell_down": 20.0,
                             "dwell_up": dwell_up, "min_interval": 60.0}
                        r = run(pv, load, price, clean, grid, "new", p)
                        print(fmt(f"ceil{ceil:.0f} k{k:.0f} up{dwell_up:.0f} fl{floor:.0f}", r, days, base.cost_eur))


if __name__ == "__main__":
    main()
