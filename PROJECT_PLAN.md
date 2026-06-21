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
  ENTSO-E API        ┌─────────────────────┐
  (A44 day-ahead) ──▶│  daily fetch job    │  runs ~16:00 (prices fixed once published)
   once/day          │  prices + economics │  builds the WHOLE next day's plan
                     └──────────┬──────────┘
                                ▼
                        schedule.json  (persisted plan: per-slot action)
                                │  action_at(now)  — no network in the hot path
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

- **Day-ahead prices are fixed once published** (~13:00 CET, visible by 16:00), so a
  **once-daily fetch job** builds the entire next-day plan and persists it (`schedule.json`,
  atomic write). The control loop runs purely from this cached plan — a slot lookup, no
  network in the hot path.
- **Resilience:** if ENTSO-E is unreachable later in the day, the cached plan keeps running.
  If the plan does not cover *now* (a fetch has failed too long), the control loop **fails
  safe to NORMAL**.
- **Python control daemon** owns the safety-critical loop (plan lookup → Modbus, plus the
  closed-loop zero-export PI controller using P1 feedback).
- **Home Assistant** (same Pi) for visualisation, history and alerts via native integrations.

---

## 3. Control surfaces

### HomeWizard P1 (sensor)
- Enable local API in the HomeWizard Energy app.
- `GET /api/v1/data` → `active_power_w` (**negative = injecting**), per-phase fields,
  `total_power_import_kwh`, `total_power_export_kwh`. Poll ~1 Hz.
- Target the **v2** HTTPS + bearer-token API (v1 deprecated on new firmware).

### Huawei SUN2000 (actuator) — built-in WLAN, Modbus **6607** ✅ validated live
**No SDongle, no Huawei meter, no installer Modbus-TCP toggle needed.** The inverter has no
SDongle; it joins FusionSolar via its built-in WLAN. Third-party Modbus is reachable on the
inverter's **Wi-Fi hotspot** (`SUN2000-<SN>` / `Changeme`) at **`192.168.200.1:6607`**, which
uses a proprietary handshake — so we use the **`huawei-solar`** library (`smart_home.inverter`),
not raw Modbus. (Raw Modbus-TCP 502 is closed; `smart_home.modbus_tcp` is kept for an
RS485-to-TCP bridge fallback.)

Deployment: the Pi **dual-homes** — `eth0` = home LAN (internet, P1, ENTSO-E),
`wlan0` = inverter hotspot (`ipv4.never-default`, so internet stays on Ethernet).

Confirmed live (SUN2000-4.6KTL-L1, `P_MAX = 5000 W`):
- **Reads are unauthenticated** — model, `active_power` (PV output, 32080), control state, etc.
- **Writes require an installer `login()`** (challenge/response, user `installer`) — confirmed
  working; `set()` returned True and read-back confirmed the change.
- Curtailment levers: **`active_power_percentage_derating` (40125, %)** and
  **`active_power_fixed_value_derating_w` (40126, W)**. The % register is written directly
  (no DTSU666 meter needed — the 47415 modes 5/6/7 *do* need a meter and are not used).
- ⚠️ **Ramp rate matters:** `DEFAULT_ACTIVE_POWER_CHANGE_GRADIENT` (47677) ≈ **0.277 %/s** by
  default → ~**3 min** for a 50% swing. Fine for 15-min price-slot curtailment; **too slow for
  closed-loop zero-export tracking** → raise the gradient (47677 is writable) in Phase 4.
- Zero-export setpoint (Phase 4): `setpoint_W = active_power(32080) + p1.active_power_w`.

---

## 4. Roadmap

1. **Read-only telemetry** — ✅ **done & validated live.** Inverter reads over WLAN 6607
   (`inverter.py`, huawei-solar); `p1.py` for the P1. Confirmed: Modbus reachable via built-in
   WLAN, writes unlocked with installer login, derating is effective but ramp-limited (0.277 %/s).
   *(P1 still to be read on-device once the Pi is placed.)*
2. **Price + economics engine + daily plan** — fetch ENTSO-E A44 day-ahead prices; compute
   `P_feedin`/`P_consume`; emit per-slot decision; persist the whole-day plan
   (`schedule.json`) refreshed once daily (~16:00 timer). Pure, unit-tested, no hardware.
   *(done)*
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
- [ ] ENTSO-E Web API token (`ENTSOE_API_TOKEN`) available on the Pi.
