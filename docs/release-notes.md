Experimental public source release for native Claude sessions in Hermes.

- One-hour cache eligibility and preparation around minute 54.
- Reset/cancellation on activity; unchanged-transcript checks before adoption.
- Bounded background work, expiry checks after sleep and observed cache-hit gating.
- Existing Hermes compressor owns the foreground compaction and persistence.
- 38 local offline tests, including real host/SDK contracts with fake credentials
  and mock HTTP; all 15 Hermes validation checks pass with disclosed internal-API
  warnings.
- MIT license, configuration schema, private security reporting and pinned CI.

This alpha is **not catalog-approved**. It still uses internal Hermes compressor
and native-client APIs, and temporarily receives the host-resolved credential.
Those are prototype limitations, not accepted catalog exceptions. A supported,
token-opaque host API is requested in
[NousResearch/hermes-agent#155029](https://github.com/NousResearch/hermes-agent/issues/155029).

No live cache savings are established by the synthetic tests. Preparing a summary
consumes tokens; a changed credential, expired cache, external memory provider,
small context or unsupported route causes a skip. Read the README and SECURITY.md
before experimental standalone installation. Catalog submission follows only after
the documented public-API dependencies are resolved.
