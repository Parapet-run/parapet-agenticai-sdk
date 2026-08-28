# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.4.2]

No functional changes. Release-pipeline verification (tag → build → PyPI
publish via Trusted Publishing).

## [0.4.1]

### Added
- Framework integration examples (`maf_sample_01` through `maf_sample_07`,
  `maf_cli`, `adk_sample_01`, `adk_webapp`, `ungoverned_vs_governed`) moved
  into this repo from the control-plane repo, plus a new
  `same_prompt_every_framework/` example: five frameworks, one Cedar rule,
  the same two prompts, side by side.

### Fixed
- `adk.py`'s `provider_for_request()` reported `"gemini"` unconditionally.
  It now reports the provider actually called.

## [0.4.0]

### Added
- **Resolve a held REVIEW call.** `authorize_tool()` now raises
  `GovernanceReviewRequired` with a ticket instead of only a plain deny;
  `Governor.wait_for_approval()` is an opt-in blocking helper. A grant is
  single-use and bound to one exact call via a content fingerprint —
  approving one call can't be replayed onto another. See
  `docs/adr/0009-approval-loop.md`.
- **The gateway joins the approval loop.** A held call's ticket rides the
  existing 403 refusal (`x-parapetai-review-id` header, or `error.data` for
  MCP clients that never see headers); the client re-presents it on retry,
  and the fingerprint is recomputed from the retried request body so an
  approved call can't be swapped for a different one.

### Fixed
- The release workflow's `environment: release` name didn't match the PyPI
  trusted-publisher config, failing publishes with `invalid-publisher`.

## [0.3.0]

### Added
- `Governor.from_control_plane()` — the framework-neutral embed path can now
  receive **control-plane-authored** policy, refreshed by a background poller.
  Previously only the Microsoft Agent Framework adapter could; every other
  framework was limited to `from_policy_dir()`, i.e. policy files the adopter
  maintained themselves. On an unreachable control plane it falls back to the
  last bundle on disk rather than failing to start; with nothing on disk it
  fails closed.
- **REVIEW decision outcome** — `@action("review")` on a Cedar `forbid` marks
  that deny as escalatable to a human, surfacing as `Decision.effect ==
  "review"`. `Decision.allowed` stays `False`, so any caller that only checks
  `allowed` blocks a held call exactly as it blocked a denied one. Requires
  unanimity across determining policies, so a hard `forbid` matching alongside
  a reviewable one keeps the deny hard. See `docs/adr/0008`.
- **Provider-agnostic SLM judge** (`litellm` backend, extra:
  `parapetai-agent[judge]`). The default `slm` backend builds an
  OpenAI/AzureOpenAI client and cannot reach a non-OpenAI-wire endpoint at all;
  this routes Anthropic, Bedrock, Vertex, Groq, Ollama and the rest through one
  code path rather than a vendor client per provider.

- **Google ADK integration** (`adk.py`: `GovernedRunner`, `ParapetPlugin`)
  behind its own `adk` extra, independent of `maf` -- `pip install
  parapetai-agent[adk]` works without ever importing `agent_framework`, and
  vice versa. Both source their shared runtime from `governance_runtime.py` /
  `scoped_data.py`, so a developer picks a framework without inheriting the
  other's dependencies.
- **The gateway PEP now ships from this repo** (`gateway/`), MIT-licensed and
  publishable, with a console script: `uvx parapetai-gateway`. It is the same
  enforcement role as this package in a different form factor — for apps that
  cannot embed the SDK, and for agents that aren't Python at all. `parapetai-mcp`
  and the conformance suite moved here too. The repo is now a uv workspace; the
  root remains the published `parapetai-agent` package, so `pip install
  parapetai-agent` is unchanged.

### Changed
- Control-plane bootstrap (identity registration, first fetch, disk-vs-memory
  policy load, heartbeat, poller thread) extracted to
  `control_plane.bootstrap_engine()` and shared by both embed paths. Two copies
  meant two sets of outage semantics.
- This repo is now the single source of truth for the Cedar engine. A second
  copy lived in the private platform repo and had re-diverged ~200 lines within
  a day of being reconciled; two engines means the control plane can author
  policy the enforcing SDK cannot execute.

### Fixed
- `pip install parapetai-agent[adk,web]` was **unsatisfiable**: the `web` extra
  pinned `starlette>=0.38,<1.0` while `google-adk>=2.7` requires
  `starlette>=1.3.1`. A developer could have the ADK integration or
  `IdentityMiddleware`, never both. The `web` bound is widened to `<2.0` --
  `identity_middleware.py` touches only `BaseHTTPMiddleware`, `Request`,
  `Response` and `ASGIApp`, the stable core unchanged in starlette 1.x.
- The heartbeat `version` field reported the **gateway's** package version
  (`parapetai-gateway`) rather than this SDK's — a copy-paste from the
  gateway's own helper. Since that package is normally absent from an embedded
  SDK, every SDK PEP reported `0.0.0-dev`. Now reports `parapetai-agent` via
  `control_plane.sdk_version()`. The same bug had been copied into
  `governance_runtime.installed_version()`; that now delegates to the one
  implementation rather than becoming a third copy.
- Three near-identical ~90-line control-plane bootstraps (in `maf.py`,
  `adk.py`, and `Governor.from_control_plane`) collapsed into
  `control_plane.bootstrap_engine()`. Three copies meant three sets of outage
  semantics, so "the agent acts as configured" could differ by which
  integration a developer picked.
- `GovernanceDenied` had two definitions (`_exceptions.py` and
  `governance_runtime.py`). Two same-named classes look identical and fail
  every `except` that caught the other; `governance_runtime` now re-exports
  the one in `_exceptions`, which needs no framework to import.

## [0.2.0]

Framework-neutral governance and cross-framework conformance.

### Added
- `Governor` — a framework-neutral `govern()` facade (`from_policy_dir`,
  `check_input` / `authorize_tool` / `check_output`, and a `@gov.tool`
  decorator) so any Python agent framework can enforce policy without a
  dedicated adapter. Built on the same `GovernanceHook` / `PolicyEngine` core as
  the Microsoft Agent Framework adapter.
- Cross-framework conformance suite proving the block happens end-to-end in the
  real runtime of the Microsoft Agent Framework, OpenAI Agents SDK, LangGraph,
  and CrewAI (`tests/test_conformance_frameworks.py`).

### Changed
- `GovernanceDenied` now lives in `parapetai_agent._exceptions` and is exported
  from the top-level package, so it can be caught without importing any
  framework-specific module. The Microsoft Agent Framework adapter re-exports the
  same class (import site unchanged).

## [0.1.1]

Initial public release of the open-source Parapet agentic-AI SDK, extracted from
the Parapet platform into its own repository.

### Added
- In-process governance middleware for the Microsoft Agent Framework
  (`GovernedAgent`, `build_middleware`).
- Cedar policy engine with pre/tool/post stage split, default-deny, fail-closed.
- Input guardrails: PII / secrets / injection / profanity scanners.
- Output evals: groundedness (lexical default, optional HHEM backend) and an SLM
  judge with rubric scoring.
- Caller identity binding (`set_identity` / `use_identity`, `IdentityMiddleware`).
- PEP <-> control-plane HTTP client: signed bundle pull, heartbeat, key
  registration (Ed25519).
- Content-free decision export over OTLP (`configure_otel`).
