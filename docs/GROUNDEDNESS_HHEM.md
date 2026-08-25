# Groundedness — the HHEM backend

Output groundedness asks: **is the model's answer supported by the source it was
given, or did it make something up?** Parapet ships two backends.

## Lexical (default, zero-dependency)

The default backend is a dependency-free faithfulness proxy: it splits the
answer into claims, checks each for support in the source, and treats a
fabricated number (a wrong price or date) as a hard contradiction regardless of
word overlap. It is honest about being a proxy — good enough to catch blatant
invention, and it ships in the base install with nothing to set up.

```python
from parapetai_agent.groundedness import score_groundedness
r = score_groundedness("The refund window is 90 days.", source="Refund window is 30 days.")
r.grounded        # False
r.contradiction   # True  (90 != 30)
```

## HHEM (optional, higher-accuracy)

For production-grade faithfulness, enable the **Vectara HHEM-2.1-Open**
cross-encoder (Apache-2.0) — a small model that scores factual consistency
between a premise (the source) and a hypothesis (the answer).

It is **not** a declared extra: `torch` drags the whole CUDA wheel stack into a
lockfile, heavy churn for a backend most deployments never enable. Install it
explicitly:

```bash
pip install transformers torch
```

Then select the `hhem` backend in the groundedness config (a bundle entry, or
`score_groundedness(..., backend="hhem")`). A bundle that names `hhem` without
the libraries installed **fails closed** (the check errors and the caller
denies) — it never silently turns groundedness off.

### Running HHEM out-of-process

Loading `transformers`/`torch` into every agent process is heavy and
duplicative. Point the agent at a shared, in-VPC HHEM scoring service instead:

```bash
export PARAPET_HHEM_URL="http://hhem.internal:8080"
```

The agent then POSTs `{premise, hypothesis}` pairs to `{PARAPET_HHEM_URL}/score`
and reads back a consistency score — one model, many agents. Because HHEM is
content-free and stateless (premise + hypothesis in, a float out), a single
endpoint can serve every tenant safely. Swap between in-process and remote with
that one env var; nothing else changes.

## Content-free either way

Both backends run inside your process. Only the **decision** — grounded or not,
and the score — becomes part of the (content-free) audit record. The source
text and the answer never leave the process.
