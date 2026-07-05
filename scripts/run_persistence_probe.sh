#!/usr/bin/env bash
# Run the 40125 persistence probe on the Pi with a clean quiesce + guaranteed
# "business as usual" restore.
#
# What it does:
#   1. Records which of the inverter-touching services are currently active.
#   2. Stops them (the inverter allows ~one Modbus client, so the probe must be sole client).
#   3. Masks the autodeploy timer so it can't restart the controller mid-test.
#   4. Runs scripts/probe_register_persistence.py (interactive — you power-cycle the inverter).
#   5. On EXIT (success, error, or Ctrl-C): unmasks the timer and restarts exactly the
#      services that were running before. The probe itself already restores the inverter
#      to 100%, and a restarted controller re-asserts 100%/plan — belt and suspenders.
#
# Run interactively so the probe can prompt you:
#     ssh -t solarpi@<pi>   # -t = allocate a TTY
#     cd ~/smart_home && sudo -v && ./scripts/run_persistence_probe.sh
set -euo pipefail

ENV_FILE=/etc/smart_home.env
VENV_PY=/home/solarpi/smart_home/.venv/bin/python
PROBE=/home/solarpi/smart_home/scripts/probe_register_persistence.py
WRITERS=(smart_home-controller.service smart_home-publisher.service)
TIMER=smart_home-autodeploy.timer

# --- record prior state so we restore EXACTLY what was running -----------------
declare -a WAS_ACTIVE=()
for svc in "${WRITERS[@]}"; do
    if systemctl is-active --quiet "$svc"; then WAS_ACTIVE+=("$svc"); fi
done
TIMER_WAS_ACTIVE=0
systemctl is-active --quiet "$TIMER" && TIMER_WAS_ACTIVE=1 || true

restore() {
    echo
    echo "--- restoring business as usual ---"
    sudo systemctl unmask "$TIMER" 2>/dev/null || true
    if [[ "$TIMER_WAS_ACTIVE" == "1" ]]; then
        sudo systemctl start "$TIMER" || echo "WARN: failed to start $TIMER"
    fi
    for svc in "${WAS_ACTIVE[@]}"; do
        sudo systemctl start "$svc" || echo "WARN: failed to start $svc"
    done
    echo "restored: timer=$([[ $TIMER_WAS_ACTIVE == 1 ]] && echo up || echo down), services=[${WAS_ACTIVE[*]:-none}]"
    echo "Tip: 'systemctl status ${WRITERS[0]}' to confirm; the controller re-asserts 100%/plan on startup."
}
trap restore EXIT

echo "Prior state: active services=[${WAS_ACTIVE[*]:-none}], autodeploy timer active=$TIMER_WAS_ACTIVE"

# --- quiesce -------------------------------------------------------------------
echo "Masking $TIMER and stopping inverter clients…"
sudo systemctl mask --now "$TIMER" 2>/dev/null || sudo systemctl stop "$TIMER" || true
for svc in "${WRITERS[@]}"; do sudo systemctl stop "$svc" || true; done
sleep 2   # let sockets close so the probe gets a clean connection

# --- run the probe (sources HUAWEI_PW from the env file) -----------------------
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a
echo "Launching probe…"
"$VENV_PY" "$PROBE" "$@"

# restore() runs on EXIT via the trap.
