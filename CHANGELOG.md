# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

### Changed
- Control-plane bootstrap (identity registration, first fetch, disk-vs-memory
  policy load, heartbeat, poller thread) extracted to
  `control_plane.bootstrap_engine()` and shared by both embed paths. Two copies
  meant two sets of outage semantics.

### Fixed
- The heartbeat `version` field reported the **gateway's** package version
  (`parapetai-gateway`) rather than this SDK's — a copy-paste from the
  gateway's own helper. Since that package is normally absent from an embedded
  SDK, every SDK PEP reported `0.0.0-dev`. Now reports `parapetai-agent` via
  `control_plane.sdk_version()`.

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
