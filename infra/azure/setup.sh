#!/bin/sh
# Create the Azure side of the demo from nothing: a resource group, a Foundry resource and
# project, a gpt-5-mini deployment, and the role that lets you deploy and invoke hosted agents.
#
#   TFD_STATE=<private state dir> SUBSCRIPTION=<name or id> ./setup.sh
#
# Optional: LOCATION (default eastus2), RG (default rg-temporal-foundry-demo),
# MODEL (default gpt-5-mini), MODEL_VERSION (default 2025-08-07), CAPACITY (default 1000,
# in thousands of tokens per minute). Writes $TFD_STATE/env.sh. Safe to run again.
# Remove everything with: az group delete -n "$RG"
set -eu
: "${TFD_STATE:?set TFD_STATE to a private directory outside the repository}"
: "${SUBSCRIPTION:?set SUBSCRIPTION to the Azure subscription name or id}"
LOC="${LOCATION:-eastus2}"; RG="${RG:-rg-temporal-foundry-demo}"
MODEL="${MODEL:-gpt-5-mini}"; MODEL_VERSION="${MODEL_VERSION:-2025-08-07}"; CAPACITY="${CAPACITY:-1000}"
PROJECT=temporal-foundry-demo
mkdir -p "$TFD_STATE"; chmod 700 "$TFD_STATE"
SUB="$(az account show --subscription "$SUBSCRIPTION" --query id -o tsv)"

for ns in Microsoft.CognitiveServices Microsoft.Compute Microsoft.Network Microsoft.Storage; do
  az provider register --subscription "$SUB" --namespace "$ns" --wait >/dev/null
done
az group create --subscription "$SUB" -n "$RG" -l "$LOC" --tags purpose=temporal-foundry-demo -o none

# The Foundry resource's name is also its public host name, so it has to be globally unique.
ACCT="$(az cognitiveservices account list --subscription "$SUB" -g "$RG" --query "[?kind=='AIServices'].name | [0]" -o tsv)"
if [ -z "$ACCT" ]; then
  ACCT="tfd-foundry-$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 5)"
  az cognitiveservices account create --subscription "$SUB" -g "$RG" -n "$ACCT" -l "$LOC" \
    --kind AIServices --sku S0 --custom-domain "$ACCT" --allow-project-management true \
    --assign-identity --tags purpose=temporal-foundry-demo --yes -o none
fi
az cognitiveservices account project show --subscription "$SUB" -g "$RG" -n "$ACCT" \
  --project-name "$PROJECT" -o none 2>/dev/null ||
  az cognitiveservices account project create --subscription "$SUB" -g "$RG" -n "$ACCT" \
    --project-name "$PROJECT" -l "$LOC" --assign-identity -o none
az cognitiveservices account deployment create --subscription "$SUB" -g "$RG" -n "$ACCT" \
  --deployment-name "$MODEL" --model-name "$MODEL" --model-version "$MODEL_VERSION" \
  --model-format OpenAI --sku-name GlobalStandard --sku-capacity "$CAPACITY" -o none

ACCT_ID="$(az cognitiveservices account show --subscription "$SUB" -g "$RG" -n "$ACCT" --query id -o tsv)"
# Foundry User carries the data actions for deploying agents, invoking them and calling the
# model. Owner or Contributor on the subscription does not. The object id comes from the
# access token, because reading it from Microsoft Graph is often not permitted.
ME="$(az account get-access-token --subscription "$SUB" --query accessToken -o tsv | python3 -c '
import base64, json, sys
payload = sys.stdin.read().split(".")[1]
print(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["oid"])')"
az role assignment create --subscription "$SUB" --assignee-object-id "$ME" \
  --assignee-principal-type User --role "Foundry User" --scope "$ACCT_ID" -o none

cat > "$TFD_STATE/env.sh" <<ENV
SUB=$SUB
RG=$RG
LOC=$LOC
ACCT=$ACCT
PROJECT=$PROJECT
ACCT_ID=$ACCT_ID
PROJECT_ENDPOINT=https://$ACCT.services.ai.azure.com/api/projects/$PROJECT
MODEL=$MODEL
ENV
echo "wrote $TFD_STATE/env.sh"; cat "$TFD_STATE/env.sh"
echo "Role assignments can take a minute to apply. Check with:"
echo "  az rest --method GET --resource https://ai.azure.com --url \"https://$ACCT.services.ai.azure.com/api/projects/$PROJECT/agents?api-version=v1\""
