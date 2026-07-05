"""Scan historical BE day-ahead prices to quantify curtailment opportunity + find candidate
low-price days for the backtest price-library. Read-only; needs ENTSOE_API_TOKEN.

    PYTHONPATH=src ENTSOE_API_TOKEN=... python3 scripts/scan_prices.py --days 120
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

from smart_home.economics import Action, decide, feedin_price
from smart_home.prices import build_schedule, default_is_night, fetch_raw_prices


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--csv-out", help="dump every fetched slot as ts_iso,belpex to this path")
    a = ap.parse_args(argv)
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        print("Set ENTSOE_API_TOKEN", file=sys.stderr)
        sys.exit(1)

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=a.days)
    raw = fetch_raw_prices(token, start, end)
    sched = build_schedule(raw)
    print(f"fetched {len(sched)} slots {start}..{end}")

    if a.csv_out:
        import csv as _csv
        with open(a.csv_out, "w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["ts_iso", "belpex"])
            for s in sched:
                w.writerow([s.start, s.belpex])
        print(f"wrote {len(sched)} slots -> {a.csv_out}")

    per_day: dict[str, list] = defaultdict(list)
    for s in sched:
        per_day[s.start[:10]].append(s)

    n_ze = sum(1 for s in sched if s.action is Action.ZERO_EXPORT)
    n_fc = sum(1 for s in sched if s.action is Action.FULL_CURTAIL)
    n_neg = sum(1 for s in sched if s.belpex < 0)
    print(f"slots: ZERO_EXPORT={n_ze} FULL_CURTAIL={n_fc} negative-price={n_neg} "
          f"({100*n_ze/len(sched):.1f}% of slots would curtail)")

    # candidate days ranked by curtailment relevance (most ZE/FC slots)
    ranked = sorted(
        per_day.items(),
        key=lambda kv: sum(1 for s in kv[1] if s.action is not Action.NORMAL),
        reverse=True,
    )
    print("\ntop curtail-relevant days (day: #curtail-slots, min BELPEX, min feedin ct/kWh):")
    for day, slots in ranked[:15]:
        nz = sum(1 for s in slots if s.action is not Action.NORMAL)
        if not nz:
            break
        mb = min(s.belpex for s in slots)
        print(f"  {day}: {nz:>3} slots  min BELPEX {mb:>7.1f}  min feedin {feedin_price(mb):>6.2f}")
    days_with_curtail = sum(1 for _, slots in ranked if any(s.action is not Action.NORMAL for s in slots))
    print(f"\n{days_with_curtail}/{len(per_day)} days had at least one curtail slot")


if __name__ == "__main__":
    main()
