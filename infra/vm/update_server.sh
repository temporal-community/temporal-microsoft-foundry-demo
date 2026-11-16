#!/bin/sh
# Replace the Temporal server binary on the demo VM, and nothing else.
#   TFD_STATE=<private state dir> ./update_server.sh <path to the linux/amd64 server binary>
#   TFD_STATE=<private state dir> ./update_server.sh --rollback
# The binary that was running is kept as /opt/temporal/temporal-server.prev, and --rollback
# puts it back. The server's SQLite file, the proxy, the consoles and the Temporal UI are not
# touched. The server is down for about ten seconds: run it when no run is working.
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
ARG="${1:?path to the linux/amd64 server binary, or --rollback}"
. "$TFD_STATE/env.sh"; . "$TFD_STATE/vm.env"; . "$TFD_STATE/storage.env"
umask 077
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

CHECK='
for i in $(seq 1 30); do /usr/local/bin/temporal operator cluster health --address 127.0.0.1:7233 >/dev/null 2>&1 && break; sleep 2; done
echo "temporal: $(systemctl is-active temporal)  health: $(/usr/local/bin/temporal operator cluster health --address 127.0.0.1:7233 2>&1 | tail -1)"
sha256sum /opt/temporal/temporal-server | cut -c1-16
journalctl -u temporal --since "-1 min" --no-pager -o cat | grep -c -i "panic\|fatal" || true'

if [ "$ARG" = "--rollback" ]; then
  cat > "$WORK/apply.sh" <<EOF
#!/bin/bash
set -euo pipefail
[ -x /opt/temporal/temporal-server.prev ] || { echo "no previous binary to go back to"; exit 1; }
systemctl stop temporal
mv /opt/temporal/temporal-server /opt/temporal/temporal-server.undone
mv /opt/temporal/temporal-server.prev /opt/temporal/temporal-server
systemctl start temporal
$CHECK
EOF
else
  SHA=$(shasum -a 256 "$ARG" | cut -c1-64)
  KEY="$(az storage account keys list --subscription "$SUB" -g "$RG" -n "$STORAGE" --query '[0].value' -o tsv)"
  az storage blob upload --account-name "$STORAGE" --account-key "$KEY" -c bin \
    -n temporal-server-linux-amd64 -f "$ARG" --overwrite --no-progress -o none
  EXPIRY="$(date -u -v+1H +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d '+1 hour' +%Y-%m-%dT%H:%MZ)"
  SAS="$(az storage blob generate-sas --account-name "$STORAGE" --account-key "$KEY" -c bin \
    -n temporal-server-linux-amd64 --permissions r --expiry "$EXPIRY" --https-only -o tsv)"
  cat > "$WORK/apply.sh" <<EOF
#!/bin/bash
set -euo pipefail
curl -fsSL -o /opt/temporal/temporal-server.new 'https://$STORAGE.blob.core.windows.net/bin/temporal-server-linux-amd64?$SAS'
echo '$SHA  /opt/temporal/temporal-server.new' | sha256sum -c - >/dev/null
chmod 0755 /opt/temporal/temporal-server.new
/opt/temporal/temporal-server.new --version | head -1
systemctl stop temporal
cp -p /opt/temporal/temporal-server /opt/temporal/temporal-server.prev
mv /opt/temporal/temporal-server.new /opt/temporal/temporal-server
systemctl start temporal
$CHECK
EOF
fi
az vm run-command invoke --subscription "$SUB" -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts @"$WORK/apply.sh" --query 'value[0].message' -o tsv
