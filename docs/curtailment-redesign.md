# Curtailment redesign — price-scaled asymmetric-window control

## Why

Validated 2026-07-05: inverter register **40125 (active-power % derating) is non-volatile**
(a write survived a genuine cold boot). So **every Modbus write costs flash endurance** — the
current loop (rewrite on every ~100 W drift, 30 s cadence, + 100 % on every restart) is wearing
the inverter. Goal: **cut writes hard while keeping the economic benefit**, by only writing when
it's economically worth a write.

Also correct `PROJECT_PLAN.md`: the "derating is volatile, resets to 100 % on reboot" claim is
false — that reset was our own shutdown-writes-100 %, not the inverter. A reboot now leaves the
last-written derating **in place**, which also changes the fail-safe story (see §Safety).

## Economic quantities (per market slot)

BELPEX in €/MWh; prices in EURct/kWh. From `economics.py` (Frank Energie dynamic, Imewo):

```
p_inj = 1.15 − 0.1·BELPEX                                   # injection penalty (cost to export 1 kWh)
c_imp = (0.1068·BELPEX + 1.5)·1.06 + adders                 # import cost; adders = 11.494 day / 10.304 night
r     = clamp(p_inj / c_imp, 0, 1)                          # how expensive injecting is *relative to* importing
```

- `r → 0`: injecting nearly free vs importing → be lazy (wide export tolerance, few writes).
- `r → 1`: injecting as costly as importing → be precise (tight, more writes justified).

Actions unchanged: `consume_price<0` → FULL_CURTAIL (0 %); `feedin_price<0` (BELPEX<11.5) →
ZERO_EXPORT (windowed control below); else NORMAL (100 %).

## ZERO_EXPORT — the acceptable-export window `[L, U]`

Work in **watts of grid export** (export = −grid_net, positive = exporting). We do nothing while
export stays inside `[L, U]`; we only write when it leaves the window. The window is **asymmetric**
because the two edges have different economics:

```
L(r) = FLOOR_W · (1 − r)                       # import-side guard: tight (import always expensive)
U(r) = L(r) + (CEIL_MAX − L(r)) · (1 − r^k)    # export ceiling: wide when cheap, → L when expensive
Tset(r) = clamp(M_MAX · (1 − r), L, U)         # where we aim export when we DO write (interior, avoids re-trigger)
```

- **Floor `L`** (import guard) stays small/tight regardless of price — importing is expensive at
  every point in the band, so we always catch a drift toward import quickly. (Key correction from
  the symmetric-deadband version.)
- **Ceiling `U`** carries the price scaling — generous when injecting is cheap, collapsing toward
  the floor when injecting is expensive.
- `k` shapes the bend: `k=1` linear; `k=2` (or a hold-then-drop) keeps the ceiling wide until
  injecting gets *significantly* expensive, then drops — the "only when it matters" behaviour.

### Defaults (tunable via the sim)

| param | default | meaning |
|---|---|---|
| `FLOOR_W` | 75 W | import-guard export floor at r=0 |
| `CEIL_MAX` | 1200 W | export tolerance ceiling at r=0 |
| `M_MAX` | 400 W | aim-point (over-production) at r=0 |
| `k` | 2 | ceiling curve shape (hold-then-drop) |
| `DWELL_DOWN_S` | 20 s | sustain time before writing on the **import** side (react fast) |
| `DWELL_UP_S` | 300 s | sustain time before writing on the **export** side (don't rush) |
| `MIN_WRITE_INTERVAL_S` | 120 s | hard floor between any two writes |
| `WRITE_BUDGET_DAY` | 400 | backstop: hold setpoint if exceeded in a day (bug guard) |

**These are the tuned/shipping defaults** (`control.WindowController` / `controller.WRITE_BUDGET_DAY`).

### Backtest result (Feb–Jul 2026 price library, `scripts/backtest_price_library.py`)

7 clean sunny measured PV/load day-templates × 76 historical curtail-relevant price days
(incl. −499 €/MWh on 2026-05-01). Max saveable ≈ **€40/yr** (perfect zero-export).

| Policy | writes/yr | € saved/yr | % of max | worst-day writes |
|---|---|---|---|---|
| Old (tight 30 s / 2 %) | 35,925 | €38.47 | 96% | 536 |
| **Tuned window (shipping)** | **~5,000** | **€33.3** | **83%** | **188** |

The € is nearly flat across all configs (81–96%) — a few extreme-price days dominate the savings
and any sane policy curtails there — so tuning targets **minimum writes** while holding ~85% of a
~€40 pie. Chasing the last €5/yr would cost ~7× the writes: a bad trade against flash wear.

### Worked check

- **BELPEX 0** (cheap): `p_inj=1.15`, `c_imp=13.08`, `r≈0.09` → `L≈68 W`, `U≈1190 W`, `Tset≈365 W`.
  Clouds swing export 100–900 W → **no writes**; export dips below ~68 W → write (import guard).
- **BELPEX −80** (expensive): `p_inj=9.15`, `c_imp=4.03`, `r=1` → `L≈0`, `U≈0`, `Tset≈0`.
  Hold a razor edge at zero, both directions — writes justified at 9 ct/kWh.

## Per-tick algorithm (ZERO_EXPORT, curtailment enabled)

1. `r`, `L`, `U`, `Tset` from the slot's BELPEX.
2. `export = −grid_net` (P1); `load = pv + grid_net`.
3. Breach test with dwell:
   - `export < L` sustained ≥ `DWELL_DOWN_S` → **breach_low** (approaching import), or
   - `export > U` sustained ≥ `DWELL_UP_S` → **breach_high** (over-exporting), else **no write**.
4. If breach **and** `now − last_write ≥ MIN_WRITE_INTERVAL_S`:
   `cap_W = load + Tset`; `derating% = clamp(cap_W/P_MAX·100, 0, 100)`; write; reset dwell + `last_write`.
5. Increment persisted write counters (cumulative + today).

## Write-minimization everywhere else

- **NORMAL / FULL_CURTAIL**: write the setpoint (100 % / 0 %) **once on action change**, only if the
  current derating differs. No periodic rewrite.
- **Restart / shutdown restore-100**: **read first, write only if not already ~100 %** — kills the
  redundant 100 % write on every autodeploy restart.
- **Daily budget backstop**: if `writes_today > WRITE_BUDGET_DAY`, stop writing (hold last setpoint)
  and log — guards against a bug causing a write storm on persistent flash.

## Safety (changed by the persistence finding)

- A reboot **no longer restores 100 %**. If the controller crashes while curtailed and the inverter
  power-cycles, it stays curtailed. Mitigation: healthy controller in NORMAL guarantees 100 %;
  startup does read+correct. Residual risk noted; consider a periodic (rare) "if NORMAL and not 100,
  correct" sweep — but rare, to respect the flash.
- Manual override still reverts OFF on restart (unchanged).

## Code mapping

- `economics.py`: add `injection_penalty(belpex)`, `import_cost(belpex, night)` (rename of
  consume_price if useful), `relative_injection_cost(belpex,…) -> r`, and
  `curtail_window(belpex, night) -> (L, U, Tset)`.
- `control.py`: `compute_setpoint` takes `Tset`/`margin` instead of the fixed 200 W; add a
  `windowed_write_decision(export, L, U, dwell state, last_write) -> (should_write, cap%)` pure helper.
- `controller.py`: replace the fixed 2 % deadband + every-tick rewrite with the windowed decision +
  dwell timers + `MIN_WRITE_INTERVAL_S` + write counters; make the restore-100 paths read-first.
- Keep all decision logic **pure** so the sim imports the real functions (not a reimplementation).

---

# Simulation plan (on historical clean data)

## Objective

Quantify, on real recorded data, **writes** (flash-wear proxy) and **€** for each policy, and pick
the knob set that keeps most of the savings while cutting writes ~10×.

## Data

- Source: HA recorder SQLite, read-only (`scripts/ha_history_export.py`, opens `immutable=1`).
- Coverage available: **2026-06-25 → 07-05**.
- Entities: `..._pv_production` (available PV), `..._load` (true load), `..._grid_net_power`,
  `..._day_ahead_price` (BELPEX), `..._active_power_derating` (to find clean windows).
- **Clean-window selection**: use only spans where `active_power_derating ≡ 100 %` **and** the
  curtail switch was OFF **and** no manual/injection override — otherwise recorded PV is already
  curtailed and unusable as "available PV". (Exclude ~06-27→30 experiments and 07-05.) Verify by
  extracting the derating + switch histories first.
- Resample all series onto a common 10 s grid by forward-fill (step series).

## Replay model

For each timestep with `available_pv`, `load`, `belpex`:
- Decide action; run the policy → a **commanded** derating that only changes on writes.
- Apply inverter ramp (0.277 %/s, reuse `simulate_curtailment.py`): `cap_ramped` tracks commanded.
- `production = min(available_pv, cap_ramped)`; `grid_net = load − production`;
  `export = max(0,−grid_net)`, `import = max(0, grid_net)`.
- Accumulate energy × slot prices → cost; count writes.
- **Baseline (no curtail)**: `production = available_pv` (i.e. the recorded clean flows).

Built-in correctness check: in a clean window, reconstructed baseline `grid_net` must match the
recorded `grid_net` (since `grid_net = load − pv`). If it doesn't, the extract/align is wrong.

## Arms

1. **Baseline** — no curtailment (reference cost).
2. **Old policy** — margin 200 W fixed, 2 % (100 W) deadband, 30 s cadence, rewrite every cycle on drift.
3. **New policy** — asymmetric window; sweep `CEIL_MAX ∈ {800,1200,1600}`, `k ∈ {1,2}`,
   `DWELL_UP_S`, `FLOOR_W`.

## Metrics (per arm)

- total writes, writes/day, max writes/hour
- feed-in penalty avoided vs baseline (€)
- extra import caused (€, should be ≈0 — validates the floor guard)
- net € benefit
- wear projection: writes/day × 365 vs assumed endurance (get the flash cycle rating from Huawei)

## Deliverable

`scripts/simulate_from_history.py` (imports the real `economics`/`control` functions), printing a
writes-vs-€ table per arm + a trace for the worst (cloudy) day. Sweet spot = new-policy knob set with
€ ≈ old policy at a fraction of the writes.
