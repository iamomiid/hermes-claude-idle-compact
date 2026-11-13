# Changelog

## 0.1.1-alpha.1 — 2026-11-13

- Prepare the standalone project for public development: MIT license, security
  reporting, contribution instructions, CI and release metadata.
- Declare all six configurable settings and the tested Hermes version floor.
- Reject non-finite settings, boolean token limits and invalid session caps.
- Capture requests only when this profile explicitly selects the context engine.
- Add an isolated offline test runner that can require host integration tests.
- Document the host API dependencies blocking catalog admission. This alpha does
  not claim catalog approval or live cache-savings verification.

## 0.1.0 — 2026-11-12

- Initial standalone Claude-only Hermes plugin.
- Prepare summaries before one-hour cache expiry and adopt them on return through
  the normal Hermes compressor.
- Include request-prefix, OAuth, expiry, cancellation and stale-summary tests.
