# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

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
