# ADR-0004: Capability-declaring adapters; observation is the only required capability

- **Status**: Accepted
- **Date**: 2026-07-11

## Context
Target agents differ wildly: some offer hooks and headless APIs, some only leave
transcript files, none document their session formats as stable contracts. A single
rich mandatory interface would either exclude most agents or force adapters to lie
(stub methods that fail at runtime).

## Decision
Adapters declare `AdapterCapabilities`; only observation is mandatory. Clients render
controls from capabilities. Adapters report typed observations through an
`AdapterContext` rather than publishing events directly. A conformance test suite
enforces capability honesty and crash containment.

## Consequences
Day-one support for "watch-only" integration of any agent that writes logs; graceful
degradation in every client; the core's trust boundary with adapters is explicit.
Cost: clients must handle capability variance — accepted as inherent to vendor
neutrality. Known risk: transcript-format drift upstream. Mitigation: fixture corpora
in adapter CI, tolerant parsers, unknown records preserved as opaque output events.

## Alternatives Considered
**Uniform full interface** (forces lying stubs). **Per-agent core integrations**
(destroys the neutrality that is the project's reason to exist).
