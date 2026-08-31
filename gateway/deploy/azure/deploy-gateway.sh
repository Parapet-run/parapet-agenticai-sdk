#!/usr/bin/env bash
# Builds gateway/Dockerfile and deploys it as a single Azure Container App.
# Requires provision.sh to have already run.
#
# Required values, read from ./.env if present (copy .env.example --
# gitignored; sourced automatically below) -- an explicit `export FOO=...`
# before running this still works too. See .env.example for what each one
# means; the three control-plane values are required because this
# deployment ships with NO local Cedar policy at all (see .env.example's own
# header comment and server/main.py's poll_once()-before-PolicyEngine
# sequencing).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./config.sh
if [[ -f ./.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

: "${PARAPETAI_CONTROL_PLANE_URL:?Set PARAPETAI_CONTROL_PLANE_URL -- see .env.example}"
: "${PARAPETAI_AGENT_ID:?Set PARAPETAI_AGENT_ID -- see .env.example}"
: "${PARAPETAI_AGENT_SECRET:?Set PARAPETAI_AGENT_SECRET -- see .env.example}"
PARAPETAI_MCP_BASE_URL="${PARAPETAI_MCP_BASE_URL:-}"
PARAPETAI_MCP_UPSTREAMS="${PARAPETAI_MCP_UPSTREAMS:-}"
PARAPETAI_MCP_AUTH_MODE="${PARAPETAI_MCP_AUTH_MODE:-none}"
PARAPETAI_MCP_OAUTH_SHARED_SECRET="${PARAPETAI_MCP_OAUTH_SHARED_SECRET:-}"
PARAPETAI_MODE="${PARAPETAI_MODE:-enforce}"
PARAPETAI_OTLP_ENDPOINT="${PARAPETAI_OTLP_ENDPOINT:-}"

if [[ "$PARAPETAI_MCP_AUTH_MODE" == "oauth2" && -z "$PARAPETAI_MCP_OAUTH_SHARED_SECRET" ]]; then
  echo "PARAPETAI_MCP_AUTH_MODE=oauth2 requires PARAPETAI_MCP_OAUTH_SHARED_SECRET -- the" >&2
  echo "gateway itself fails closed on this at startup (see mcp_oauth.py), so it's" >&2
  echo "caught here too rather than deploying a container that will crash-loop." >&2
  exit 1
fi

echo "== Building gateway image in ACR =="
az acr build --registry "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --image "${GATEWAY_IMAGE_NAME}:latest" \
  --file "$REPO_ROOT/gateway/Dockerfile" \
  "$REPO_ROOT"

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" \
  --query "passwords[0].value" -o tsv)
ENV_ID=$(az containerapp env show --name "$CONTAINERAPPS_ENV" --resource-group "$RESOURCE_GROUP" \
  --query id -o tsv)

# Same reasoning as parapet-platform/deploy/azure/deploy-maf-webapp.sh's own
# comment here: Container Apps only cuts a new revision when the template
# SPEC changes, and the image ref below is the literal string ":latest"
# every time -- tying revisionSuffix to the fresh digest forces a new
# revision exactly when the image content actually changed. THE SUFFIX MUST
# GO INSIDE THE YAML (template.revisionSuffix), NOT a separate
# --revision-suffix flag alongside --yaml (silently dropped -- confirmed
# live in that repo, twice).
IMAGE_DIGEST=$(az acr repository show --name "$ACR_NAME" --image "${GATEWAY_IMAGE_NAME}:latest" \
  --query digest -o tsv)
REVISION_SUFFIX="d${IMAGE_DIGEST#sha256:}"
REVISION_SUFFIX="${REVISION_SUFFIX:0:20}"

# PARAPETAI_MCP_UPSTREAMS is a JSON object -- its own double quotes would
# collide with the YAML `value: "..."` convention every other env var below
# uses. Single-quoted YAML scalar instead, with any literal single quote in
# the value doubled per YAML's own escaping rule (JSON strings never contain
# one unless a URL genuinely does).
MCP_UPSTREAMS_YAML="${PARAPETAI_MCP_UPSTREAMS//\'/\'\'}"

# Container Apps rejects a `secrets:` entry whose value is the empty string
# outright ("value or keyVaultUrl and identity should be provided") -- so
# this secret (and the env var referencing it) must be OMITTED entirely when
# PARAPETAI_MCP_OAUTH_SHARED_SECRET isn't set (the none-mode default), not
# just declared with an empty value. Confirmed live: the unconditional
# version of this failed exactly this way on a real deploy.
MCP_OAUTH_SECRET_YAML=""
MCP_OAUTH_ENV_YAML=""
if [[ -n "$PARAPETAI_MCP_OAUTH_SHARED_SECRET" ]]; then
  MCP_OAUTH_SECRET_YAML="      - name: mcp-oauth-shared-secret
        value: \"${PARAPETAI_MCP_OAUTH_SHARED_SECRET}\""
  MCP_OAUTH_ENV_YAML="          - name: PARAPETAI_MCP_OAUTH_SHARED_SECRET
            secretRef: mcp-oauth-shared-secret"
fi

MANIFEST="./.last-manifest-gateway.yaml"
cat > "$MANIFEST" <<YAML
location: ${LOCATION}
type: Microsoft.App/containerApps
properties:
  environmentId: ${ENV_ID}
  configuration:
    ingress:
      external: true
      targetPort: 8080
      transport: auto
    registries:
      - server: ${ACR_LOGIN_SERVER}
        username: ${ACR_USERNAME}
        passwordSecretRef: acr-password
    secrets:
      - name: acr-password
        value: "${ACR_PASSWORD}"
      - name: parapetai-agent-secret
        value: "${PARAPETAI_AGENT_SECRET}"
${MCP_OAUTH_SECRET_YAML}
  template:
    revisionSuffix: ${REVISION_SUFFIX}
    containers:
      - name: gateway
        image: ${ACR_LOGIN_SERVER}/${GATEWAY_IMAGE_NAME}:latest
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
          - name: PARAPETAI_CONTROL_PLANE_URL
            value: "${PARAPETAI_CONTROL_PLANE_URL}"
          - name: PARAPETAI_AGENT_ID
            value: "${PARAPETAI_AGENT_ID}"
          - name: PARAPETAI_AGENT_SECRET
            secretRef: parapetai-agent-secret
          - name: PARAPETAI_MCP_BASE_URL
            value: "${PARAPETAI_MCP_BASE_URL}"
          - name: PARAPETAI_MCP_UPSTREAMS
            value: '${MCP_UPSTREAMS_YAML}'
          - name: PARAPETAI_MCP_AUTH_MODE
            value: "${PARAPETAI_MCP_AUTH_MODE}"
${MCP_OAUTH_ENV_YAML}
          - name: PARAPETAI_MODE
            value: "${PARAPETAI_MODE}"
          - name: PARAPETAI_OTLP_ENDPOINT
            value: "${PARAPETAI_OTLP_ENDPOINT}"
    scale:
      minReplicas: ${GATEWAY_MIN_REPLICAS}
      maxReplicas: ${GATEWAY_MAX_REPLICAS}
YAML

echo "== Creating/updating the gateway Container App =="
# Two phases, not one `create --yaml` -- see parapet-platform's matching
# deploy scripts for why (a real, observed `create --yaml` limitation around
# complex config, not a bug in the generated YAML itself).
if ! az containerapp show --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "-- app doesn't exist yet, minimal create first --"
  az containerapp create --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" \
    --environment "$CONTAINERAPPS_ENV" \
    --image "${ACR_LOGIN_SERVER}/${GATEWAY_IMAGE_NAME}:latest" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --target-port 8080 \
    --ingress external \
    --min-replicas "$GATEWAY_MIN_REPLICAS" \
    --max-replicas "$GATEWAY_MAX_REPLICAS" \
    --output none
fi
az containerapp update --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --yaml "$MANIFEST" --output none

FQDN=$(az containerapp show --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn -o tsv)
PUBLIC_URL="https://${FQDN}"

echo
echo "gateway deployed: ${PUBLIC_URL}"
echo
echo "Verify:"
echo "  curl ${PUBLIC_URL}/__parapetai/ready"
echo
if [[ "$PARAPETAI_MCP_AUTH_MODE" == "oauth2" ]]; then
  echo "OAuth metadata (what Atlassian's DCR flow will discover):"
  echo "  curl ${PUBLIC_URL}/.well-known/oauth-protected-resource"
  echo "  curl ${PUBLIC_URL}/.well-known/oauth-authorization-server"
  echo
  echo "Register this URL as Rovo's custom/external MCP server:"
  echo "  ${PUBLIC_URL}/a/<agent-id>/mcp"
else
  echo "MCP endpoint (no auth yet -- PARAPETAI_MCP_AUTH_MODE=none):"
  echo "  ${PUBLIC_URL}/a/<agent-id>/mcp"
  echo
  echo "Switch to oauth2 mode before registering with Atlassian Rovo -- see README.md."
fi
