# ADR-0003: SQLite is the default store; MongoDB is an optional backend plugin

- **Status**: Accepted (deviates deliberately from the brief)
- **Date**: 2026-07-11

## Context
The brief names MongoDB. But the brief also demands local-first, offline, one-process
simplicity, and Linux-first open source. Requiring a MongoDB server for a tool that
watches log files on a laptop is a heavy install burden; MongoDB's SSPL license also
complicates redistribution/packaging for some downstreams. The workload is an
append-only event log plus modest state — a textbook fit for embedded storage.

## Decision
Define `EventStore`/`StateStore` interfaces. Default implementation: SQLite (WAL
mode, JSON payload columns, ULID keys). Ship `prodeo-storage-mongodb` as a
first-party optional plugin in Phase 2 for users who want server-based storage,
horizontal read scale, or already operate Mongo.

## Consequences
`pip install` + run just works with zero services; backups are one file. The storage
interface is exercised by two real implementations early, which keeps it honest.
Cost: we maintain two backends; mitigated by a shared contract test suite both must
pass.

*Phase 2 status*: the shared contract suite shipped as
`prodeo.persistence.testing.EventStoreContractSuite` (SQLite passes it; see its
docstring for how a backend adopts it). The MongoDB plugin itself was deferred
beyond Phase 2 — the suite plus the `EventQuery` cursor semantics are the
ready-made conformance gate for whenever it (or any other backend) lands.

## Alternatives Considered
**MongoDB only** (fails local-first simplicity; SSPL friction). **Postgres default**
(great engine, same server-dependency problem). **Plain JSONL files** (queryability
and retention management get reinvented badly).
