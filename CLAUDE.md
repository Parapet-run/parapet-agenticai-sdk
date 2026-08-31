# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**The enforcement side of Parapet.** This repo owns everything that actually
evaluates a Cedar decision on a real request — the in-process SDK
(`parapetai-agent`, published to PyPI) and the standalone proxy PEP
(`parapetai-gateway`, this repo's `gateway/`) — plus `mcp-server/`,
`conformance/`, and the Cedar `policies/` those enforcement points load.
MIT licensed, public: `github.com/Parapet-run/parapet-agenticai-sdk`.

Policy *authoring*, tenancy, billing, the operator console, and fleet/audit
aggregation live in a **separate, private repo** ("the control plane" /
`parapet-platform`), which is never on the decision path and never evaluates
Cedar. It consumes this package as an ordinary versioned PyPI dependency
(`parapetai-agent>=X,<Y`), not a workspace member. **This repository is the
single source of truth for the Cedar engine.** A second copy of it lived in
the control-plane repo once and re-diverged ~200 lines within a day of being
reconciled — that's the one failure this split exists to prevent. If you're
tempted to special-case decision logic for one consumer, don't: `policy/`,
`providers/parsers.py`, `signing.py`, `identity.py`, and `otel/openinference.py`
are shared foundation, imported directly by `gateway/` in this repo and, via
PyPI, by the control plane too.

## Architecture

Two form factors of **one** Cedar engine, chosen by whether you can modify
the agent process:

```
Can embed?  --embed--> parapetai-agent (this repo's src/)
                        Governor / GovernedAgent / GovernedRunner
                        runs Cedar IN the agent's own process

Can't?      --proxy---> parapetai-gateway (this repo's gateway/)
                        base-URL interception, no MITM, no CA distribution
                        agent sets OPENAI_BASE_URL -> gateway -> upstream
                        runs Cedar in a sidecar process instead
```

Both call the exact same `policy.engine.PolicyEngine` / `policy.hooks.
GovernanceHook` / `providers.parsers.Snapshot` — the only difference is where
that code runs. Embed when you can change the app; run the gateway when you
can't (a non-Python agent, or one you don't control).

### The path of one request (either form factor)

```
prompt
  |
  v
[ identity ]        who is calling? every decision is scoped to a caller
  |
  v
[ input  · pre  ]   PII/secrets/injection scanners + Cedar model_call decision
  |                 DENY -> the model never sees the prompt
  v
[ model call    ]
  |
  v
[ tool   · call ]   Cedar tool_call authorization, per tool, by name+args+role
  |                 DENY -> the tool never runs; agent continues
  v
[ output · post ]   groundedness (HHEM/lexical) + SLM judge -> Cedar post decision
  |                 DENY -> the answer is withheld
  v
response (only if every gate allowed)
```

Each gate is one Cedar decision; between gates only booleans/metadata move —
never the content being judged. Full narrative: `docs/ARCHITECTURE.md`.

### Three integration paths into the SDK, one engine

- **`Governor`** (`src/parapetai_agent/govern.py`) — framework-neutral: three
  explicit calls (`check_input` / `authorize_tool` / `check_output`) any loop
  can make. No adapter, no framework dependency. This is what LangGraph,
  CrewAI, the OpenAI Agents SDK, or a bare `while` loop use.
- **`GovernedAgent` / `build_middleware`** (`src/parapetai_agent/maf.py`,
  `maf` extra) — Microsoft Agent Framework. `GovernedAgent` subclasses
  `agent_framework.Agent`; the governable seam is `Agent(middleware=[...])`.
- **`GovernedRunner` / `build_plugin`** (`src/parapetai_agent/adk.py`, `adk`
  extra) — Google ADK. Subclasses `google.adk.runners.Runner`; ADK's own
  governable seam is `Runner(plugins=[...])`, not the `Agent` class, which is
  why this integration is named differently from MAF's rather than forced
  into a shared `GovernedAgent` name (see `adk.py`'s module docstring).
  `InMemoryGovernedRunner` mirrors `google.adk.runners.InMemoryRunner` for
  callers who'd otherwise hit a `TypeError` wiring session/artifact/memory
  services by hand.

`maf` and `adk` are mutually independent extras — installing one never pulls
in the other's framework SDK. Both source identity (`scoped_data.py`) and the
audit/OTel/registry plumbing (`governance_runtime.py`) from the same shared
modules, so switching frameworks doesn't mean relearning identity code.
`GovernanceDenied` (`_exceptions.py`) is catchable without importing any
framework. `GovernanceReviewRequired` (see REVIEW below) subclasses it.

### REVIEW — a third decision outcome, not just allow/deny

`@action("review")` on a Cedar `forbid` marks that deny as escalatable to a
human: `Decision.effect == "review"`. Three properties make this safe to
layer on:

- **`Decision.allowed` stays `False` for a review.** Every caller written
  before REVIEW existed — including a bare `if not decision.allowed`, and
  `GovernanceReviewRequired` subclassing `GovernanceDenied` — keeps blocking
  a held call exactly as it blocked a plain deny.
- **Reviewability requires unanimity**: every determining policy must carry
  `@action("review")`. A hard `forbid` matching alongside a reviewable one
  keeps the deny hard — this is a privilege-escalation guard, not a nicety.
- **The control plane is on the *approval* path, never the *decision* path.**
  Cedar decides `review` locally, with no network call. Collecting a grant
  needs the control plane; an unreachable one just means no approval is
  available — the local deny still stands. A grant is single-use, bound to
  one exact call by a content fingerprint (never a prompt/response preview).

`Governor.wait_for_approval()` is opt-in blocking; the default is to raise
`GovernanceReviewRequired` with a ticket and return immediately — a ~1ms
governance check does not become a multi-minute one by default. See
`docs/adr/0008-review-decision-outcome.md` and `docs/adr/0009-approval-loop.md`.

## Non-negotiable invariants

Violating any of these is a security bug, not a style preference. Sourced
from `src/parapetai_agent/policy/engine.py`'s own header and `docs/adr/`.

1. **Fail closed.** An unparsed payload, an evaluation error, or a missing
   policy denies. No exception path becomes an implicit allow.
2. **Cedar is default-deny.** No matching `permit` is a Deny; `forbid` always
   beats `permit`, unconditionally, regardless of file order.
3. **A bad or unreachable bundle never empties the policy set.** Reload keeps
   the previous generation on failure.
4. **Prompt/response content is never logged unless explicitly opted in.**
   The decision audit record and OTel spans are content-free by construction
   (`policy/hooks.py`'s `content_free()` strips `*_preview` keys from every
   audited context), not by configuration — there's no flag that turns
   content on for the standard path.
5. **REVIEW is a deny, not a soft allow** — see above. Unanimity across
   determining policies; `Decision.allowed` stays `False`.
6. **An unreachable control plane cannot soften an enforcement decision.**
   It can cost you an approval opportunity or a policy refresh; it can never
   turn a deny into an allow.

## Extras

| Extra | Brings in | For |
|---|---|---|
| `maf` | `agent-framework`, `mcp`, OTel SDK + OTLP exporter | Microsoft Agent Framework integration and OTel export |
| `adk` | `google-adk`, OTel SDK + OTLP exporter | Google ADK integration and OTel export |
| `web` | `starlette` | `IdentityMiddleware`, JWT bearer extraction |
| `judge` | `litellm` | Provider-agnostic SLM-judge backend (Anthropic, Bedrock, Vertex, Groq, Ollama). Not needed for the default `slm` backend. |
| `dev` | `pytest`, `ruff`, `mypy`, ... | Local dev / CI only |
| _(base)_ | `cedarpy`, `httpx`, `cryptography`, `structlog`, `opentelemetry-api` | Cedar engine, control-plane protocol client, Ed25519 PEP identity |

The base install never imports a web framework or an agent framework — a CLI
script or background worker can depend on it without pulling either in. The
HHEM groundedness backend (`transformers`+`torch`) is deliberately **not** an
extra at all (would drag the CUDA wheel stack into every install) — install
explicitly per `docs/GROUNDEDNESS_HHEM.md`.

## This repo is a uv workspace

Root package **is** the published `parapetai-agent` (not restructured into a
member) — `pip install parapetai-agent` is unaffected by the workspace.
Members: `gateway/` (`parapetai-gateway`) and `mcp-server/` (`parapetai-mcp`).

**Gotcha:** a bare `uv sync` installs the root only. Gateway/mcp-server tests
run under `uv run --package <name>` — running them from the root venv fails
with `ModuleNotFoundError: parapetai_gateway`.

## Dev loop

```bash
make install        # uv sync --all-extras
make test            # test-sdk + test-gateway
make test-sdk         # uv run --extra maf --extra adk --extra judge --extra dev pytest tests -q
make test-gateway      # uv run --package parapetai-gateway --extra dev pytest gateway/tests -q
make lint              # ruff check (src, tests, gateway/src, gateway/tests, AND examples/ deliberately)
make typecheck          # mypy --strict on src and gateway/src
make check                # lint + typecheck + test -- what CI runs
make conformance           # the live cross-framework subset of tests/test_maf.py
make docs                   # build the docs site (mkdocs build --strict); make docs-serve for live preview
```

Single test:

```bash
uv run --extra maf --extra adk --extra judge --extra dev pytest tests/test_adk.py -v
uv run --extra maf --extra adk --extra judge --extra dev pytest tests -k "GovernedAgent and not live" -v
uv run --package parapetai-gateway --extra dev pytest gateway/tests/test_streaming.py -v
```

`ruff check` deliberately lints `examples/` too — they arrived here unlinted
from the control-plane repo with stale cross-repo paths, and unlinted example
code is exactly how customers copy a bug: it's the first thing anyone pastes.

## Testing layers — two things both called "conformance"

- **`tests/`** — the published SDK's own suite: `policy/engine.py` +
  `policy/hooks.py` (red here = a rule or the engine itself broke, and
  `gateway/` depends on this code directly, so check gateway too),
  `control_plane.py`/`pep_identity.py`/`signing.py` (the PEP↔control-plane
  protocol), `maf.py`/`adk.py` (framework adapters), `govern.py`
  (framework-neutral), `content_checks.py`/`groundedness.py`/
  `response_judge.py` (input/output evals), `identity_middleware.py`/
  `identity_store.py`/`token_identity.py`, and
  `test_conformance_frameworks.py` — proof the block happens in the **real
  runtime** of MAF, the OpenAI Agents SDK, LangGraph, and CrewAI (a
  different thing from the item below despite the similar name).
- **`gateway/tests/`** — the standalone HTTP-interception PEP: forwarding,
  streaming (never buffer/reorder SSE), credential forwarding
  (passthrough vs. broker mode), client fingerprinting, prompt logging,
  review approvals. Red = **gateway** code broke, not the shared engine.
- **`conformance/`** (the directory) — hermetic fixtures the two suites
  above spawn (`fake-upstream/`, an OpenAI-shaped canned server;
  `mcp-probe/`, a minimal MCP server/client) **plus** a separate Docker-based
  per-framework harness (`conformance/frameworks/{autogen,crewai,langgraph,
  openai-agents}/`) that proves the **gateway's base-URL interception**
  actually routes for a given client library, tracked in `matrix.yaml` by
  status (`verified` requires a green test at a pinned version — that rule
  is the product; `probable`/`unknown`/`unsupported` may never be
  represented to a customer as "supported"). Do not conflate this with
  `test_conformance_frameworks.py` above — one proves the SDK adapters work
  in-process, the other proves the gateway proxy routes for a given
  framework's HTTP client.

Conflating these wastes debugging time: when the Docker matrix goes red you
want to know instantly that your SDK code is fine and a vendor changed
something.

## Packaging and publishing

`release.yml` publishes **two** packages from **one workflow file**, as two
jobs gated by tag prefix — not one file per package. `pypi` (tag `v*`)
builds and publishes `parapetai-agent`; `pypi-mcp` (tag `mcp-v*`) builds and
publishes `parapetai-mcp`. Both jobs use `uv build --package <name>`
(load-bearing — a bare `uv build` in a workspace builds every member) and
check the tag against that package's own `pyproject.toml` before publishing.
**Both jobs use the identical `environment: release`.** That's deliberate,
not a copy-paste leftover: PyPI's Trusted Publisher registration is keyed on
`(repo, workflow FILENAME, environment name)`, not on which PyPI project is
publishing — the same GitHub identity can be a trusted publisher for
multiple PyPI projects at once, and `parapetai-mcp`'s trusted-publisher entry
was registered against this exact file and environment name to match
`parapetai-agent`'s, rather than getting its own. **This means the mcp
release job cannot be moved to a separate workflow file** without also
re-registering PyPI's trusted publisher for `parapetai-mcp` — the OIDC
token's `workflow` claim is the file path, and a same-named file elsewhere
doesn't satisfy it. The GitHub Actions `environment:` name must match the
PyPI trusted-publisher config **exactly** in general — a mismatch fails as
`invalid-publisher`, which reads like a missing publisher rather than a
one-word disagreement; this has already cost a release cycle once (see the
comments in `release.yml` and `fix/release-environment-name`, PR #3).

`gateway/` (`parapetai-gateway`) is MIT and carries its own
`[project.scripts]` console entry point (meant to be `uvx
parapetai-gateway`-installable eventually) but has **no publish workflow at
all** — deliberately deferred, since nothing has been registered as a PyPI
trusted publisher for it yet. Don't add a `release-gateway.yml` (or a
`pypi-gateway` job in `release.yml`) speculatively; build it once a
trusted-publisher registration actually exists for `parapetai-gateway`'s
PyPI project, and ask first whether it should share `release.yml`/
`environment: release` (like `parapetai-mcp` ended up doing) or get its own
file/environment — that choice depends entirely on how it gets registered on
PyPI, which is done outside this repo.

`CHANGELOG.md` is kept current through `[0.4.2]`; the workspace's
independent packages (`parapetai-gateway`, `parapetai-mcp`) do not share this
changelog or a version number with `parapetai-agent` or each other.

## Known gaps

Unbuilt or undocumented, not broken. Don't investigate these as bugs.

- `gateway/README.md` links `docs/adr/0002-base-url-over-mitm.md` and
  `docs/adr/0003` (credential mode); neither file exists in `docs/adr/` here
  — only 0006, 0008, 0009 made it across the repo split. The decisions they
  describe are real and enforced (see `gateway/README.md`'s own inline
  summary of each), just not preserved as ADR files in this repo yet.
- `conformance/matrix.yaml`: only `langgraph`, `crewai` (OpenAI path only),
  and `autogen` are `verified`; `openai-agents` is `probable`; `adk`,
  `llamaindex`, `haystack`, `mastra`, `maf` (gateway's own Azure path),
  `foundry`, `dify` are `unknown`. Do not describe an `unknown`/`probable`
  framework as gateway-supported without adding a green test first.
- HHEM groundedness backend needs `pip install transformers torch` by hand
  (deliberately not a declared extra) — see `docs/GROUNDEDNESS_HHEM.md`.
- `maf_webapp/` (a long-running MAF web-app demo referenced from
  `examples/README.md`) is deliberately **not** in this repo — it's built
  and deployed to Azure from the control-plane repo's `deploy/azure/`
  scripts, since it's operated infrastructure rather than a sample to copy.
- `parapetai-gateway` has no PyPI publish path yet — deferred until a
  trusted publisher is registered for it (see Packaging and publishing
  above). `uvx parapetai-gateway` in `gateway/README.md` is the target
  contract, not yet a CI-verified fact.

## Working agreements

- **Anything that changes the decision path belongs here, in this repo —
  never in the control-plane repo.** If you're editing Cedar evaluation,
  identity plumbing, a framework adapter, the gateway proxy, or the signing
  scheme, this is the only place that change can live; the control plane
  only ever consumes a released version of it over PyPI.
- **Never weaken a fail-closed path.** No error branch may become an
  implicit allow. Touching a decision path means adding a test that proves
  it still denies on failure (`CONTRIBUTING.md`).
- **Keep the audit content-free.** Don't add prompt/response content to a
  decision record, span, or log without an explicit, documented opt-in.
- **Base install stays lean.** Core imports neither a web framework nor an
  agent framework; framework code stays behind a guarded, optional-extra
  import (`__init__.py`'s three `try/except ImportError` blocks are the
  pattern to follow for a new integration).
- **A new framework adapter's own-specific work is exactly one thing**:
  building a `Snapshot` from that framework's request/response objects, and
  calling `GovernanceHook.evaluate()` at that framework's own hook point.
  Everything else — the engine, the hook sequence, audit, OTel, identity — is
  already framework-agnostic; reuse it rather than re-deriving it
  (`policy/hooks.py`'s own module docstring states this explicitly).
- Every provider/framework claim needs a fixture or a conformance test, not
  an assertion in a doc — `conformance/matrix.yaml`'s status field is the
  enforced version of this rule for the gateway's client-library coverage.
- **The `/docs` site (`mkdocs.yml`) is living documentation, not a
  one-time snapshot — keep it in sync with every change that would make
  it wrong.** A PR that changes a public constructor signature
  (`Governor`, `GovernedAgent`, `GovernedRunner`, `governed_identity`,
  `Decision`, an exception's attributes), adds/removes/renames an
  environment variable, adds or changes a `parapetai-mcp` tool or skill,
  moves a framework's conformance status in `conformance/matrix.yaml`, or
  adds a new framework integration, updates the matching page(s) under
  `docs/` in the **same** change — not as a follow-up. The reference pages
  under `docs/reference/` are hand-authored against the real
  signatures/docstrings (no mkdocstrings/autodoc), specifically so they
  can carry "how to invoke this" guidance a docstring dump can't — that
  means they drift if not updated by hand alongside the code. Verify with
  `make docs` (`mkdocs build --strict` — fails the build on a broken
  internal link) before considering docs-affecting work done; `make
  docs-serve` for a live-reload preview while writing.

## Where to look

- The [docs site](https://parapet-run.github.io/parapet-agenticai-sdk/)
  (built from `docs/` + `mkdocs.yml`) — installation, quickstart, the
  framework guides, the full `Governor`/`GovernedAgent`/`GovernedRunner`/
  `governed_identity`/`Decision` API reference, every environment
  variable, and the `parapetai-mcp` CLI/tools/skills reference. Start
  here before re-deriving something from source that's already written up.
- `docs/ARCHITECTURE.md` — the request path, fail-closed, the two-plane split
- `docs/CONTROL_PLANE_API.md` — the exact HTTP protocol/signing scheme a PEP speaks
- `docs/OBSERVABILITY.md` — decisions as content-free OTel spans
- `docs/GROUNDEDNESS_HHEM.md` — the two output-faithfulness backends
- `docs/maf-integration-pattern.md` — the one wiring pattern behind all seven `maf_sample_0N` ports
- `docs/adr/0006` — `@stage`/`@action` Cedar policy annotations (pre/post, ALTER)
- `docs/adr/0008` / `docs/adr/0009` — REVIEW as a decision outcome, and the approval loop that resolves one
- `examples/INTEGRATION_GUIDE.md` — every way to construct/invoke a `GovernedAgent`/`GovernedRunner`, cross-referenced to the example that demonstrates it
- `examples/same_prompt_every_framework/` — the fastest way to see what integration costs across frameworks
- `CONTRIBUTING.md` — PR ground rules, security-issue reporting
