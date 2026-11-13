### Problem or Use Case

We built [Claude Idle Compact](https://github.com/iamomiid/hermes-claude-idle-compact),
an experimental standalone Hermes plugin that prepares a summary at about 90% of
a one-hour prompt-cache lifetime and adopts it when the user returns. The goal is
to avoid paying to reread a large cold conversation merely to summarize it.

We want to remove the prototype's private imports and submit a catalog-compliant
implementation. This is a concrete consumer of a host-owned context-maintenance
API, not a request to list the prototype or waive rules 9/11.

The current public surfaces don't cover two operations together:

1. A cached-prefix summary on the live session's actual approved Claude route and
   account, with the credential kept entirely in core. `ctx.llm.complete()` cannot
   retain the native system/tools/thinking/cache-marker payload. The public
   `credentialed_provider_request` provider registry lists `openai-codex` but not
   `anthropic` on main at the time of this request.
2. Adoption of a completed summary through the existing compressor's normal
   conversation lease, memory/checkpoint checks, boundaries, protected head/tail,
   redaction and persistence, without overriding `_call_summary_llm` or resolving
   private resident-agent registries.

The prototype uses the internal native client builder and compressor, so it is
explicitly marked ineligible for the catalog until this dependency is resolved.
Its tests use synthetic transcripts, fake credentials and a mock HTTP transport;
we are not presenting live cache savings as established results.

### Proposed Solution

Expose a small host-owned preparation/adoption capability for context-maintenance
plugins. Exact names are up to maintainers. One possible design is:

- Hooks receive an opaque session/request reference and non-secret cache timing
  and usage metadata. The reference binds the profile, model, account, request
  generation and cache prefix; it is not an auth token or a stored `next_call`.
- A public method prepares a checkpoint from that reference using the live
  provider's approved transport. Core appends the summary instruction, keeps the
  original prefix unchanged, reports token/cache usage, respects cancellation and
  a latest-start deadline, and refuses silent retries or account/model fallback.
- A public method registers a completed candidate for the next eligible
  compaction, or core performs adoption on return. Core validates the unchanged
  snapshot and owns the actual transcript commit. No background mutation of the
  live conversation is required.

The plugin would only decide when to request preparation: interactive sessions,
large enough context, actual one-hour history caching, timer resets on new
activity, bounded concurrency and cancellation on close/reset. Core would own all
credential and transcript authority. The same primitive could serve other context
plugins and other providers without putting a Claude-specific timer in core.

If native Claude subscription requests are intentionally excluded from the
public request surface, please confirm which approved Claude route should be
targeted. The catalog build will not work around that policy with its own OAuth
client, credential store or vendor-client impersonation.

### Alternatives Considered

- Related #152139 requests public compressor access and shared token/threshold
  helpers. This request adds token-opaque cached-prefix preparation and safe
  prepared-summary adoption; simply exporting the class is insufficient.
- #152499 provides a read-only compaction preview, not a completion or commit path.
- #117886 discusses background compaction. This use case keeps the live
  transcript unchanged while away and prepares specifically before cache expiry.
- A cron job starts a separate session; its summary request does not automatically
  share the original cache prefix or account/session authority.
- Saving and invoking middleware `next_call` after the invocation is not a
  documented lifetime contract. Copying the compressor or handling tokens inside
  the plugin preserves the catalog concerns, so neither is the proposed solution.

### Feature Type

Developer experience / performance and reliability.

### Contribution

We can adapt the plugin and contribute a focused implementation with behavior
tests once maintainers confirm the intended public API and supported Claude route.
