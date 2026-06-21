# smart_home

Dynamic PV curtailment for a **Huawei SUN2000-4.6KTL-L1** inverter on a **Frank Energie
day-ahead** electricity contract. Avoids injecting (or producing) when it would cost money.

See [`PROJECT_PLAN.md`](PROJECT_PLAN.md) for the economic model, architecture, and roadmap.

## Decision model (per market slot)

Given the day-ahead `BELPEX` (EUR/MWh):

| Condition | Action |
|---|---|
| `consume_price < 0` (≈ BELPEX < −116) | **FULL_CURTAIL** — inverter off; grid pays us to consume |
| `feedin_price < 0` (BELPEX < 11.5) | **ZERO_EXPORT** — clip surplus, still self-consume |
| otherwise | **NORMAL** — export surplus, earn the feed-in price |

## Layout

```
src/smart_home/
  economics.py   # pure decision engine (BELPEX -> Action)
  prices.py      # Frank Energie -> raw BELPEX -> daily Slot schedule
tests/           # offline unit tests
```

## Develop / test

```bash
pytest          # runs the offline suite (no network, no hardware)
```

## Print today's real schedule

Needs your Frank Energie login. Run in a terminal (keeps credentials out of any transcript):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[frank]"
FRANK_ENERGIE_EMAIL='you@example.com' \
FRANK_ENERGIE_PASSWORD='...' \
python -m smart_home.prices
```

Expected output — one row per hour (Brussels time) with BELPEX, all-in consume price,
feed-in revenue, and the resulting action.
