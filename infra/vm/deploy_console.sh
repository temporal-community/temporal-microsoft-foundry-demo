#!/bin/sh
# Install or update the booth console on the demo VM, and the proxy routes in front of it.
#   TFD_STATE=<private state dir> ./deploy_console.sh
# Packs console/, carries it to the VM through the storage account, and applies it with
# `az vm run-command` (the VM has no SSH). The generated script embeds secrets and is deleted.
#
# One console runs per booth station, so that two laptops do not share sandboxes. Station 1
# is the site root and reads agent.env. Station N (2 to 9) is served under /N/ and exists when
# the state directory has agent-N.env; its task queue and Worker Deployment are tfd-N.
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
. "$TFD_STATE/env.sh"; . "$TFD_STATE/vm.env"
# The storage account install_server.sh created to carry files to the VM.
if [ -f "$TFD_STATE/storage.env" ]; then . "$TFD_STATE/storage.env"; fi
: "${STORAGE:?no storage account: run infra/vm/install_server.sh first, or set STORAGE}"
umask 077
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

STATIONS="1"
for N in 2 3 4 5 6 7 8 9; do
  if [ -f "$TFD_STATE/agent-$N.env" ]; then STATIONS="$STATIONS $N"; fi
done

tar -C "$REPO" -czf "$WORK/console.tgz" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude 'screenshots' --exclude 'state' --exclude 'tests' --exclude 'tools' console
KEY="$(az storage account keys list --subscription "$SUB" -g "$RG" -n "$STORAGE" --query '[0].value' -o tsv)"
az storage blob upload --account-name "$STORAGE" --account-key "$KEY" -c bin -n console.tgz \
  -f "$WORK/console.tgz" --overwrite --no-progress -o none
EXPIRY="$(date -u -v+1H +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d '+1 hour' +%Y-%m-%dT%H:%MZ)"
SAS="$(az storage blob generate-sas --account-name "$STORAGE" --account-key "$KEY" -c bin \
  -n console.tgz --permissions r --expiry "$EXPIRY" --https-only -o tsv)"
# The questions offered on the page: TFD_PRESETS, or presets.txt in the state directory
# (separated by |).
PRESETS="${TFD_PRESETS:-$(cat "$TFD_STATE/presets.txt" 2>/dev/null || true)}"
TOKEN="$(cat "$TFD_STATE/certs/token")"

# The proxy config: host and token filled in, and one route per extra station.
: > "$WORK/routes"
for N in $STATIONS; do
  [ "$N" = 1 ] && continue
  printf '\tredir /%s /%s/\n\thandle_path /%s/* {\n\t\treverse_proxy 127.0.0.1:809%s\n\t}\n' \
    "$N" "$N" "$N" "$N" >> "$WORK/routes"
done
awk -v routes="$WORK/routes" '
  /# STATION_ROUTES/ { while ((getline line < routes) > 0) print line; next }
  { print }' "$HERE/Caddyfile" |
  sed -e "s/{\$TFD_HOST}/$VM_FQDN/" -e "s/{\$TFD_TOKEN}/$TOKEN/" > "$WORK/Caddyfile"

cat > "$WORK/apply.sh" <<EOF
#!/bin/bash
set -euo pipefail
id tfdconsole >/dev/null 2>&1 || useradd --system --home /var/lib/tfd-console --create-home --shell /usr/sbin/nologin tfdconsole
mkdir -p /opt/tfd-console /var/lib/tfd-console
curl -fsSL -o /tmp/console.tgz 'https://$STORAGE.blob.core.windows.net/bin/console.tgz?$SAS'
rm -rf /opt/tfd-console/console.new && mkdir -p /opt/tfd-console/console.new
tar -C /opt/tfd-console/console.new -xzf /tmp/console.tgz --strip-components=1 && rm -f /tmp/console.tgz
rm -rf /opt/tfd-console/app.old; [ -d /opt/tfd-console/app ] && mv /opt/tfd-console/app /opt/tfd-console/app.old
mv /opt/tfd-console/console.new /opt/tfd-console/app
cd /opt/tfd-console/app
UV_PROJECT_ENVIRONMENT=/opt/tfd-console/venv UV_PYTHON_INSTALL_DIR=/opt/tfd-console/python /usr/local/bin/uv sync --frozen --no-dev --quiet
EOF

UNITS=""
for N in $STATIONS; do
  if [ "$N" = 1 ]; then
    SFX=""; QUEUE="tfd"; PORT=8080; AGENT_FILE="$TFD_STATE/agent.env"
  else
    SFX="-$N"; QUEUE="tfd-$N"; PORT="809$N"; AGENT_FILE="$TFD_STATE/agent-$N.env"
  fi
  STATION_AGENT="$(. "$AGENT_FILE"; echo "$AGENT_NAME")"
  UNITS="$UNITS tfd-console$SFX:$PORT"
  cat >> "$WORK/apply.sh" <<EOF
cat > /etc/tfd-console$SFX.env <<'TFD_EOF'
TEMPORAL_ADDRESS=127.0.0.1:7233
TEMPORAL_NAMESPACE=default
TFD_TASK_QUEUE=$QUEUE
TFD_ACCESS_CODE=$(cat "$TFD_STATE/certs/access-code")
TFD_INTERNAL_TOKEN=$TOKEN
TFD_SESSION_SECRET=$(cat "$TFD_STATE/certs/session-secret")
TFD_COOKIE_SECURE=1
FOUNDRY_PROJECT_ENDPOINT=$PROJECT_ENDPOINT
TFD_AGENT_NAME=$STATION_AGENT
TFD_MODEL_NAME=gpt-5-mini
TFD_REGION=East US 2
TFD_FOUNDRY_STOP=1
TFD_TEMPORAL_UI_URL=/temporal-ui
TFD_STATE_DIR=/var/lib/tfd-console$SFX
${PRESETS:+TFD_PRESETS=$PRESETS}
TFD_EOF
chmod 600 /etc/tfd-console$SFX.env
cat > /etc/systemd/system/tfd-console$SFX.service <<'TFD_EOF'
$(sed -e "s|/etc/tfd-console.env|/etc/tfd-console$SFX.env|" -e "s|--port 8080|--port $PORT|" \
      -e "s|^StateDirectory=tfd-console\$|StateDirectory=tfd-console$SFX|" "$REPO/console/deploy/tfd-console.service")
TFD_EOF
mkdir -p /var/lib/tfd-console$SFX
chown -R tfdconsole:tfdconsole /var/lib/tfd-console$SFX
EOF
done

cat >> "$WORK/apply.sh" <<EOF
cat > /etc/caddy/Caddyfile.new <<'TFD_EOF'
$(cat "$WORK/Caddyfile")
TFD_EOF
# Restart the proxy only when its config changed: a restart cuts every worker's connection.
if cmp -s /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile; then
  rm -f /etc/caddy/Caddyfile.new; CADDY_CHANGED=0
else
  mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile; CADDY_CHANGED=1
fi
chown root:caddy /etc/caddy/Caddyfile; chmod 640 /etc/caddy/Caddyfile
chown -R tfdconsole:tfdconsole /opt/tfd-console
systemctl daemon-reload
for unit in $UNITS; do
  name="\${unit%%:*}"
  systemctl enable "\$name" >/dev/null 2>&1
  systemctl restart "\$name"
done
if [ "\$CADDY_CHANGED" = 1 ]; then caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && systemctl restart caddy; fi
sleep 4
echo "caddy: \$(systemctl is-active caddy) (config changed: \$CADDY_CHANGED)"
for unit in $UNITS; do
  name="\${unit%%:*}"; port="\${unit##*:}"
  echo "\$name: \$(systemctl is-active "\$name")  / -> \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:\$port/)  /api/session (no cookie) -> \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:\$port/api/session)"
done
free -m | awk '/^Mem:/ { print "memory: " \$3 " MB used of " \$2 " MB" }'
journalctl -u tfd-console --since "-1 min" --no-pager -o cat | tail -3 | cut -c1-200
EOF
az vm run-command invoke --subscription "$SUB" -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts @"$WORK/apply.sh" --query 'value[0].message' -o tsv
