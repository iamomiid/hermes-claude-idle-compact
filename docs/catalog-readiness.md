# Catalog readiness

The public source release is an experimental standalone plugin. A passing Hermes
validator is not catalog approval. We will not submit a catalog entry that conceals
the following unresolved requirements.

## Completed

- MIT license, maintainer metadata and a Hermes `>=0.23.0` version floor, matching
  the tested host's reported version. The exact tested source is pinned in CI.
- Manifest declarations for actual hooks, middleware and all settings.
- Synthetic tests for cancellation, expiry, credential changes, prefix integrity,
  real host compressor adoption and plugin discovery. No live provider calls.
- Public installation, rollback, data handling, security reporting and contribution
  documentation. No claimed measured savings or implied Nous endorsement.
- CI using immutable action pins and Hermes' own validator/PM environment builder.

## Upstream dependencies — blocking catalog submission

### 1. Host-owned Claude summary requests

The prototype calls Hermes' internal credential resolver and native client builder.
The plugin therefore temporarily receives the resolved credential. Catalog rule
11 requires subscription requests to stay behind the host-owned credentialed
request route. The public `credentialed_provider_request` surface currently lists
`openai-codex`, not `anthropic`; calling it with `anthropic` is not a supported fix.

The needed public operation must preserve the live session's model, account,
profile, system, tools, thinking settings and cache prefix, without exposing a
token. It must not silently fall back to another account/model or retry after the
cache deadline. The plugin must adopt the host's approved Claude transport rather
than invent a sign-in route or vendor-client identity.

### 2. Prepared-summary adoption using the standard compressor

The prototype subclasses `ContextCompressor` and overrides protected summary-call
methods. The documented `ContextEngine` ABC does not expose native prepared-summary
adoption. `ctx.llm.complete()` takes messages but cannot preserve the full native
wire request's system/tools/cache markers. Replacing it with that facade would
silently lose the cache behavior this plugin exists to provide.

A public preparation/adoption operation should let core keep its existing memory
checks, conversation lease, stale-result fence, boundary planning, secret redaction,
protected head/tail and persistence. No core module monkey patch or private-agent
lookup should be needed by the plugin.

Related upstream requests:

- [Public context-engine/compressor APIs, #152139](https://github.com/NousResearch/hermes-agent/issues/152139)
- [Compaction preview, #152499](https://github.com/NousResearch/hermes-agent/pull/152499)
- [Background compaction, #117886](https://github.com/NousResearch/hermes-agent/issues/117886)

The focused proposal was submitted as
[Hermes #155029](https://github.com/NousResearch/hermes-agent/issues/155029).
Its source is [upstream-api-request.md](upstream-api-request.md).

## Admission sequence after those APIs ship

1. Refactor the runtime adapter/engine to use the agreed public APIs and remove
   credential material and protected compressor overrides from plugin code.
2. Test on a released Hermes that contains those APIs; set a truthful released
   `requires_hermes` floor. Do not claim an unreleased future version is supported.
3. Confirm real one-hour cache usage with an opt-in test and retain anonymized
   token evidence. Synthetic tests alone do not establish live cost savings.
4. Run `hermes plugins validate --install-deps` at the exact release commit and
   resolve the private-import warnings under rules 9/11.
5. Add one `plugin-catalog/claude-idle-compact.yaml` entry in a data-only PR to
   `NousResearch/hermes-agent`, with the full release SHA, quoted version, matching
   capabilities, `tier: community`, and network/background/data disclosures.
6. Wait for catalog CI and maintainer review; only their merge publishes the listing.

No pending-entry YAML is presented as installable catalog metadata before these
gates pass. The current alpha remains installable by its public Git URL.

Canonical policy: [Hermes catalog submission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
