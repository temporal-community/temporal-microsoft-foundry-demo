#!/bin/sh
# Put the custom Temporal server and its proxy on the demo VM.
#   TFD_STATE=<private state dir> ./install_server.sh <path to the linux/amd64 server binary>
# The VM has no SSH, so the binary travels through a private storage account and the setup
# runs with `az vm run-command`. Run after create_vm.sh. Safe to run again (a new binary
# replaces the old one; the server's SQLite file is kept).
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
BIN="${1:?path to the linux/amd64 server binary (see infra/server/build-server.sh)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$TFD_STATE/env.sh"; . "$TFD_STATE/vm.env"
umask 077
mkdir -p "$TFD_STATE/certs"
# The bearer token workers present to the proxy (as their Temporal API key) and to the console.
[ -f "$TFD_STATE/certs/token" ] || openssl rand -hex 32 > "$TFD_STATE/certs/token"

STORAGE="${STORAGE:-tfdstore$(printf '%s' "$ACCT" | tail -c 5)}"
az storage account show --subscription "$SUB" -g "$RG" -n "$STORAGE" -o none 2>/dev/null ||
  az storage account create --subscription "$SUB" -g "$RG" -n "$STORAGE" -l "$LOC" \
    --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false \
    --min-tls-version TLS1_2 --tags purpose=temporal-foundry-demo -o none
KEY="$(az storage account keys list --subscription "$SUB" -g "$RG" -n "$STORAGE" --query '[0].value' -o tsv)"
az storage container create --account-name "$STORAGE" --account-key "$KEY" -n bin -o none
az storage blob upload --account-name "$STORAGE" --account-key "$KEY" -c bin \
  -n temporal-server-linux-amd64 -f "$BIN" --overwrite --no-progress -o none
EXPIRY="$(date -u -v+1H +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d '+1 hour' +%Y-%m-%dT%H:%MZ)"
SAS="$(az storage blob generate-sas --account-name "$STORAGE" --account-key "$KEY" -c bin \
  -n temporal-server-linux-amd64 --permissions r --expiry "$EXPIRY" --https-only -o tsv)"
printf "STORAGE='%s'\nBINARY_URL='%s'\n" "$STORAGE" \
  "https://$STORAGE.blob.core.windows.net/bin/temporal-server-linux-amd64?$SAS" > "$TFD_STATE/storage.env"

"$HERE/gen_setup.sh" "$BIN"
az vm run-command invoke --subscription "$SUB" -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts @"$TFD_STATE/vm-setup.sh" --query 'value[0].message' -o tsv
rm -f "$TFD_STATE/vm-setup.sh"

# Settings the other scripts read. Existing files are kept, so a changed value survives.
TOKEN="$(cat "$TFD_STATE/certs/token")"
# The code visitors type to open the console, and the key that signs its login cookie.
[ -f "$TFD_STATE/certs/access-code" ] || openssl rand -hex 5 > "$TFD_STATE/certs/access-code"
[ -f "$TFD_STATE/certs/session-secret" ] || openssl rand -hex 32 > "$TFD_STATE/certs/session-secret"
# For the scripts on this machine: source it before scripts/tfd.py and scripts/setup_deployment.py.
[ -f "$TFD_STATE/client.env" ] || cat > "$TFD_STATE/client.env" <<ENV
export TEMPORAL_ADDRESS=$VM_FQDN:443
export TEMPORAL_NAMESPACE=default
export TEMPORAL_API_KEY=$TOKEN
export TEMPORAL_TLS=1
ENV
# The environment of the worker in Foundry. The timings make a sandbox leave about 20 s
# after its work stops; TFD_ALLOW_FAULTS lets the console crash a worker on request.
[ -f "$TFD_STATE/worker-booth.env" ] || cat > "$TFD_STATE/worker-booth.env" <<ENV
TEMPORAL_ADDRESS=$VM_FQDN:443
TEMPORAL_NAMESPACE=default
TEMPORAL_API_KEY=$TOKEN
TEMPORAL_TLS=1
TFD_CONSOLE_URL=https://$VM_FQDN
TFD_MODEL=foundry
TFD_KEEPALIVE_SECONDS=45
TFD_BUSY_LINGER_SECONDS=10
TFD_IDLE_EXIT_SECONDS=15
TFD_DEADLINE_MARGIN_SECONDS=30
TFD_DRAIN_SECONDS=20
TFD_ALLOW_FAULTS=1
ENV
echo "settings are in $TFD_STATE (client.env, worker-booth.env, certs/access-code)"
