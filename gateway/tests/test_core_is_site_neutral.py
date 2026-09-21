"""The core gateway knows nothing about where it is deployed.

Everything specific to a site -- a cloud provider, a secrets manager product, a
cluster distribution, a DNS suffix -- belongs in that site's deploy directory
(certificate mounts, identities, load-balancer annotations, node pools). The
gateway itself only knows generic mechanisms: files on disk, environment
variables, an optional opaque `PARAPETAI_GATEWAY_SITE` label.

This guard fails if a hosting-specific term appears anywhere in the gateway
source, comments included, so a second site (another cloud, on-prem) never has to
untangle the first one's assumptions from the code.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "parapetai_gateway"

# Hosting-specific terms. Deliberately NOT here: "Entra"/"Okta" (identity
# providers a tenant configures, not where the gateway runs), "CSI" and
# "Kubernetes" (generic mechanisms any site may use), or CDNs in front of an
# UPSTREAM provider (not where the gateway itself is hosted).
_SITE_SPECIFIC = re.compile(
    r"azure|key\s*vault|keyvault|\baks\b|cloudapp|containerapp|container apps|"
    r"\baws\b|amazon|\beks\b|\bgke\b|\bgcp\b|google cloud|"
    r"secrets manager \(aws\)|windows\.net",
    re.IGNORECASE,
)


def test_the_core_gateway_source_names_no_hosting_environment() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if _SITE_SPECIFIC.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:100]}")
    assert not offenders, (
        "site-specific text in the core gateway; move it to that site's deploy directory:\n"
        + "\n".join(offenders)
    )


def test_the_guard_actually_matches_what_it_is_meant_to_catch() -> None:
    for text in (
        "Azure Key Vault",
        "an AKS cluster",
        "AWS Secrets",
        "on GKE",
        "x.cloudapp.azure.com",
    ):
        assert _SITE_SPECIFIC.search(text), text
    for text in ("a secrets manager", "Entra ID", "a CSI driver", "Kubernetes Secret"):
        assert not _SITE_SPECIFIC.search(text), text
