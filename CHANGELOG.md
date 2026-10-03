# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-03

### Added

- MCP server with 14 tools and 7 slash-command prompts for crypto (vs BTC) and US stocks (vs SPY).
- Beta-adjusted relative strength over 5m / 15m / 1h / 4h, raw movers, and strength since a
  benchmark low or a given time.
- Signal engine: hard gates, 0-100 confidence scoring and trade plans with structure/ATR stops.
- Wilder RSI on 1h / 4h / 1d.
- Per-market watchlists, including building one from a screenshot via the client model.
- Background bot with desktop notifications (macOS, Linux), optional Discord/Slack webhook,
  per-symbol cooldowns and a health heartbeat.
- CLI mirroring every tool, with `--json` output.

[0.1.0]: https://github.com/ajinkya-cs/rs-radar/releases/tag/v0.1.0
