# Contributing

Use Python 3.10 or newer. Runtime dependencies are supplied by Hermes; scheduling
tests use only the Python standard library.

```sh
python3 scripts/check.py
```

The standalone run explicitly reports skipped Hermes integration tests. To
require all host contracts in a Hermes runtime with the Anthropic extra installed:

```sh
python3 scripts/check.py --require-hermes
hermes plugins validate "$PWD" --json
```

Use Hermes' managed development environment (activate the Hermes checkout's
`activate` script, then return to this repository), or the interpreter built by
the pinned CI job. An unrelated system Python will not contain the host packages.
`scripts/check.py` uses a temporary Hermes data home and blocks socket connections
while tests run. Do not add live provider calls to the default suite. The OAuth
contract test uses the real host client builder and SDK with fake credentials and
an in-memory HTTP transport.

CI runs standalone tests on Linux, macOS and Windows, plus the pinned Hermes
validator and host integration contracts. A passing validator can still report
private API warnings; those remain catalog admission blockers, not waived checks.

Keep provider support restricted to native Claude. Do not read credential stores,
introduce fallback accounts, replay a saved `next_call` after its middleware
invocation, mutate Hermes modules, or suppress compatibility warnings. New host
capabilities must have an upstream public API before the catalog build uses them.

For releases, update `plugin.yaml`, `CHANGELOG.md` and the compatibility evidence.
Only prepare a catalog-entry PR after all requirements in
[catalog readiness](docs/catalog-readiness.md) are met. Do not increase the Hermes
compatibility floor beyond the current release or claim unmeasured cache savings.
