"""Price-library backtest: replay measured PV/load day-templates against a library of historical
BELPEX days, so we can tune the curtailment knobs across REAL negative-price regimes (not just the
one calm recorded week). Reuses the shipping policy glue in `simulate_from_history`.

    PYTHONPATH=src python3 scripts/backtest_price_library.py \
        --measured /tmp/sim_raw.csv --prices /tmp/prices_150d.csv --sweep

Method: build clean sunny day-templates from the recorded PV/load; for each historical price day,
overlay its intraday BELPEX curve (by time-of-day) on each template and run the policy. Aggregate
writes + € across the whole price distribution, annualised, plus worst-day robustness.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt

import simulate_from_history as S
from smart_home.economics import Action, decide, feedin_price

S.DT = 30                       # 30 s grid — matches control cadence, keeps the sweep fast
BINS = 86400 // S.DT            # bins per day
P_MAX = S.P_MAX


def _bin(ts: float) -> int:
    d = dt.datetime.fromtimestamp(ts)
    return (d.hour * 3600 + d.minute * 60 + d.second) // S.DT


def _ffill_day(arr: list[float | None], fill0: bool) -> list[float]:
    out, last = [], (0.0 if fill0 else None)
    for v in arr:
        if v is not None:
            last = v
        out.append(last if last is not None else 0.0)
    return out


def build_templates(measured_csv: str) -> list[dict]:
    """Clean, sunny recorded days -> per-second-of-day PV/load templates."""
    series = S.load_csv(measured_csv)
    grid, cols, clean = S.build_grid(series)
    pv, load = cols[S.PV], cols[S.LOAD]
    by_date: dict[str, dict] = {}
    for i, t in enumerate(grid):
        if pv[i] is None or load[i] is None:
            continue
        day = dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        rec = by_date.setdefault(day, {"pv": [None] * BINS, "load": [None] * BINS,
                                       "clean": [True] * BINS, "peak": 0.0})
        b = _bin(t)
        rec["pv"][b], rec["load"][b] = pv[i], load[i]
        rec["clean"][b] = clean[i]
        rec["peak"] = max(rec["peak"], pv[i])
    templates = []
    for day, rec in sorted(by_date.items()):
        daylight = range(8 * BINS // 24, 20 * BINS // 24)          # 08:00..20:00
        seen = [b for b in daylight if rec["pv"][b] is not None]
        if not seen:
            continue
        clean_frac = sum(rec["clean"][b] for b in seen) / len(seen)
        if clean_frac > 0.95 and rec["peak"] > 2500:               # clean + genuinely sunny
            templates.append({"day": day, "peak": rec["peak"],
                              "pv": _ffill_day(rec["pv"], True), "load": _ffill_day(rec["load"], True)})
    return templates


def load_price_days(prices_csv: str) -> dict[str, list[float]]:
    """Historical BELPEX -> {date: per-bin price array (ffilled within the day)}."""
    raw: dict[str, list[float | None]] = {}
    with open(prices_csv) as fh:
        for row in csv.DictReader(fh):
            ts = dt.datetime.fromisoformat(row["ts_iso"])
            day = ts.strftime("%Y-%m-%d")
            b = (ts.hour * 3600 + ts.minute * 60) // S.DT
            raw.setdefault(day, [None] * BINS)[b] = float(row["belpex"])
    return {d: _ffill_day(a, False) for d, a in raw.items()}


def replay(template: dict, price_day: list[float], date_str: str, arm: str, p: dict | None):
    """Run one template under one price day; return the accounting Acc."""
    base = dt.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
    grid = [base + i * S.DT for i in range(BINS)]
    clean = [True] * BINS
    return S.run(template["pv"], template["load"], price_day, clean, grid, arm, p)


def curtail_relevant(price_day: list[float]) -> bool:
    return any(feedin_price(b) < 0 for b in price_day)


def build_baseline(templates, price_days):
    """Cache the no-curtail cost/penalty per (day, template) — arm-independent, so compute once."""
    days = [d for d, pd in price_days.items() if curtail_relevant(pd)]
    cache = {}
    for d in days:
        for ti, T in enumerate(templates):
            a = replay(T, price_days[d], d, "baseline", None)
            cache[(d, ti)] = (a.cost_eur, a.penalty_eur)   # penalty = max saveable that day
    return days, cache


def evaluate(templates, days, price_days, base_cache, arm, p, total_cal_days):
    """Annualised writes + €saved for one config, plus worst-day writes and extreme-day €."""
    day_writes, day_saved = [], []
    worst_writes, extreme_saved = 0, 0.0
    scale = 365.0 / total_cal_days
    for d in days:
        w_t, s_t = [], []
        for ti, T in enumerate(templates):
            a = replay(T, price_days[d], d, arm, p)
            base_cost = base_cache[(d, ti)][0]
            w_t.append(a.writes)
            s_t.append(base_cost - a.cost_eur)
        mw, ms = sum(w_t) / len(w_t), sum(s_t) / len(s_t)
        day_writes.append(mw)
        day_saved.append(ms)
        worst_writes = max(worst_writes, max(w_t))
        if d == "2026-05-01":
            extreme_saved = ms
    return {
        "annual_writes": sum(day_writes) * scale,
        "annual_saved": sum(day_saved) * scale,
        "worst_day_writes": worst_writes,
        "extreme_saved": extreme_saved,
    }


def annual_ceiling(templates, days, price_days, base_cache, total_cal_days):
    """Max saveable/yr = the full injection penalty a perfect zero-export would eliminate."""
    per_day = []
    for d in days:
        per_day.append(sum(base_cache[(d, ti)][1] for ti in range(len(templates))) / len(templates))
    return sum(per_day) * 365.0 / total_cal_days


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--measured", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args(argv)

    templates = build_templates(a.measured)
    price_days = load_price_days(a.prices)
    total_cal_days = len(price_days)
    days, base_cache = build_baseline(templates, price_days)
    ceiling = annual_ceiling(templates, days, price_days, base_cache, total_cal_days)
    print(f"templates (clean sunny days): {[t['day'] for t in templates]}")
    print(f"price library: {total_cal_days} days, {len(days)} curtail-relevant")
    print(f"max saveable/yr (perfect zero-export) = €{ceiling:.2f}\n")

    default = {"floor": 75.0, "ceil": 1200.0, "k": 2.0, "dwell_down": 20.0,
               "dwell_up": 150.0, "min_interval": 60.0}

    def show(name, arm, p=None):
        r = evaluate(templates, days, price_days, base_cache, arm, p, total_cal_days)
        pct = 100 * r["annual_saved"] / ceiling if ceiling else 0
        print(f"{name:26} writes/yr={r['annual_writes']:>7.0f}  saved/yr=€{r['annual_saved']:>6.2f} "
              f"({pct:>3.0f}% of max)  | worst-day writes={r['worst_day_writes']:>4}  05-01=€{r['extreme_saved']:>5.2f}")
        return r

    show("old (tight 30s/2%)", "old")
    show("new (default k2)", "new", default)

    if a.sweep:
        print("\n--- sweep (new policy, k=2) ---")
        for ceil in (600.0, 900.0, 1200.0, 1600.0):
            for dwell_up in (90.0, 150.0, 300.0):
                for mi in (60.0, 120.0):
                    p = {"floor": 75.0, "ceil": ceil, "k": 2.0, "dwell_down": 20.0,
                         "dwell_up": dwell_up, "min_interval": mi}
                    r = evaluate(templates, days, price_days, base_cache, "new", p, total_cal_days)
                    pct = 100 * r["annual_saved"] / ceiling if ceiling else 0
                    print(f"  ceil{ceil:>5.0f} up{dwell_up:>4.0f} mi{mi:>4.0f}: "
                          f"writes/yr={r['annual_writes']:>6.0f}  saved/yr=€{r['annual_saved']:>6.2f} "
                          f"({pct:>3.0f}%)  worst-day={r['worst_day_writes']:>3}  05-01=€{r['extreme_saved']:>5.2f}")


if __name__ == "__main__":
    main()
