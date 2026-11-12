# Claude Idle Compact for Hermes

A standalone, Claude-only Hermes plugin. It prepares a checkpoint while the
conversation's one-hour prompt cache should still be warm, then lets Hermes apply
the checkpoint through its normal compaction pipeline when the next message arrives.

**Status: experimental local implementation, not installed or enabled by creating
this folder.** Tests use fake provider responses; live cache savings have not been
measured. The plugin does not change Hermes core files, your configuration, or T3.

## Behavior

1. Observe the actual native Claude request and its completed assistant response.
2. At a successful turn end, require at least 200,000 provider-reported prompt
   tokens, a compressible middle, enough messages for Hermes' preflight gate, and
   actual one-hour cache markers covering conversation history. Guessing a TTL
   from a model name or config is insufficient.
3. Schedule one preparation at `last_request_start + 54 minutes`. New turns and
   requests cancel/rearm the timer. No LLM runs just to check the clock.
4. Reuse the captured system, tools, messages, thinking settings, and cache markers;
   append the last assistant response and a summary instruction. Hermes resolves
   the credential and builds its native Anthropic client, including OAuth headers.
5. Hold the summary in memory. Do not edit the live transcript in the background.
6. On return, request one foreground compaction only if the transcript still
   matches. The one newly arriving user message is allowed and retained. Hermes
   owns compaction boundaries, protected head/tail, role alternation, secret
   redaction, memory checks, persistence, and commit fencing.

If the timer wakes after cache expiry (including after laptop sleep), it skips
the request. A failed, incomplete, tool-using, or low-cache-hit summary is discarded
without a background retry or cold auxiliary fallback. Normal Hermes size-based
and manual compaction remain available.

## Scope and compatibility

- Only native `anthropic` / `anthropic_messages` requests to
  `https://api.anthropic.com` with a `claude-*` model and an interactive platform.
  No OpenAI, Codex, OpenRouter, proxies, Bedrock, or other providers.
- Cron, subagent, API, webhook and batch platforms are excluded. Bot Chat turns
  caused by routine delivery count as conversation activity and invalidate/rearm
  preparation when they pass through the interactive backend.
- The host process must remain running and keep the agent resident. Closing the
  backend or evicting the agent cancels its preparation. A restart loses pending
  timers and held summaries; conversation history is untouched. There is no daemon,
  cron job, disk transcript dump, or restart recovery process.
- State is isolated by Hermes profile home, session ID and agent instance.
  At most one preparation is in flight per process, with at most 32 retained
  session states. Another busy preparation causes a skip, not queued paid work.
- Uses the host's current credential only if it matches the live agent's
  credential fingerprint. A token refresh, credential rotation, or ambiguous
  account selection skips that idle window. The plugin never selects another
  account to get around a limit.
- Manual/focused/recovery compaction uses normal fresh summaries. External memory
  providers and `compression.checkpoint_required` disable idle preparation in
  this version; their handoff context must be collected at compaction time.
- The default threshold means small chats intentionally do not compact on idle.
  A new compacted prefix still needs a cache write on return, and the prepared
  summary itself consumes tokens. Savings depend on actual cache hits and workload;
  API dollar estimates do not establish a fixed Claude subscription allowance.
- The summary's total output budget includes thinking. A captured fixed thinking
  budget that leaves fewer than 1,024 tokens for summary text causes a skip; the
  plugin never changes thinking settings to force a cache hit or silently raises
  its configured output cap.

Tested against local Hermes source commit
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` (2026-11-12).
Hermes' public plugin API does not currently expose an exact cached-prefix
completion operation. This version therefore has an isolated internal adapter for
the native Claude client and compressor summary seams. **Hermes' validator warns
about those internal imports.** They are not a stable catalog compatibility
contract. Re-run integration tests after updating Hermes. A compatibility probe
checks required methods, but cannot guarantee compatibility with every future
release. Target the normal `in_process` plugin runtime; host-isolated execution
has not been verified.

## Install in one profile

Start with CMO. Run these commands from this plugin directory, after reviewing
the code. They are installation instructions, not steps the creation task ran:

```sh
mkdir -p "$HOME/.hermes/profiles/cmo/plugins"
ln -s "$PWD" "$HOME/.hermes/profiles/cmo/plugins/claude-idle-compact"
hermes -p cmo plugins enable claude-idle-compact
hermes -p cmo config set context.engine claude_idle_compact
hermes -p cmo config set prompt_caching.cache_ttl auto
```

Only one context engine can be active. Do not combine with another context-engine
plugin. Keep ordinary compression enabled. Leave
`compression.idle_compact_after_seconds: 0` to avoid a separate on-return
summarization taking precedence. `auto` uses one-hour caching for interactive
sessions and five-minute caching for machine-paced work in the tested Hermes
version; the plugin itself only trusts actual request markers.

Restart the owning Hermes backend between tasks and resume the conversation so
its agent is constructed with this context engine. Enabling hooks during a run
does not replace the context engine on an already-created agent. No conversation
reset/deletion is required. The plugin only starts scheduling after an eligible
completed Claude turn; it does not reconstruct prior requests from old logs.

See [config.example.yaml](config.example.yaml) for optional settings. Merge keys
into the profile's existing configuration; do not replace its plugin list.

To disable:

```sh
hermes -p cmo config set context.engine compressor
hermes -p cmo plugins disable claude-idle-compact
```

Restart the owning backend between tasks to release existing agents and timers.

## Tests

Scheduling tests require Python 3.10+ and the standard library:

```sh
python3 -m unittest discover -s tests -v
```

That command skips the host integration tests if Hermes is not importable. To run
all tests under Hermes' managed runtime:

```sh
hermes --run-module unittest discover -s "$PWD/tests" -v
```

For a completely separate data home, set `HERMES_HOME` to a temporary directory
when launching that command; Hermes may provision its managed dependencies there.
Tests do not send live model requests. The OAuth test uses the real Hermes client
builder and Anthropic SDK with an in-memory mock HTTP transport and fake token.
Coverage includes real plugin discovery/unloading, native compressor adoption,
timer cancellation/reset, expiry after sleep, profile separation, changed-history
rejection, actual request prefix preservation, cache misses, and credential changes.

Validate the plugin:

```sh
hermes plugins validate "$PWD" --json
```

## Observe usage

Hermes logs contain `Claude idle summary scheduled`, `ready`, `abandoned`, and
`usage` entries. Completed summary calls log actual cache-read, cache-write, fresh
input and output token buckets, including summaries later discarded. Logs contain
no prompt or summary text and no credentials. Calls interrupted before final
provider usage becomes available may not have complete token counts.

These direct background calls are not automatically included in the main-loop
session usage ledger or Claude Code's JSONL/ccusage reports. Include the plugin's
usage log entries when auditing spend. `get_status()` also adds a
`claude_idle_compact` status to the context engine's status dict.

## Layout

- `controller.py`: clock, cancellation, generation guards and transcript matching;
  no Hermes or provider dependencies.
- `hermes_adapter.py`: profile/config access, compatibility checks, response
  sanitization, native authentication and Claude summary requests.
- `engine.py`: normal Hermes compression with a prepared summary substituted at
  its summary-call seam; no core monkey patches or direct database writes.
- `__init__.py`: plugin registration and live-request capture.
- `tests/`: standard-library tests, plus optional real Hermes contracts.
