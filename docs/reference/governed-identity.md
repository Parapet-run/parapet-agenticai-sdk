# `governed_identity`

A context manager that asserts the *end user's* identity for the duration
of a governed call — separate from the agent's own static identity
(`agent_id`/`Caller`), which is set once at construction. Cedar policies
that gate on `context.identity_claims`/`context.identity_roles` (like the
quickdemo's org-scoped rules) see whatever this sets.

!!! warning "Two implementations, not one"
    There are **two distinct `governed_identity()` definitions** in this
    codebase, both context managers, both meant to be imported by end
    users — but with different signatures. Which one you get depends on
    where you import it from.

## `parapetai_agent.scoped_data.governed_identity`

The base implementation. This is what `parapetai_agent.adk` re-exports
verbatim (`from parapetai_agent.scoped_data import governed_identity`) —
so for **Google ADK**, this is the one you use.

```python
from parapetai_agent.scoped_data import governed_identity
# or, equivalently, for ADK:
from parapetai_agent.adk import governed_identity

@contextlib.contextmanager
def governed_identity(
    *,
    claims: Mapping[str, Any] | None = None,
    roles: Sequence[Any] | None = None,
    token: str | None = None,
    extractor: TokenIdentityExtractor | None = None,
) -> Iterator[None]: ...
```

| Parameter | Type | Meaning |
|---|---|---|
| `claims` | `Mapping[str, Any] \| None` | Already-parsed end-user identity claims, e.g. `{"org": "Sales", "name": "Tony"}`. Set ambiently via `current_identity()` for the duration of the `with` block. |
| `roles` | `Sequence[Any] \| None` | Already-parsed end-user roles, e.g. `["OrderViewer"]`. Used together with, or independently of, `claims`. |
| `token` | `str \| None` | A raw bearer token, decoded via `identity_from_bearer_token()` into both end-user identity and (if present) agent identity. |
| `extractor` | `TokenIdentityExtractor \| None` | Only relevant with `token=` — overrides the default `JwtIdentityExtractor()` for a non-JWT token format. |

**Exactly one** of `(claims and/or roles)` or `token` must be given:

- **Zero sources**: raises `ValueError`. If there's genuinely no identity
  to assert, call the framework's own run method directly, unwrapped,
  rather than this with nothing in it.
- **More than one source**: raises `ValueError`. Passing both
  `claims`/`roles` and `token=` almost always means one was left in by
  mistake while editing — there's no defined merge semantics across two
  identity sources to fall back on.

This fails **loud**, not silent, on ambiguity — unlike an *unwrapped* call
(which evaluates against empty `identity_claims`/`identity_roles`; Cedar's
own default-deny already makes that the safe failure mode for any policy
that checks identity: denied, not skipped or bypassed).

```python
from google.adk.agents import Agent
from parapetai_agent.adk import GovernedRunner, governed_identity

runner = GovernedRunner(agent=root_agent, app_name="demo", session_service=...)

with governed_identity(claims={"org": "Sales", "name": "Tony"}):
    async for event in runner.run_async(user_id="Tony", session_id=..., new_message=...):
        ...
```

## `parapetai_agent.maf.governed_identity`

A **separate, MAF-specific** definition — richer, with an additional
`credential`/`scope` pair for [azure-identity](https://learn.microsoft.com/python/api/overview/azure/identity-readme)
credentials, since MAF's own `FoundryChatClient` commonly takes exactly
that kind of credential.

```python
from parapetai_agent.maf import governed_identity

@contextlib.contextmanager
def governed_identity(
    *,
    claims: Mapping[str, Any] | None = None,
    roles: Sequence[Any] | None = None,
    token: str | None = None,
    credential: TokenCredential | None = None,
    scope: str = "https://cognitiveservices.azure.com/.default",
    extractor: TokenIdentityExtractor | None = None,
) -> Iterator[None]: ...
```

Same `claims`/`roles`/`token`/`extractor` meanings as above, plus:

| Parameter | Type | Meaning |
|---|---|---|
| `credential` | `TokenCredential \| None` | An azure-identity credential — the **same one** you'd pass to `FoundryChatClient`. Internally dispatches to `identity_from_azure_credential()`. |
| `scope` | `str` | Default `"https://cognitiveservices.azure.com/.default"`. The OAuth scope requested from `credential` when resolving identity. |

**Exactly one** of `(claims and/or roles)`, `token`, or `credential` must
be given — same fail-loud rule as above (zero sources or more than one
source both raise `ValueError`).

```python
from azure.identity import AzureCliCredential
from parapetai_agent import GovernedAgent
from parapetai_agent.maf import governed_identity

async with GovernedAgent(client=client, name="agent", instructions="...") as agent:
    with governed_identity(credential=AzureCliCredential()):
        result = await agent.run(prompt)
```

## Which one do I import?

| You're using | Import from |
|---|---|
| `GovernedAgent` (Microsoft Agent Framework) | `parapetai_agent.maf` |
| `GovernedRunner` (Google ADK) | `parapetai_agent.adk` (re-exports `scoped_data`'s version — no `credential=`) |
| Framework-neutral `Governor` | `parapetai_agent.scoped_data` — pass `roles`/`claims` directly to `check_input()`/`authorize_tool()`/`check_output()` instead; those calls don't read the ambient context `governed_identity()` sets, so wrapping them in it has no effect. |

The ADK re-export has no `credential=`/`scope=` support — if you need an
azure-identity credential inside an ADK agent for some other reason, build
the claims yourself and pass `claims=`/`roles=` instead.
