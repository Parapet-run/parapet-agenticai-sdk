#!/usr/bin/env bash
# Builds and deploys sample-mcp-server/ (a toy weather/flights MCP server)
# as a SECOND Container App in the same environment as the gateway --
# INTERNAL-ONLY ingress, not externally reachable at all. This is the
# network-lock pattern from this deployment's own README's governance
# discussion: since nothing but the gateway can reach this app's network
# address, there is no direct URL an admin could mistakenly register with
# Atlassian that bypasses Cedar -- the only route to these tools is through
# the gateway.
#
# Requires provision.sh to have already run (uses the same
# CONTAINERAPPS_ENV/ACR as deploy-gateway.sh). Purely a demo/test upstream --
# real deployments point PARAPETAI_MCP_BASE_URL/PARAPETAI_MCP_UPSTREAMS at
# their own real MCP server(s) instead.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./config.sh

SAMPLE_APP_NAME="${SAMPLE_MCP_APP_NAME:-parapet-sample-mcp}"
SAMPLE_IMAGE_NAME="${SAMPLE_MCP_IMAGE_NAME:-parapet-sample-mcp}"

echo "== Building sample MCP server image in ACR =="
az acr build --registry "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --image "${SAMPLE_IMAGE_NAME}:latest" \
  --file "$REPO_ROOT/gateway/deploy/azure/sample-mcp-server/Dockerfile" \
  "$REPO_ROOT"

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query "passwords[0].value" -o tsv)

if ! az containerapp show --name "$SAMPLE_APP_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "== Creating the sample MCP server Container App (internal ingress only) =="
  az containerapp create --name "$SAMPLE_APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --environment "$CONTAINERAPPS_ENV" \
    --image "${ACR_LOGIN_SERVER}/${SAMPLE_IMAGE_NAME}:latest" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --target-port 8090 \
    --ingress internal \
    --min-replicas 1 \
    --max-replicas 1 \
    --output none
else
  echo "== Updating the sample MCP server image =="
  az containerapp update --name "$SAMPLE_APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --image "${ACR_LOGIN_SERVER}/${SAMPLE_IMAGE_NAME}:latest" \
    --output none
fi

INTERNAL_FQDN=$(az containerapp show --name "$SAMPLE_APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo
echo "Sample MCP server deployed, internal-only: https://${INTERNAL_FQDN}"
echo "(NOT publicly reachable -- only other apps inside ${CONTAINERAPPS_ENV} can reach it,"
echo "which is the point: the gateway is the only path to it.)"
echo
echo "Set in your .env:"
echo "  PARAPETAI_MCP_BASE_URL=https://${INTERNAL_FQDN}/mcp"
echo "then re-run ./deploy-gateway.sh."
