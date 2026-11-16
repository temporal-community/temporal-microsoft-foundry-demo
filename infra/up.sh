#!/bin/sh
# From an Azure subscription with nothing in it to a running demo.
#
#   TFD_STATE=<private dir outside the repository> SUBSCRIPTION=<name or id> infra/up.sh
#
# Needs: az (logged in, Owner or User Access Administrator on the subscription), go, git,
# uv, python3 and openssl. Took 15 minutes when we ran it on an empty resource group; most
# of that is Azure and the server build.
# Every step can be run again; a step that is already done is skipped or repeated harmlessly.
#
# Optional: LOCATION (eastus2), RG (rg-temporal-foundry-demo), CAPACITY (model deployment,
# thousands of tokens per minute), VM_SIZE (Standard_D2als_v7), TFD_STATIONS (1),
# TFD_BUILD_DIR (/tmp/tfd-build).
#
# What it creates, all in one resource group: a Foundry resource and project with a gpt-5-mini
# deployment, a small VM with a public name, a storage account for carrying files to the VM,
# and one hosted agent per station. Remove everything with: az group delete -n <RG>
set -eu
: "${TFD_STATE:?set TFD_STATE to a private directory outside the repository}"
: "${SUBSCRIPTION:?set SUBSCRIPTION to the Azure subscription name or id}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
BUILD="${TFD_BUILD_DIR:-/tmp/tfd-build}"
step() { printf '\n== %s\n' "$1"; }

step "1 of 6: Foundry resource, project and model deployment"
"$HERE/azure/setup.sh"
. "$TFD_STATE/env.sh"

step "2 of 6: the VM"
[ -f "$TFD_STATE/vm.env" ] || "$HERE/vm/create_vm.sh"
. "$TFD_STATE/vm.env"

step "3 of 6: the Temporal server, built from source with the Foundry compute provider"
BIN="$BUILD/build/temporal-server-linux-amd64"
[ -x "$BIN" ] || "$HERE/server/build-server.sh" "$BUILD" linux amd64

step "4 of 6: server, proxy and Temporal UI on the VM"
"$HERE/vm/install_server.sh" "$BIN"

step "5 of 6: wait until this account may deploy agents (role assignments take a minute)"
n=0
until az rest --method GET --resource https://ai.azure.com \
  --url "$PROJECT_ENDPOINT/agents?api-version=v1" -o none 2>/dev/null; do
  n=$((n + 1)); [ "$n" -le 30 ] || { echo "still refused after 5 minutes"; exit 1; }
  sleep 10
done

step "6 of 6: the worker in Foundry and the console"
(cd "$REPO/worker" && uv sync --quiet)
TFD_STATIONS="${TFD_STATIONS:-1}" "$REPO/scripts/release.sh"

cat <<DONE

The demo is at https://$VM_FQDN
Access code:   cat $TFD_STATE/certs/access-code
Check it:      TFD_BASE=https://$VM_FQDN TFD_CODE=\$(cat $TFD_STATE/certs/access-code) \\
                 $REPO/worker/.venv/bin/python $REPO/scripts/booth_check.py
The VM costs about \$0.08 an hour while it runs. Stop paying for it with:
               az vm deallocate --subscription $SUB -g $RG -n $VM
DONE
