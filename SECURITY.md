# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue. Report privately via
[GitHub Security Advisories](https://github.com/ajinkya-cs/rs-radar/security/advisories/new).
You can expect an initial response within a few days.

## Scope notes

- rs-radar runs locally and only makes outbound HTTPS requests to public market-data APIs and, if
  configured, your webhook.
- API keys and webhook URLs are read from environment variables and are never persisted or logged.
- Desktop notifications pass text to the OS notifier as process arguments, never through a shell or
  interpolated script source.
