#!/bin/sh
# Bring up the local stand-in for the whole demo, for working on the console without Azure:
# the custom Temporal server with its worker controller, a Worker Deployment whose "compute
# provider" starts worker/local/session.py (one process per wake, stub model), all reporting
# to a console on 127.0.0.1:8080.
#
#   tools/local_harness.sh          # restart the server and register the deployment
#
# Needs the server build at /tmp/foundry/build/temporal-server-dev and the run directory at
# /tmp/foundry/run (see infra/server/build-server.sh and run-server.sh).
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUN=/tmp/foundry/run
PY="$REPO/worker/.venv/bin/python"
BUILD="${1:-local2}"

[ -f "$RUN/server.pid" ] && kill "$(cat "$RUN/server.pid")" 2>/dev/null || true
pkill -9 -f 'microsoft-foundry-demo/worker/main.py' 2>/dev/null || true
pkill -9 -f 'worker/local/session.py' 2>/dev/null || true
sleep 2
cd "$RUN"
TFD_CONSOLE_URL=http://127.0.0.1:8080 TFD_CONSOLE_TOKEN=local-token TFD_BUSY_LINGER_SECONDS=5 \
  PATH="/tmp/foundry/scripts/bin:$PATH" nohup /tmp/foundry/build/temporal-server-dev \
  --config-file config/development-sqlite.yaml --allow-no-auth start > logs/server.log 2>&1 &
echo $! > server.pid
for _ in 1 2 3 4 5 6 7 8 9 10; do
  temporal operator cluster health --address 127.0.0.1:7233 >/dev/null 2>&1 && break
  sleep 1
done
temporal operator namespace create -n default --address 127.0.0.1:7233 | tail -1
"$PY" "$REPO/scripts/setup_deployment.py" --deployment tfd --build-id "$BUILD" --provider subprocess \
  --provider-config "{\"command\": \"$PY\", \"args\": \"$REPO/console/tools/local_session.py,--build-id,$BUILD,--idle-timeout,60,--idle-exit,10\", \"timeout\": \"70s\"}" \
  --set-current | grep -E '^(created|set current|create )'
