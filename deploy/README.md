# Deploy (Raspberry Pi)

The Pi runs two pieces:
- **`smart_home-refresh.timer`** → daily at 16:00, fetches ENTSO-E day-ahead prices and writes
  the whole-day plan to `~/.smart_home/schedule.json`.
- **`smart_home-controller.service`** → the always-on loop that applies the plan to the
  inverter (fail-safe to 100% on any error / stale plan / shutdown).

## One-time setup

```bash
# 1. software + venv + wifi (run on the Pi, in the repo)
./scripts/bootstrap_pi.sh

# 2. connect wlan0 -> inverter AP (PSK not echoed)
sudo nmcli device wifi connect "SUN2000-HV2310422608" password '<PSK>' ifname wlan0
sudo nmcli connection modify "SUN2000-HV2310422608" ipv4.never-default yes ipv4.route-metric 700

# 3. secrets
sudo cp deploy/smart_home.env.example /etc/smart_home.env
sudo nano /etc/smart_home.env            # fill in ENTSOE_API_TOKEN, HUAWEI_PW, P1_HOST
sudo chown root:root /etc/smart_home.env && sudo chmod 600 /etc/smart_home.env

# 4. install + enable units
sudo cp deploy/smart_home-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart_home-refresh.timer
sudo systemctl start smart_home-refresh.service     # build the first plan now
sudo systemctl enable --now smart_home-controller.service
```

## Operate

```bash
systemctl status smart_home-controller
journalctl -u smart_home-controller -f          # live control decisions
systemctl list-timers smart_home-refresh.timer
```

**Before enabling the controller, dry-run it** (computes + logs, never writes):

```bash
set -a; source /etc/smart_home.env; set +a
.venv/bin/python -m smart_home.controller --p1-host "$P1_HOST" --dry-run --once
```

## Monitoring stack (Home Assistant + MQTT)

`docker-compose.yml` runs **Mosquitto** + **Home Assistant** (host networking).
`smart_home-publisher.service` runs the controller in **`--dry-run`** (never writes) so HA
shows live data before actuation is enabled. Telemetry (PV / grid / load + phases) is read and
published at **1 Hz** by default (`--telemetry-interval`); the curtailment decision still only
ticks every `--interval` (30 s). Lower the rate (`--telemetry-interval 2`) if the inverter
hotspot link is flaky.

> **HA recorder:** 1 Hz × ~12 sensors is a lot of rows. If the SQLite DB grows uncomfortably,
> add a `recorder:` block in HA `configuration.yaml` — e.g. `commit_interval: 5` and a short
> `purge_keep_days` — or exclude the high-rate power sensors from long-term recording. Power
> sensors keep their hourly long-term statistics regardless.

```bash
cd deploy && docker compose up -d                       # Mosquitto + Home Assistant
sudo cp deploy/smart_home-publisher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now smart_home-publisher
```

In HA (`http://<pi-ip>:8123`): onboard → **Settings → Devices & Services → Add Integration →
MQTT** → broker `127.0.0.1`, port `1883`, no auth. The "Smart Home Curtailment" device and its
entities appear automatically (retained discovery). Paste `deploy/ha-dashboard.yaml` into a new
dashboard's raw config for ready-made gauges/graphs.

> If your HA was first onboarded **before** the `object_id` discovery field (commit 44524c1),
> its entities are named `sensor.smart_home_curtailment_*` and `ha-dashboard.yaml` (which uses
> `sensor.solar_*`) shows no data. Use `deploy/ha-dashboard.legacy-ids.yaml` instead, or rename
> the entities in the HA UI to the `solar_*` ids.

**Going live later — HA switch:** the controller exposes a **"Curtailment control" switch**
(`switch.solar_curtail_enable`, or `switch.smart_home_curtailment_curtailment_control` on
legacy-id installs). It gates whether the plan is actually written to the inverter:

- **OFF (default, safe):** plan + decisions are computed and published exactly as in dry-run,
  but nothing is written — the inverter is held at full power. The state persists across
  restarts (`~/.smart_home/curtail_enabled`) and defaults OFF, so a reboot never silently
  curtails.
- **ON:** the planned derating is executed.

To make the switch effective you must run the controller **write-capable** (not `--dry-run`),
which needs the installer password:

```bash
# 1. add the inverter installer password to the env (required for any write)
echo 'HUAWEI_PW=<installer-password>' | sudo tee -a /etc/smart_home.env
# 2. swap the dry-run publisher for the write-capable, switch-gated controller
sudo systemctl disable --now smart_home-publisher
sudo cp deploy/smart_home-controller.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now smart_home-controller
```

`smart_home-controller.service` loads `/etc/smart_home.env` (so it gets `HUAWEI_PW`, `MQTT_HOST`,
`P1_HOST`) and publishes the same telemetry + switch. With the switch OFF, behaviour is identical
to the dry-run publisher; flip it ON in HA when you're ready to actuate. `--dry-run` remains a
hard override that disables writes (and hides the switch) regardless.

**Manual derating override:** a **"Manual override" switch** + **"Manual derating %" number**
let you set the inverter derating by hand from HA. When the override is ON the controller writes
that % directly and **ignores the plan** (full precedence, regardless of the curtailment switch);
turn it OFF to return to automatic. The override **reverts to OFF on restart** (the % is
remembered), so a reboot can't strand the inverter at a manual value. Changes apply within one
control tick (~30 s). Like all writes, it needs the controller running write-capable (`HUAWEI_PW`,
not `--dry-run`).

**Manual injection (export) limit:** an **"Injection limit" switch** + **"Injection target (W)"
number** cap grid export at a chosen wattage. When ON the controller closed-loops the inverter
each tick (output ≈ `load + target`) so export holds at the target — e.g. target 0 W = zero
export, 1500 W = export up to 1.5 kW. It overrides the plan and is **mutually exclusive** with the
manual-derating override (turning one on turns the other off). Same restart behaviour (override
reverts to OFF, target W remembered). HA shows action **`INJECTION`** while active.
