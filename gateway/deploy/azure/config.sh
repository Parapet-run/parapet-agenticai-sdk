#!/usr/bin/env bash
# Shared config, SOURCED (not executed) by every script in this directory --
# one place to override names/region instead of repeating flags. Same idiom
# as parapet-platform/deploy/azure/config.sh, but for a standalone,
# single-container deployment: no shared Container Apps Environment, no
# Storage Account/Azure Files share. There is nothing here for the gateway
# to persist across restarts -- Cedar policy comes from the control plane at
# every boot (see this repo's CLAUDE.md "no local cedar policy" note and
# server/main.py's poll_once()-before-PolicyEngine sequencing), and OAuth
# client/code/token state (mcp_oauth.py) is deliberately in-memory and
# ephemeral. ACR_NAME must be GLOBALLY unique across all of Azure (not just
# this subscription) if you change it from the placeholder below.
export RESOURCE_GROUP="${RESOURCE_GROUP:-parapet-gateway-rg}"
export LOCATION="${LOCATION:-eastus}"
export ACR_NAME="${ACR_NAME:-parapetgatewayacr}"
export CONTAINERAPPS_ENV="${CONTAINERAPPS_ENV:-parapet-gateway-env}"
export LOG_ANALYTICS_NAME="${LOG_ANALYTICS_NAME:-parapet-gateway-logs}"

export GATEWAY_APP_NAME="${GATEWAY_APP_NAME:-parapet-gateway}"
export GATEWAY_IMAGE_NAME="${GATEWAY_IMAGE_NAME:-parapet-gateway}"

# mcp_oauth.py's client/code/token registry and the bundle-poller's live
# policy state are both process-local with no distributed lock -- see
# mcp_oauth.py's module docstring. A second warm replica would have its own,
# independent OAuth client registry, so a DCR done against replica A would
# 401 if load-balanced to replica B. Raise these only after that state moves
# to a shared store.
export GATEWAY_MIN_REPLICAS="${GATEWAY_MIN_REPLICAS:-1}"
export GATEWAY_MAX_REPLICAS="${GATEWAY_MAX_REPLICAS:-1}"

# Custom domain (bind-custom-domain.sh) -- empty means "use the
# auto-generated *.azurecontainerapps.io FQDN". parapet.run is the canonical
# public domain used by parapet-platform's own control-plane/website
# deployments (Cloudflare-managed there); mcpgateway.parapet.run is this
# gateway's real, chosen subdomain under it, not a placeholder.
export GATEWAY_DOMAIN="${GATEWAY_DOMAIN:-mcpgateway.parapet.run}"

# This script lives at gateway/deploy/azure/ -- three levels below the repo
# root, which is what the Dockerfile's build context must be (it COPYs
# gateway/pyproject.toml and gateway/src using paths relative to repo root).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export REPO_ROOT
