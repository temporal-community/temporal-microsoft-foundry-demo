#!/bin/sh
# Create the VM that hosts the custom Temporal server for the Foundry demo.
#   TFD_STATE=<private state dir> ./create_vm.sh      (reads env.sh; writes vm.env)
# Everything lands in the demo resource group; `az group delete -n $RG` removes it all.
set -eu
: "${TFD_STATE:?set TFD_STATE to the private state directory}"
. "$TFD_STATE/env.sh"
VM=vm-tfd-temporal
KEY="$TFD_STATE/id_ed25519"
LABEL="tfd-temporal-$(printf '%s' "$ACCT" | tail -c 5)"

for ns in Microsoft.Compute Microsoft.Network; do
  az provider register --subscription "$SUB" --namespace "$ns" --wait >/dev/null
done
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C tfd-demo -f "$KEY"

az vm create --subscription "$SUB" -g "$RG" -n "$VM" -l "$LOC" \
  --image Ubuntu2404 --size "${VM_SIZE:-Standard_D2als_v7}" --admin-username tfd \
  --ssh-key-values "$KEY.pub" --assign-identity '[system]' \
  --public-ip-sku Standard --public-ip-address-dns-name "$LABEL" \
  --nsg-rule NONE --tags purpose=temporal-foundry-demo -o none

NSG="$(az network nsg list --subscription "$SUB" -g "$RG" --query "[?contains(name,'$VM')].name | [0]" -o tsv)"
# One open port: 443, which serves gRPC behind a bearer token and the console behind its
# access code. No SSH; administer the VM with `az vm run-command`.
az network nsg rule create --subscription "$SUB" -g "$RG" --nsg-name "$NSG" -n https \
  --priority 110 --access Allow --protocol Tcp --destination-port-ranges 443 \
  --source-address-prefixes '*' -o none

PRINCIPAL="$(az vm show --subscription "$SUB" -g "$RG" -n "$VM" --query identity.principalId -o tsv)"
# The controller and the console on this VM call Foundry as the VM's identity: invoke hosted
# agents, and stop or re-invoke the sessions it created.
for role in "Foundry Agent Consumer" "Foundry User"; do
  az role assignment create --subscription "$SUB" --assignee-object-id "$PRINCIPAL" \
    --assignee-principal-type ServicePrincipal --role "$role" --scope "$ACCT_ID" -o none
done

FQDN="$LABEL.$LOC.cloudapp.azure.com"
printf 'VM=%s\nVM_FQDN=%s\nVM_PRINCIPAL=%s\nNSG=%s\n' "$VM" "$FQDN" "$PRINCIPAL" "$NSG" > "$TFD_STATE/vm.env"
cat "$TFD_STATE/vm.env"
