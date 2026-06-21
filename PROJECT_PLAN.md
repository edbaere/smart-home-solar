# Smart Home — Dynamic PV Curtailment

Optimise a Huawei SUN2000-4.6KTL-L1 inverter against the Frank Energie **dynamic
(day-ahead)** electricity contract, so we never inject or produce when it costs money.

- **Inverter:** Huawei SUN2000-4.6KTL-L1 (single-phase residential), controlled via Modbus.
- **Grid meter:** HomeWizard P1 dongle — measures net exchange with the grid.
- **Contract:** Frank Energie dynamic, BELPEX-indexed. DSO: **Fluvius Imewo**.
- **Target host:** Raspberry Pi (`systemd` service + Home Assistant).

---

## 1. Economic model

All values in **EURct/kWh**. `BELPEX` is the hourly (→ quarter-hourly) day-ahead price in €/MWh.

### Feed-in revenue (export 1 kWh)
```
P_feedin = 0.1 × BELPEX − 1.150            # VAT-exempt
```

### Consumption cost (import 1 kWh)
```
P_consume = (0.1068 × BELPEX + 1.500) × 1.06    # energy, incl. 6% VAT
          + 1.166      # GSC
          + 0.371      # WKK
          + 5.0328     # bijzondere accijns (VAT-exempt)
          + 0.2042     # bijdrage op energie (VAT-exempt)
          + 4.72       # grid "Afname Normaal" — Imewo, digital meter w/ peak metering
                       # (night "Excl. nacht" = 3.53)
```
Non-energy adders ≈ **11.494** EURct/kWh (day) / **10.304** (night).

### Decision rules (evaluate per market slot, in this order)

| Order | Condition | Action | Threshold (day) |
|---|---|---|---|
| 1 | `P_consume < 0` | **FULL_CURTAIL** — inverter active power = 0; grid pays us to consume | BELPEX < ≈ −116 €/MWh |
| 2 | `P_feedin < 0`  | **ZERO_EXPORT** — throttle PV so net grid exchange ≈ 0; still self-consume | BELPEX < 11.5 €/MWh |
| 3 | else | **NORMAL** — self-consume + export surplus, earn `P_feedin` | — |

> Self-consumption always beats export (it saves the full, taxed `P_consume` vs. the
> market-only `P_feedin`), so we only ever curtail the **surplus** (Rule 2) — except when
> importing is itself profitable (Rule 1), where we shut production entirely.

---

## 2. Architecture — Hybrid

```
                ┌────────────────────────┐
  Frank API ───▶│  price + economics     │  fetch_prices.py wrapper
 (day-ahead)    │  engine (pure Python)   │  → per-slot decision schedule
                └───────────┬─────────────┘
                            │ decision (FULL_CURTAIL / ZERO_EXPORT / NORMAL)
                            ▼
   HomeWizard P1 ─────▶┌──────────────────┐──────▶ Huawei SUN2000
   net power (1 Hz)    │  control daemon  │ Modbus  active-power % (reg ~40125)
                       │  (PI loop +      │  TCP    (volatile setpoint)
                       │   watchdog)      │
                       └──────────────────┘
                            │ telemetry
                            ▼
                    Home Assistant  (dashboards, history, alerting)
                  native HomeWizard + wlcrs/huawei_solar integrations
```

- **Python control daemon** owns the safety-critical loop (prices → decision → Modbus,
  plus the closed-loop zero-export PI controller using P1 feedback).
- **Home Assistant** (same Pi) for visualisation, history and alerts via native integrations.

---

## 3. Control surfaces

### HomeWizard P1 (sensor)
- Enable local API in the HomeWizard Energy app.
- `GET /api/v1/data` → `active_power_w` (**negative = injecting**), per-phase fields,
  `total_power_import_kwh`, `total_power_export_kwh`. Poll ~1 Hz.
- Target the **v2** HTTPS + bearer-token API (v1 deprecated on new firmware).

### Huawei SUN2000 (actuator) — Modbus via `wlcrs/huawei_solar`
- Modbus-TCP via SDongle (port 502, slave 1) or RS485.
- **Active power % derating** (holding reg ~40125, gain ×10): `0` = full shutdown;
  modulate for zero-export.
- No Huawei DTSU666 meter present → inverter can't self-limit export. **Zero-export is
  done in software** (P1 feedback → PI loop, ~2–5 s, small import deadband).
- ⚠️ **Verify:** Modbus *write* may require "Modbus TCP (unrestricted)" enabled via the
  installer / FusionSolar commissioning. Confirm read first, then write.

---

## 4. Roadmap

1. **Read-only telemetry** — P1 + inverter Modbus read; confirm register map; log net & PV power.
2. **Price + economics engine** — wrap `fetch_prices.py`; compute `P_feedin`/`P_consume`;
   emit per-slot decision. Pure, unit-tested, no hardware.
3. **Actuation (fail-safe first)** — write active-power %; watchdog forces 100% on
   crash/stale-price/exception; test Rule 1 (FULL_CURTAIL on/off).
4. **Zero-export loop** — closed-loop PI using P1 feedback for Rule 2.
5. **Home Assistant + dashboards + alerting**; deploy as `systemd` service on the Pi.

---

## 5. Safety principles

- **Fail open (no curtailment):** any failure → restore 100% active power. Never leave the
  inverter stuck at 0.
- Active-power setpoint is **volatile** (resets to 100% on inverter reboot) — good fail-safe.
- **Rate-limit** Modbus writes; deadband + hysteresis on the PI loop to avoid oscillation.
- All times in **`Europe/Brussels`** with DST handling.
- Plan for **15-minute** slots (BE market granularity + quarter-hour metering).

---

## 6. Open items to verify

- [ ] Modbus **write** access on this inverter/SDongle firmware.
- [ ] HomeWizard P1 API version (v1 vs v2) + token.
- [ ] Confirm exact Huawei active-power register/mode for the L1 series.
- [ ] Confirm Imewo day vs night grid tariff handling (and capacity/peak tariff impact when
      doing FULL_CURTAIL — avoid setting a new monthly import peak).
- [ ] Frank API auth (`FRANK_ENERGIE_EMAIL` / `FRANK_ENERGIE_PASSWORD`) on the Pi.
