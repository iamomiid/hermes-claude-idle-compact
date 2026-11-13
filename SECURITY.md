# Security and data handling

For a vulnerability or a possible credential leak, use this repository's
[private vulnerability reporting](https://github.com/iamomiid/hermes-claude-idle-compact/security/advisories/new).
Public issues must contain synthetic examples only.

The current alpha is an experimental standalone installation, not an approved
Hermes catalog plugin. It imports internal Hermes client and compressor APIs and
temporarily holds a host-resolved Claude credential while building the request.
The catalog-compatible host-owned request API is an upstream dependency; see
[catalog readiness](docs/catalog-readiness.md).

Captured requests, conversation snapshots and prepared summaries are retained in
process memory only. The plugin does not write them to disk. It does not read
credential files, refresh OAuth grants, or store credentials itself. It uses
Hermes' profile-scoped resolver and native client. It refuses credential changes
and custom hosts at the plugin eligibility boundary, and never retries a
failed background summary on a different account or provider.

The summary request goes to `https://api.anthropic.com`. The plugin sends no
analytics or telemetry to its author. Operational logs include session IDs,
status and token counts, without message text, summaries or credentials.
Hermes' ordinary application logs and provider logging remain under the host's
configuration. The summary consumes the selected Claude account's allowance.

Report reproductions with synthetic transcripts and fake credentials. Never
attach `auth.json`, `.env`, Claude CLI credentials, or a real request capture.
