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
  prices.py      # ENTSO-E day-ahead -> raw BELPEX -> daily Slot schedule
tests/           # offline unit tests
```

Stdlib only — no third-party runtime dependencies.

## Develop / test

```bash
pytest          # runs the offline suite (no network, no hardware)
```

## Print today's real schedule

Day-ahead prices come from the [ENTSO-E Transparency Platform]
(https://web-api.tp.entsoe.eu/api) (document type A44, bidding zone Belgium
`10YBE----------2`). You need a Web API security token. No `pip install` needed:

```bash
ENTSOE_API_TOKEN='your-token' python3 -m smart_home.prices
# (from the repo root; src is on the path via pyproject's pytest config, or use:)
ENTSOE_API_TOKEN='your-token' PYTHONPATH=src python3 -m smart_home.prices
```

Expected output — one row per slot (Brussels time) with BELPEX (EUR/MWh, taken verbatim),
all-in consume price, feed-in revenue, and the resulting action. Tomorrow's prices appear
after they're published (~13:00 CET).
