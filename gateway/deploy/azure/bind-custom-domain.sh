#!/usr/bin/env bash
# Binds GATEWAY_DOMAIN (config.sh) to the already-deployed gateway Container
# App, with a free Azure-managed TLS certificate. Run AFTER deploy-gateway.sh
# -- the app must already exist. Same two-phase pattern as
# parapet-platform/deploy/azure/bind-custom-domain.sh (this repo has only
# one app to bind, so no component argument is needed).
#
# This is a multi-step, HALF-MANUAL process -- DNS propagation is out of any
# script's control, and adding the records themselves happens at whatever
# registrar/DNS provider manages the zone, which this script has no API
# access to. Running it PRINTS the exact records to add, waits for you to
# confirm they're in place, attaches the hostname to the app (validating the
# TXT record), THEN asks Azure to issue + bind a managed certificate for it
# -- confirmed live (parapet-platform's own version of this script) these
# are two separate API calls, not one combined operation, despite
# `hostname bind`'s name suggesting otherwise. If DNS hasn't propagated yet,
# one of those steps fails cleanly -- just add the records, wait, and re-run
# this same script; every step here is idempotent.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./config.sh
if [[ -f ./.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

if [[ -z "$GATEWAY_DOMAIN" ]]; then
  echo "GATEWAY_DOMAIN is empty in config.sh -- set it before running this." >&2
  exit 1
fi

# The DNS label relative to whatever zone your registrar/DNS provider
# manages -- "mcpgateway" from "mcpgateway.parapet.run". Assumes the
# hostname is exactly one label under the registered zone; if you're using a
# deeper/different zone layout, adjust the printed record names by hand.
LABEL="${GATEWAY_DOMAIN%%.*}"

DEFAULT_FQDN=$(az containerapp show --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn -o tsv)
VERIFICATION_ID=$(az containerapp show --name "$GATEWAY_APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query properties.customDomainVerificationId -o tsv)

echo "== DNS records needed for ${GATEWAY_DOMAIN} =="
echo
echo "Add these two records at whatever provider manages the zone this"
echo "domain lives under (entered RELATIVE to that zone, not as full names):"
echo
echo "  Type   Host                Value"
echo "  TXT    asuid.${LABEL}      ${VERIFICATION_ID}"
echo "  CNAME  ${LABEL}            ${DEFAULT_FQDN}"
echo
echo "DNS propagation is usually minutes, occasionally longer. This"
echo "script's next step (Azure validating + issuing a certificate) will"
echo "simply fail if the records aren't visible yet -- just re-run this"
echo "same script once they are; nothing above needs redoing."
echo
read -r -p "Press Enter once both records are in place... "

echo "== Adding ${GATEWAY_DOMAIN} as a custom hostname on ${GATEWAY_APP_NAME} (validates the TXT record) =="
az containerapp hostname add \
  --hostname "$GATEWAY_DOMAIN" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$GATEWAY_APP_NAME" \
  --output none

echo "== Binding ${GATEWAY_DOMAIN} to ${GATEWAY_APP_NAME} (this provisions a free managed TLS certificate) =="
az containerapp hostname bind \
  --hostname "$GATEWAY_DOMAIN" \
  --resource-group "$RESOURCE_GROUP" \
  --name "$GATEWAY_APP_NAME" \
  --environment "$CONTAINERAPPS_ENV" \
  --validation-method CNAME \
  --output none

echo "Bound: https://${GATEWAY_DOMAIN}"
echo
echo "The auto-generated ${DEFAULT_FQDN} keeps working too -- this adds a"
echo "second hostname, it doesn't replace the first. Update whatever Rovo/"
echo "Atlassian \"external MCP server\" URLs and OAuth redirect_uris you've"
echo "already registered to use the new domain if you want it canonical,"
echo "but nothing here requires that."
