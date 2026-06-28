# smart_home — dynamic PV curtailment

Stop your solar inverter from **paying to export** when day-ahead electricity prices go
negative. This is a small, dependency-light controller for a **Huawei SUN2000** inverter on a
**day-ahead (dynamic) electricity contract**: it fetches tomorrow's prices from ENTSO-E, decides
per 15-minute slot whether exporting is worth it, and derates the inverter accordingly — with
live monitoring and manual controls in Home Assistant.

> ⚠️ **Safety / disclaimer.** This software writes to a **grid-tied inverter** using the
> installer account. You are responsible for your own equipment and for complying with your grid
> operator's rules. It ships fail-safe (any error, stale plan, or shutdown restores 100% output)
> and defaults to **monitoring only** — writes are off until you explicitly enable them — but it
> comes with **no warranty**. Use at your own risk.

See [`PROJECT_PLAN.md`](PROJECT_PLAN.md) for the full economic model and architecture, and
[`deploy/README.md`](deploy/README.md) for the step-by-step Raspberry Pi setup.

## What it does

Given the day-ahead `BELPEX` price (EUR/MWh) for each slot:

| Condition | Action |
|---|---|
| `consume_price < 0` (≈ BELPEX < −116) | **FULL_CURTAIL** — inverter off; the grid pays you to consume |
| `feedin_price < 0` (≈ BELPEX < 11.5) | **ZERO_EXPORT** — clip surplus, keep covering your own load |
| otherwise | **NORMAL** — export freely, earn the feed-in price |

Prices are fixed once published (~13:00 CET), so the whole next-day plan is built once daily and
cached; the control loop then runs entirely from the cached plan (no network in the hot path) and
fails safe to NORMAL if the plan ever goes stale.

## What you need

- **Raspberry Pi** with Ethernet + Wi-Fi (Pi 3B+/4/5), Raspberry Pi OS Lite 64-bit.
- **HomeWizard P1 meter** on your digital meter (for live net-grid power).
- **Huawei SUN2000** inverter reachable over Modbus (built-in Wi-Fi hotspot on the L1 series).
- **A day-ahead electricity contract** and a free **ENTSO-E API token**.

Full prerequisite list (accounts, how to enable the HomeWizard local API and inverter Modbus,
where to find the Wi-Fi PSK and installer password) is in [`deploy/README.md`](deploy/README.md).

## Getting started

```bash
git clone <this-repo> && cd smart_home
pip install -e ".[dev]"     # core is stdlib-only; dev adds pytest
pytest                       # offline suite, no network or hardware
```

Try it against real prices (just needs a token, no hardware):

```bash
ENTSOE_API_TOKEN='your-token' python3 -m smart_home.prices       # today's schedule, one row/slot
ENTSOE_API_TOKEN='your-token' python3 -m smart_home.schedule now  # action for the current slot
```

Read your hardware (read-only, never writes):

```bash
pip install -e ".[hw]"                       # huawei-solar, for the inverter
python3 -m smart_home.inverter               # inverter dump (defaults to 192.168.200.1:6607)
python3 -m smart_home.p1 <p1-ip>             # net grid power (− = exporting)
python3 -m smart_home.status --p1-host <p1-ip>  # both devices + a preview of each action's derating
```

Then deploy on the Pi (bootstrap script + systemd units + Home Assistant):
**[`deploy/README.md`](deploy/README.md)**.

## Control & monitoring (Home Assistant)

The controller publishes to MQTT with HA auto-discovery — live PV/grid/load, the day-ahead
forecast chart, and these controls (all default **off / safe**, and revert safely on restart):

- **Curtailment control** — master switch: run the automatic day-ahead plan, or just monitor.
- **Manual override** + **Manual derating %** — set a fixed inverter cap by hand.
- **Injection limit** + **Injection target (W)** — hold grid export at a chosen wattage.

The two manual modes are mutually exclusive and take precedence over the plan. A ready-to-paste
example dashboard is in [`deploy/ha-dashboard.yaml`](deploy/ha-dashboard.yaml) (gauges + forecast
chart) to get you started — adapt it to your own setup.

## Layout

```
src/smart_home/
  economics.py   # pure decision engine (BELPEX -> Action) + tariff model
  prices.py      # ENTSO-E day-ahead -> raw BELPEX -> daily Slot schedule
  schedule.py    # persisted whole-day plan (refresh daily, look up action_at(now))
  p1.py          # read-only HomeWizard P1 reader (net grid power)
  inverter.py    # Huawei SUN2000 reader over built-in WLAN 6607 (huawei-solar) [hw extra]
  modbus_tcp.py  # generic raw Modbus-TCP reader (RS485-bridge / port-502 fallback)
  control.py     # compute derating % from action + live measurements (pure)
  controller.py  # control daemon: apply plan -> inverter, fail-safe; publishes to MQTT
  mqtt.py        # Home Assistant MQTT discovery + state publisher [mqtt extra]
  status.py      # read-only pre-flight: live readings + setpoint preview (no writes)
tests/           # offline unit tests (no network, no hardware)
deploy/          # systemd units + env template + Pi deploy guide + example HA dashboard
scripts/         # bootstrap_pi.sh (post-flash Pi setup), simulate_curtailment.py
```

The core (`economics`/`prices`/`schedule`/`p1`) is **stdlib-only**. Inverter access adds
`huawei-solar` (`.[hw]`); MQTT adds `paho-mqtt` (`.[mqtt]`).

## Scope & assumptions

This was built for one specific setup, so adapting it elsewhere means editing a few spots:

- **Belgium / BELPEX.** `prices.py` queries the **BE bidding zone** (`10YBE----------2`). Change
  the zone for another market.
- **Tariff coefficients.** `economics.py` encodes a **Frank Energie / IMEWO** dynamic tariff
  (the feed-in and consume formulas, and the ~11.5 / −116 EUR/MWh thresholds derive from it).
  Swap in your own contract's coefficients.
- **Inverter.** Targets a **SUN2000-L1** with the built-in Wi-Fi hotspot (Modbus on
  `192.168.200.1:6607`). Other SUN2000 models work over Modbus but the connection details differ.
- **Meter.** Net grid power comes from a **HomeWizard P1**; `modbus_tcp.py` is a fallback for
  other meters.

## License

No license yet — currently **all rights reserved**. You're welcome to read it; ask before reusing.
(A permissive license like MIT can be added later if you want others to build on it.)
