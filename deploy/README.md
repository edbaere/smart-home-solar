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
