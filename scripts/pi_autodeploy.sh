#!/usr/bin/env bash
# Auto-deploy the latest main onto the Pi. Run by smart_home-autodeploy.timer (~every 5 min).
#
# Flow: fetch main -> if changed, check it out, install deps, run the tests; only restart the
# controller if the tests PASS. On failure, roll the working tree back to the last-known-good
# commit so the Pi never runs untested code (and retries on the next tick once a fix is merged).
# Idempotent and safe to run by hand.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV=./.venv/bin

git fetch --quiet origin main
PREV=$(git rev-parse @)
REMOTE=$(git rev-parse origin/main)
if [ "$PREV" = "$REMOTE" ]; then
    exit 0   # nothing new
fi
if ! git merge-base --is-ancestor "$PREV" "$REMOTE"; then
    # Local HEAD has commits origin/main doesn't (ahead or diverged, e.g. an unpushed local
    # commit) -- resetting here would silently discard them. Leave it alone and retry next tick.
    echo "autodeploy: local ${PREV:0:7} is ahead of/diverged from origin/main ${REMOTE:0:7} -- skipping to avoid discarding local work" >&2
    exit 0
fi

echo "autodeploy: ${PREV:0:7} -> ${REMOTE:0:7}"
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
    echo "autodeploy: tests passed -> controller restarted at ${REMOTE:0:7}"
else
    echo "autodeploy: TESTS FAILED at ${REMOTE:0:7} -> rolling back to ${PREV:0:7}" >&2
    git reset --hard "$PREV"
    "$VENV/pip" install -q -e ".[dev,hw,mqtt]"
    exit 1
fi
