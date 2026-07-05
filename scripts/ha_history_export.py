"""Read-only extractor for the Home Assistant recorder SQLite DB.

Used to pull historical PV / grid / price series for offline curtailment simulation.
Opens the DB **immutable** (read-only, no locks) so it's safe to run while HA is live.

    # discover solar-related entities + their coverage
    sudo python3 scripts/ha_history_export.py --list

    # export series to CSV (long format: ts_iso,entity_id,value)
    sudo python3 scripts/ha_history_export.py \
        --export sensor.solar_pv_power,sensor.solar_grid_power,sensor.solar_belpex \
        --since 2026-06-28 --until 2026-07-05 --out /tmp/sim_week.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3

DEFAULT_DB = "/home/solarpi/smart_home/deploy/ha-config/home-assistant_v2.db"
PATTERNS = ("solar", "pv", "belpex", "grid", "derating", "curtail", "load", "export", "import")


def _connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?immutable=1", uri=True)


def _fmt(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def list_stats(db: str) -> None:
    """Show long-term (hourly) + short-term (5-min) statistics coverage for solar entities."""
    con = _connect(db)
    cur = con.cursor()
    where = " OR ".join("statistic_id LIKE ?" for _ in PATTERNS)
    metas = cur.execute(
        f"SELECT id, statistic_id FROM statistics_meta WHERE {where} ORDER BY statistic_id",
        tuple(f"%{p}%" for p in PATTERNS),
    ).fetchall()
    for tbl in ("statistics", "statistics_short_term"):
        print(f"=== {tbl} ({'hourly, long retention' if tbl=='statistics' else '5-min, ~10 days'}) ===")
        for mid, sid in metas:
            try:
                n, tmin, tmax = cur.execute(
                    f"SELECT COUNT(*), MIN(start_ts), MAX(start_ts) FROM {tbl} WHERE metadata_id=?",
                    (mid,),
                ).fetchone()
            except sqlite3.OperationalError:
                continue
            if n:
                print(f"  {sid:44} rows={n:>7}  {_fmt(tmin)} .. {_fmt(tmax)}")
    con.close()


def list_entities(db: str) -> None:
    con = _connect(db)
    cur = con.cursor()
    where = " OR ".join("entity_id LIKE ?" for _ in PATTERNS)
    rows = cur.execute(
        f"SELECT metadata_id, entity_id FROM states_meta WHERE {where} ORDER BY entity_id",
        tuple(f"%{p}%" for p in PATTERNS),
    ).fetchall()
    for mid, ent in rows:
        n, tmin, tmax = cur.execute(
            "SELECT COUNT(*), MIN(last_updated_ts), MAX(last_updated_ts) FROM states WHERE metadata_id=?",
            (mid,),
        ).fetchone()
        print(f"{ent:44} rows={n:>9}  {_fmt(tmin)} .. {_fmt(tmax)}")
    con.close()


def export(db: str, entities: list[str], since: str, until: str, out: str) -> None:
    con = _connect(db)
    cur = con.cursor()
    t0 = dt.datetime.fromisoformat(since).timestamp()
    t1 = dt.datetime.fromisoformat(until).timestamp()
    written = 0
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_iso", "entity_id", "value"])
        for ent in entities:
            row = cur.execute("SELECT metadata_id FROM states_meta WHERE entity_id=?", (ent,)).fetchone()
            if not row:
                print(f"WARN: {ent} not found")
                continue
            rows = cur.execute(
                "SELECT last_updated_ts, state FROM states "
                "WHERE metadata_id=? AND last_updated_ts BETWEEN ? AND ? ORDER BY last_updated_ts",
                (row[0], t0, t1),
            ).fetchall()
            for ts, state in rows:
                if state in (None, "unknown", "unavailable", ""):
                    continue
                w.writerow([dt.datetime.fromtimestamp(ts).isoformat(), ent, state])
                written += 1
            print(f"{ent}: {len(rows)} rows")
    con.close()
    print(f"wrote {written} rows -> {out}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Read-only HA recorder extractor.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--list", action="store_true", help="list solar-related entities + coverage")
    ap.add_argument("--stats", action="store_true", help="show long-term/short-term statistics coverage")
    ap.add_argument("--export", help="comma-separated entity_ids to export")
    ap.add_argument("--since", help="ISO date/datetime (inclusive)")
    ap.add_argument("--until", help="ISO date/datetime (inclusive)")
    ap.add_argument("--out", default="/tmp/ha_export.csv")
    a = ap.parse_args(argv)

    if a.stats:
        list_stats(a.db)
        return
    if a.list or not a.export:
        list_entities(a.db)
        return
    if not (a.since and a.until):
        ap.error("--export requires --since and --until")
    export(a.db, [e.strip() for e in a.export.split(",")], a.since, a.until, a.out)


if __name__ == "__main__":
    main()
