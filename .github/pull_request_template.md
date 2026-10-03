## What & why

<!-- What does this change and what problem does it solve? Link the issue if there is one. -->

## How it was tested

<!-- Tests added/updated, and any manual verification against live data. -->

## Checklist

- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest` passes
- [ ] New behaviour is covered by tests (no network in unit tests: use `tests/fakes.py` or `httpx.MockTransport`)
- [ ] Docs updated (README / `docs/methodology.md`) if user-facing behaviour changed
- [ ] No API keys, webhook URLs or personal data in code, tests or fixtures
