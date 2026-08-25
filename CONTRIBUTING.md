# Contributing

Thanks for your interest in Parapet.

## Development setup

```bash
git clone https://github.com/Parapet-run/parapet-agenticai-sdk
cd parapet-agenticai-sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[maf,web,dev]"
```

## Checks

Everything must pass before a PR is merged:

```bash
pytest            # test suite
ruff check .      # lint
mypy src          # types
```

The package ships `py.typed` — keep it fully typed.

## Ground rules

- **Never weaken a fail-closed path.** No error branch may become an implicit
  allow. If you touch a decision path, add a test that proves it denies on
  failure.
- **Keep the audit content-free.** Do not add prompt or response content to a
  decision record, span, or log without an explicit, documented opt-in.
- **Base install stays lean.** The core must not import a web framework or an
  agent framework. Framework code goes behind the `maf` / `web` extras with a
  guarded import.
- **Cedar semantics are load-bearing.** Default-deny and `forbid > permit` are
  invariants, not preferences.

## Reporting security issues

Please do not open a public issue for a vulnerability. Contact the maintainers
privately first so a fix can ship before disclosure.
