# Contributing

Thanks for your interest! Bug reports, new data providers and well-argued signal improvements are all
welcome.

## Setup

```bash
git clone https://github.com/ajinkya-cs/rs-radar && cd rs-radar
uv sync
```

Before opening a PR, run what CI runs:

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

## Ground rules

- **No network in unit tests.** Use `tests/fakes.py` (a synthetic provider) or `httpx.MockTransport`
  (see `tests/conftest.py::mock_http`). Live checks belong behind `@pytest.mark.network`.
- **Keep analytics pure.** `indicators.py` and `signals.py` take values and return values. I/O lives in
  providers and the scanner.
- **Strict typing.** `mypy --strict` must pass.
- **No secrets, ever.** No API keys, webhook URLs or personal data in code, tests, fixtures or logs.
  Error messages should only include URL hosts.
- **Conventional commits**: `feat(scope): …`, `fix: …`, `docs: …`, `test: …`, `ci: …`.

## Adding a data provider

1. Implement the `MarketDataProvider` protocol in `src/rs_radar/providers/` (see `stocks.py` for a
   compact example): `benchmark()`, `universe()`, `resolve()`, `candles()`, plus `trend_interval` and
   `supported_intervals`.
2. Route all HTTP through the shared `HttpClient` so rate limiting and retries apply.
3. Add parsing tests with recorded payload shapes via `httpx.MockTransport`.
4. Wire it up in `config.build_provider` and document it in the README.

## Proposing a signal change

Open an issue first. Describe the logic, what new information it adds beyond the existing features,
and how you validated it. Update `docs/methodology.md` in the same PR as the code.
