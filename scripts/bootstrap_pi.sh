#!/usr/bin/env bash
# Post-flash setup for the smart_home Pi. Run ON the Pi, from the repo root,
# after the repo has been cloned:
#     ./scripts/bootstrap_pi.sh
# Idempotent: safe to re-run. Does NOT handle secrets or wlan0 PSK (see deploy/README.md).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Inverter Wi-Fi hotspot SSID — printed on the inverter label / shown in the FusionSolar app.
# Pass it as the first arg, e.g. ./scripts/bootstrap_pi.sh "SUN2000-HVxxxxxxxxxx"
SSID="${1:-SUN2000-HVxxxxxxxxxx}"

echo "[1/4] apt packages (git, python3-venv)"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv

echo "[2/4] python venv + dependencies ([dev,hw,mqtt])"
cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev,hw,mqtt]"

echo "[3/4] wifi country (BE) + radio"
sudo raspi-config nonint do_wifi_country BE || true
sudo rfkill unblock wifi || true

echo "[4/4] tests"
.venv/bin/pytest -q

cat <<EOF

Bootstrap done. Next:
  * connect wlan0 to the inverter:
      sudo nmcli device wifi connect '$SSID' password '<PSK>' ifname wlan0
      sudo nmcli connection modify '$SSID' ipv4.never-default yes ipv4.route-metric 700
  * install secrets + systemd units: see deploy/README.md
EOF
