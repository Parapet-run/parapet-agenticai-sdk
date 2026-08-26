"""Live example: the simplest possible governed agent -- ported from
Microsoft Agent Framework's own "Hello Agent" sample
(https://github.com/microsoft/agent-framework/blob/main/python/samples/01-get-started/01_hello_agent.py),
non-streaming and streaming, both governed by real Cedar policy.

Uses the SAME client + credential upstream does -- FoundryChatClient and
AzureCliCredential -- not a simplified stand-in. GovernedAgent doesn't
care which agent_framework-compatible client backs it (confirmed by
reading src/parapetai_agent/maf.py, not assumed -- see
docs/maf-integration-pattern.md), so no parapetai-agent code needed changing to
support this. agent-framework-foundry itself is confirmed to be a thin
wrapper (FoundryChatClient's own MRO is
FoundryChatClient -> RawFoundryChatClient -> RawOpenAIChatClient ->
BaseChatClient -- Foundry's wire protocol IS OpenAI-compatible) around
agent-framework-core, the same foundation every other client in this
directory sits on.

WIRING -- what changed from upstream, and it's genuinely this short now:

    from parapetai_agent import GovernedAgent  # was: from agent_framework import Agent
    ...
    agent = GovernedAgent(client=..., name=..., instructions=...)

That's the whole integration. Everything below is OPTIONAL and defaults
to something sane with zero configuration -- see
docs/maf-integration-pattern.md for the full story, this file only notes
what's relevant inline:
  - policy_dir/entities_path (omitted here): a real Cedar policy set
    ships INSIDE parapetai-agent itself (default_policies/, base permits) --
    omitting these enforces that, not an empty/no-op engine, and never
    requires a writable filesystem (works in a read-only container).
  - agent_id/control_plane_url/agent_secret (omitted here): fall back to
    PARAPETAI_AGENT_ID/PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET in .env --
    all three set governs this by a REAL control-plane-provisioned
    agent's bundle instead of the local default; get that three-line
    block from the control plane's agent detail page ("Integrating this
    agent") after provisioning one there.
  - persist_policy_dir (omitted here): a control-plane-pulled bundle
    stays in memory only, never written to disk -- the right default for
    a serverless/k8s process with no writable/persistent volume. Pass a
    real directory to get the OLD on-restart-survives-a-brief-outage
    behavior back, if this process does have somewhere durable to write.
  - local_log_dir (set below): opt-in rotating JSON-lines decision audit
    log -- one kwarg instead of a separate configure_rotating_audit_log()
    call.
  - console=False (set below): local_log_dir still writes the file, this
    just skips ALSO echoing every decision as a JSON line to stdout, so
    it doesn't interleave with this script's own print(f"Agent: ...")
    output -- see GovernedAgent's own docstring for what else this
    suppresses (the auto-wired OTel console exporter, if a control plane
    is configured). Telemetry actually sent anywhere (the file, or a
    control plane) is unaffected either way.

Run (local dry run, governed by the bundled default policy, no control
plane needed) -- needs `az login` once first (AzureCliCredential, same as
upstream, reads your Azure CLI's own local token cache -- not settable
via .env):
    az login
    cp examples/maf_sample_01/.env.example examples/maf_sample_01/.env
    # fill in FOUNDRY_PROJECT_ENDPOINT in that .env
    uv run --with agent-framework python3 examples/maf_sample_01/run_example.py

Set PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET/PARAPETAI_AGENT_ID in .env to
govern this by a real control-plane-provisioned agent's bundle instead --
see the control plane's agent detail page ("Integrating this agent") for
the exact block to paste in, printed once right after provisioning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from parapetai_agent import GovernedAgent, governed_identity

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

# Optional -- swap the default in-memory identity store
# (parapetai_agent.identity_store.InMemoryIdentityStore) for a real shared
# backend (Redis, a database) if this agent will ever run as more than
# one replica; see that module's own docstring for why that's a real
# correctness requirement in that case (identity set on one replica is
# invisible to another), not cosmetic polish. Left at its default here
# -- a single hello-world script has no concept of "more than one
# replica" to begin with -- shown only so the seam is visible:
#
#   from parapetai_agent import configure_identity_store
#   configure_identity_store(MyRedisBackedIdentityStore())


async def main() -> None:
    # The SAME credential goes to both the chat client and (optionally)
    # identity extraction below -- one az login, two uses.
    credential = AzureCliCredential()

    # <create_agent>
    async with GovernedAgent(
        client=FoundryChatClient(credential=credential),
        name="HelloAgent",
        instructions="You are a friendly assistant. Keep your answers brief.",
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # write the audit log to logs/, don't also print it here
    ) as agent:
        # </create_agent>

        # Optional: extract the signed-in az login identity from the SAME
        # credential already passed to FoundryChatClient above, and set
        # it ambiently for every Cedar decision made inside this block.
        # governed_identity() dispatches to identity_from_azure_credential()
        # here because credential= was given -- the SAME call also
        # accepts claims=/roles= (already-parsed) or token= (a raw
        # bearer JWT); see its own docstring for why one name beats
        # picking the matching function by hand. Every call below already
        # worked without this; add it once a real Cedar policy needs to
        # key on WHO is signed in (e.g. policies/30-identity.cedar's
        # OrderViewer role gate), not just WHICH agent is calling --
        # an unwrapped agent.run() evaluates such a policy against EMPTY
        # identity_claims/identity_roles, which denies (Cedar's own
        # default-deny), not silently allows.
        with governed_identity(credential=credential):
            # Non-streaming: get the complete response at once
            result = await agent.run("What is the capital of France?")
            print(f"Agent: {result}")

            # Streaming: receive tokens as they are generated -- CLAUDE.md
            # invariant #6 (streaming must relay faithfully) applies to
            # GovernedAgent exactly as it does to the standalone gateway;
            # this proves the governed path never buffers/reorders chunks.
            print("Agent (streaming): ", end="", flush=True)
            async for chunk in agent.run("Tell me a one-sentence fun fact.", stream=True):
                if chunk.text:
                    print(chunk.text, end="", flush=True)
            print()


if __name__ == "__main__":
    asyncio.run(main())
