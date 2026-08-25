# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.1] - Unreleased

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
