#!/usr/bin/env bash
# Auto-deploy the latest main onto the Pi. Run by smart_home-autodeploy.timer (~every 5 min).
#
# Flow: fetch main -> if it differs from the last commit actually deployed (see MARKER below),
# check it out, install deps, run the tests; only restart the controller if the tests PASS. On
# failure, roll the working tree back to the last-known-good commit so the Pi never runs untested
# code (and retries on the next tick once a fix is merged). Idempotent and safe to run by hand.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV=./.venv/bin
MARKER="$HOME/.smart_home-last-deployed"   # outside the repo -- survives git reset --hard
LOCK="$HOME/.smart_home-deploy.lock"

# The timer (~5 min) and a hand-run of this script can overlap -- without a lock they race on
# `git reset --hard` and the pip/build/restart sequence. Serialize: if another run is already
# deploying, skip rather than race it. Same pattern as home-automation and portfolio-app.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "autodeploy: another run is already in progress, skipping"
    exit 0
fi

git fetch --quiet origin main
PREV=$(git rev-parse @)
REMOTE=$(git rev-parse origin/main)
LAST_DEPLOYED=$(cat "$MARKER" 2>/dev/null || echo "")
# Compares against the last commit this script actually finished deploying, not local HEAD.
# A HEAD-vs-origin check is wrong whenever a commit is made AND pushed from this clone (i.e.
# working directly on the Pi): the push makes local HEAD equal origin/main instantly, so the
# check reads "nothing new" and silently skips the whole deploy -- no pip install, no pytest
# gate, no controller restart -- even though that commit has never been deployed. The marker
# only advances after a deploy actually succeeds, so pushing from here can't fake it.
# Matches home-automation/deploy/autodeploy.sh and portfolio-app/deploy/autodeploy.sh.
if [ "$LAST_DEPLOYED" = "$REMOTE" ]; then
    exit 0   # already deployed
fi
if ! git merge-base --is-ancestor "$PREV" "$REMOTE"; then
    # Local HEAD has commits origin/main doesn't (ahead or diverged, e.g. an unpushed local
    # commit) -- resetting here would silently discard them. Leave it alone and retry next tick.
    echo "autodeploy: local ${PREV:0:7} is ahead of/diverged from origin/main ${REMOTE:0:7} -- skipping to avoid discarding local work" >&2
    exit 0
fi
if ! git diff --quiet HEAD --; then
    # The ancestor check above only covers *committed* local work. `git reset --hard` discards
    # uncommitted changes to tracked files too, and unlike commits those have no reflog to
    # recover from -- so they are the more dangerous of the two. Untracked files are safe
    # (nothing here runs `git clean`).
    echo "autodeploy: uncommitted changes to tracked files -- skipping to avoid discarding them (commit or stash and this deploys normally)" >&2
    exit 0
fi

# deploy/docker-compose.yml passes these into the controller from THIS process's environment.
# If they are missing, `docker compose up` cheerfully recreates the container with blank
# credentials and it crash-loops -- which is exactly what a hand-run without the env did on
# 2026-08-14. The systemd unit supplies them via EnvironmentFile=/etc/smart_home.env; a manual
# run needs them sourced first. Fail here, before touching the working tree or the container,
# rather than discovering it from a restart loop:
#   set -a; source <(sudo cat /etc/smart_home.env); set +a; ./scripts/pi_autodeploy.sh
# MQTT_PORT is deliberately NOT required: it is absent from /etc/smart_home.env and the
# controller defaults to 1883, so demanding it would block every deploy including the timer's.
for v in P1_HOST HUAWEI_USER HUAWEI_PW MQTT_HOST MQTT_USER MQTT_PW NODE_ID; do
    if [ -z "${!v:-}" ]; then
        echo "autodeploy: \$$v is not set -- refusing to deploy (would recreate the controller with blank credentials). Run via 'sudo systemctl start smart_home-autodeploy.service', or source /etc/smart_home.env first." >&2
        exit 1
    fi
done

echo "autodeploy: ${LAST_DEPLOYED:0:7} -> ${REMOTE:0:7}"
git reset --hard origin/main
"$VENV/pip" install -q -e ".[dev,hw,mqtt]"
( cd deploy && docker compose build )
sudo cp deploy/smart_home-*.service deploy/smart_home-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

if "$VENV/pytest" -q; then
    # Controller runs containerized (restart: unless-stopped, no systemd wrapper -- same as
    # homeassistant/mosquitto); `up -d` recreates it against the image just rebuilt above.
    # smart_home-controller.service stays installed-but-disabled as the rollback path.
    ( cd deploy && docker compose --profile controller up -d controller )
    # Only now is this commit genuinely deployed -- record it so the next tick goes quiet. Written
    # last on purpose: a failure anywhere above leaves the marker stale, so the next tick retries.
    echo "$REMOTE" > "$MARKER"
    echo "autodeploy: tests passed -> controller restarted at ${REMOTE:0:7}"
else
    echo "autodeploy: TESTS FAILED at ${REMOTE:0:7} -> rolling back to ${PREV:0:7}" >&2
    git reset --hard "$PREV"
    "$VENV/pip" install -q -e ".[dev,hw,mqtt]"
    exit 1
fi
