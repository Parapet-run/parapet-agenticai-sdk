"""Live example: the simplest possible governed agent -- Google ADK's own
quickstart shape (https://google.github.io/adk-docs/get-started/quickstart/),
run in both non-streaming and streaming modes, now governed by real Cedar
policy via
[`parapetai_agent.adk.GovernedRunner`](../../src/parapetai_agent/adk.py).

WIRING -- what changed from a plain ADK quickstart, and it's genuinely
this short:

    from parapetai_agent.adk import GovernedRunner  # was: from google.adk.runners import Runner
    ...
    runner = GovernedRunner(agent=..., app_name=..., session_service=...)

That's the whole integration. Everything else below is OPTIONAL and
defaults to something sane with zero configuration -- see
parapetai_agent/adk.py's own module docstring and build_plugin()'s
docstring for the full story on each kwarg (mirrors
parapetai_agent/maf.py's build_middleware(), which docs/maf-integration-
pattern.md documents once for its seven ported MAF samples -- ADK only has
this one sample so far, so the same notes just live here instead):
  - policy_dir/entities_path (omitted here): the same bundled default
    Cedar policy set (base permits) ships inside parapetai-agent -- no
    setup step, no writable filesystem needed.
  - agent_id/control_plane_url/agent_secret (omitted here): fall back to
    PARAPETAI_AGENT_ID/PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET in
    .env -- all three set governs this by a REAL control-plane-provisioned
    agent's bundle instead of the local default; get that three-line block
    from the control plane's agent detail page ("Integrating this agent")
    after provisioning one there.
  - local_log_dir (set below): opt-in rotating JSON-lines decision audit
    log -- one kwarg instead of a separate configure_rotating_audit_log()
    call.
  - console=False (set below): local_log_dir still writes the file, this
    just skips ALSO echoing every decision as a JSON line to stdout, so it
    doesn't interleave with this script's own print(f"Agent: ...") output.

Uses the Gemini Developer API (GOOGLE_API_KEY) -- the lowest-friction real
ADK auth path (no GCP project, no Application Default Credentials, unlike
Vertex AI/Gemini Enterprise). Confirmed directly against the installed
google-genai Client's own source (google/genai/client.py: defaults to
Gemini Developer API mode whenever GOOGLE_API_KEY is set --
`enterprise`/`vertexai` both default False), not assumed from docs.

Run (needs a real Gemini API key -- https://aistudio.google.com/apikey):
    cp examples/adk_sample_01/.env.example examples/adk_sample_01/.env
    # edit .env: fill in GOOGLE_API_KEY
    uv run --extra adk python3 examples/adk_sample_01/run_example.py

Set PARAPETAI_CONTROL_PLANE_URL/PARAPETAI_AGENT_SECRET/PARAPETAI_AGENT_ID in
.env to govern this by a real control-plane-provisioned agent's bundle
instead -- see the control plane's agent detail page ("Integrating this
agent") for the exact block to paste in, printed once right after
provisioning.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.sessions import InMemorySessionService
from google.genai import types

from parapetai_agent.adk import GovernedRunner, governed_identity

EXAMPLE_DIR = Path(__file__).resolve().parent

_ENV_FILE = EXAMPLE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)

APP_NAME = "hello-agent"
# Google retires/renames Gemini model ids faster than this file gets
# updated -- override via .env (GOOGLE_MODEL=...) rather than editing this
# default if the API starts rejecting it. gemini-2.5-flash (this file's
# original default) started 404ing for new accounts with "no longer
# available to new users ... use models/gemini-3.6-flash" -- a real, live
# API response seen while building this example, not a guess, which is
# why the default below is gemini-3.6-flash now.
MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.6-flash")


def _text_of(content: types.Content | None) -> str:
    if content is None or not content.parts:
        return ""
    return "".join(part.text or "" for part in content.parts)


async def main() -> None:
    root_agent = Agent(
        name="HelloAgent",
        model=MODEL,
        instruction="You are a friendly assistant. Keep your answers brief.",
    )

    # <create_runner>
    runner = GovernedRunner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=InMemorySessionService(),
        local_log_dir=EXAMPLE_DIR / "logs",
        console=False,  # write the audit log to logs/, don't also print it here
        # OFF by default (see build_plugin()'s own docstring): Session.user_id
        # is unverified, and ADK requires it unconditionally, so defaulting
        # this on would make identity-gated Cedar policies silently
        # stricter for ADK than for MAF on the same bundle. This one script
        # opts in deliberately -- it's a single-operator CLI sample with no
        # real caller/adversary distinction to protect (same "trusted,
        # local" framing `adk web`'s own docstring uses), so "alice" being
        # unverified doesn't matter here the way it would behind a real
        # multi-user web app -- see adk_webapp/ for that case instead.
        trust_session_user_id=True,
    )
    # </create_runner>

    await runner.session_service.create_session(app_name=APP_NAME, user_id="alice", session_id="s1")

    # Non-streaming: the session's own user_id ("alice") is the identity
    # Cedar sees for this call -- ParapetPlugin's session.user_id fallback,
    # explicitly opted into above via trust_session_user_id=True (OFF by
    # default -- see adk.py's own module docstring's "Identity" section for
    # why). Same behavior GovernedRunner/build_plugin give any
    # ADK app for free.
    async for event in runner.run_async(
        user_id="alice",
        session_id="s1",
        new_message=types.Content(
            role="user", parts=[types.Part(text="What is the capital of France?")]
        ),
    ):
        text = _text_of(event.content)
        if text:
            print(f"Agent: {text}")

    # Streaming, WITH an explicit ambient identity overriding the
    # session's own user_id -- governed_identity() is the SAME context
    # manager parapetai_agent.maf exports, unchanged (see
    # parapetai_agent/scoped_data.py): pick claims=/roles= (already
    # parsed) or token= (a raw bearer JWT), dispatching internally so
    # callers never have to pick which mechanism matches their data's
    # shape. Streaming here also exercises CLAUDE.md invariant #6
    # (streaming must relay faithfully) on the governed path -- every
    # partial chunk below is relayed unmodified; see adk.py's own
    # module docstring's "Streaming" section for exactly what that means
    # for ParapetPlugin specifically.
    print("Agent (streaming): ", end="", flush=True)
    with governed_identity(claims={"oid": "real-verified-alice"}, roles=["OrderViewer"]):
        async for event in runner.run_async(
            user_id="alice",
            session_id="s1",
            new_message=types.Content(
                role="user", parts=[types.Part(text="Tell me a one-sentence fun fact.")]
            ),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            text = _text_of(event.content)
            if text:
                print(text, end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
