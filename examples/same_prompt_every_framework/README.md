# The same prompt, every framework

Five agent frameworks. One Cedar rule. The same two prompts through all of
them, side by side.

The point is the last column. **The line you write differs per framework; the
decision does not.**

```
  prompt A (allowed): What is the status of incident INC0010026?
  prompt B (denied) : Delete incident INC0010026.

  framework      the line you write                                              lookup    delete
  -------------- --------------------------------------------------------------  --------- ---------
  maf            GovernedAgent(...)           # was: agent_framework.Agent       ran       blocked
  adk            InMemoryGovernedRunner(...)  # was: adk.runners.InMemoryRunner  ran       blocked
  openai-agents  @gov.tool                    # under @function_tool             ran       blocked
  crewai         @gov.tool                    # under @crewai.tools.tool         ran       blocked
  langgraph      @gov.tool                    # under langchain_core @tool       ran       blocked

  5 framework(s) ran. Same rule, same outcome in every one.
```

`policies/10-incident.cedar` forbids `delete_incident`. Nothing else does any
work: no per-framework policy, no per-framework exception handling, no
allowlist in code.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run --extra maf --extra adk python3 examples/same_prompt_every_framework/run_example.py
```

Frameworks you have not installed are **skipped, not failed** — you install the
one you use, and nobody installs all five. To widen the table:

```bash
uv run --extra maf --extra adk \
  --with crewai --with langgraph --with langchain-anthropic --with openai-agents \
  python3 examples/same_prompt_every_framework/run_example.py
```

## Two seams, not five

Reading `adapters/`, the integrations fall into exactly two shapes:

| Shape | Frameworks | Why |
|---|---|---|
| **Swap the class** | MAF (`GovernedAgent`), ADK (`InMemoryGovernedRunner`) | The framework owns the model/tool loop, so governance attaches where that loop lives — the Agent for MAF, the Runner for ADK. |
| **Decorate the tool** | OpenAI Agents, CrewAI, LangGraph | No governable seam of their own worth wrapping, so `@gov.tool` sits under the framework's own tool decorator. |

Which shape a framework needs is a property of that framework, not an
inconsistency in this SDK — see `src/parapetai_agent/adk.py`'s module docstring
for the ADK-vs-MAF case in detail.

## Why each scenario offers only one tool

Offered both tools and asked to delete, Claude declines to call the destructive
one at all — the run reports `not called` and the deny path is never exercised.
That is the *model* being careful, not Parapet working, and a demo that cannot
tell those apart proves nothing. Narrowing the toolset makes the model actually
attempt the call, which is the only way to observe the block.

## Why the verdict comes from the decision stream

This was the hard part, and it is worth knowing if you write your own harness.

**Exceptions are not a reliable signal.** MAF raises `GovernanceDenied` but its
own framework swallows it before your code sees it; ADK never raises at all —
it replaces the tool result. So "no exception and the body did not run" cannot
distinguish *blocked* from *the model never called it*.

An early version of this demo inferred exactly that way and reported a plain
`ImportError` as a successful governance block. A demo whose failure mode is a
false green is worse than no demo.

Every integration emits the same structlog `decision` event, so that is the one
signal common to all five. The run reports:

- `ran` — the body executed **and** Cedar permitted it
- `blocked` — Cedar returned deny/review; the body never executed
- `RAN AFTER DENY` — the failure this exists to catch: executed despite a deny
- `not called` — Cedar was never asked; the model did not call the tool
- `error: X` — the framework broke before reaching governance

The script exits non-zero if any live framework disagrees with the others.
