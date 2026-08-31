#!/usr/bin/env bash
# Provisions the shared Azure infrastructure deploy-gateway.sh needs:
# resource group, Container Registry, Container Apps Environment (+ its Log
# Analytics workspace). No Storage Account/Azure Files share -- unlike
# parapet-platform's control-plane/maf-webapp deployments, this gateway has
# no state that needs to survive a restart (see config.sh's own comment).
#
# Idempotent: every `az ... create` below is safe to re-run.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./config.sh

echo "== Resource group: $RESOURCE_GROUP ($LOCATION) =="
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "== Container Registry: $ACR_NAME =="
az acr create --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --sku Basic --admin-enabled true --output none

echo "== Log Analytics workspace: $LOG_ANALYTICS_NAME =="
az monitor log-analytics workspace create \
  --resource-group "$RESOURCE_GROUP" --workspace-name "$LOG_ANALYTICS_NAME" \
  --location "$LOCATION" --output none
LOG_ANALYTICS_CLIENT_ID=$(az monitor log-analytics workspace show \
  --resource-group "$RESOURCE_GROUP" --workspace-name "$LOG_ANALYTICS_NAME" \
  --query customerId -o tsv)
LOG_ANALYTICS_CLIENT_SECRET=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "$RESOURCE_GROUP" --workspace-name "$LOG_ANALYTICS_NAME" \
  --query primarySharedKey -o tsv)

echo "== Container Apps Environment: $CONTAINERAPPS_ENV =="
az containerapp env create --name "$CONTAINERAPPS_ENV" --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --logs-workspace-id "$LOG_ANALYTICS_CLIENT_ID" \
  --logs-workspace-key "$LOG_ANALYTICS_CLIENT_SECRET" \
  --output none

echo
echo "Provisioned. Next: copy .env.example to .env, fill in your control-plane"
echo "agent_id/agent_secret and downstream MCP server URL, then run"
echo "./deploy-gateway.sh"
