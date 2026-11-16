#!/bin/sh
# Release the current worker code to every booth station, then the console.
#   TFD_STATE=<private state dir> TFD_STATIONS=3 scripts/release.sh
#
# A station is one Worker Deployment, one task queue, one Foundry agent and one console, so
# that two booth laptops never share a sandbox. Station 1 is deployment "tfd" at the site
# root; station N is "tfd-N" under /N/. For each station this deploys a new Foundry agent
# (a new Temporal build ID), then updates the consoles, then makes the new builds current.
# Workflows already running stay pinned to the build they started on.
#
# Run it when no run is working. It takes about two minutes per station.
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
STATIONS="${TFD_STATIONS:-1}"
IDLE_TIMEOUT="${TFD_IDLE_TIMEOUT:-120}"
# The cooloff has to be longer than a wake-up takes. With 5 s, a backlog check that fell into
# a slow wake-up asked Foundry for a second sandbox (docs/operations.md, "Scaler settings").
SCALER='{"scale_up_cooloff_ms": 15000, "metrics_poll_interval_ms": 30000}'
PY="$REPO/worker/.venv/bin/python"
. "$TFD_STATE/env.sh"; . "$TFD_STATE/vm.env"
export PROJECT_ENDPOINT

(cd "$REPO/worker" && uv export --no-dev --no-hashes --no-emit-project -o requirements.txt >/dev/null 2>&1)

N=1
while [ "$N" -le "$STATIONS" ]; do
  if [ "$N" = 1 ]; then NAME="tfd"; FILE="$TFD_STATE/agent.env"; URL="https://$VM_FQDN"
  else NAME="tfd-$N"; FILE="$TFD_STATE/agent-$N.env"; URL="https://$VM_FQDN/$N"; fi
  echo "station $N: deploying the worker for $NAME"
  "$PY" "$HERE/deploy_agent.py" --idle-timeout "$IDLE_TIMEOUT" --env-file "$TFD_STATE/worker-booth.env" \
    --deployment "$NAME" --env "TFD_CONSOLE_URL=$URL" > "$FILE.new"
  grep -q '^AGENT_NAME=' "$FILE.new" && grep -q '^BUILD_ID=' "$FILE.new"
  mv "$FILE.new" "$FILE"
  N=$((N + 1))
done

echo "consoles"
TFD_STATE="$TFD_STATE" "$REPO/infra/vm/deploy_console.sh"

set -a; . "$TFD_STATE/client.env"; set +a
N=1
while [ "$N" -le "$STATIONS" ]; do
  if [ "$N" = 1 ]; then NAME="tfd"; FILE="$TFD_STATE/agent.env"; else NAME="tfd-$N"; FILE="$TFD_STATE/agent-$N.env"; fi
  AGENT_NAME="$(. "$FILE"; echo "$AGENT_NAME")"; BUILD_ID="$(. "$FILE"; echo "$BUILD_ID")"
  echo "station $N: $NAME build $BUILD_ID on $AGENT_NAME"
  "$PY" "$HERE/setup_deployment.py" --deployment "$NAME" --build-id "$BUILD_ID" --provider azure-foundry \
    --provider-config "{\"project_endpoint\": \"$PROJECT_ENDPOINT\", \"agent_name\": \"$AGENT_NAME\"}" \
    --scaler-config "$SCALER" --set-current | grep -E "set current|build_id|STATUS" | head -4
  N=$((N + 1))
done
echo "check each station: TFD_BASE=https://$VM_FQDN[/N] TFD_CODE=... $PY scripts/booth_check.py"
