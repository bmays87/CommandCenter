# ADR-0021: Adapter model catalogs and truthful session control

- **Status**: Accepted
- **Date**: 2026-08-11

## Context

Model selection was free-text everywhere: the dashboard's only "list" of
models was a hardcoded three-item datalist hint in one React component
(missing newer aliases such as `fable`), and no API exposed which models an
adapter supports. Separately, model switching was unreliable in two ways:

1. **Stale controllability.** `metadata["controlled"]="true"` was written
   once at launch and never revoked. After a server restart the launcher's
   in-memory session set is empty — `set_model` raises — but the rebuilt
   session still carried `controlled=true`, so the dashboard offered a
   Composer that could only fail.
2. **Invisible state.** `refresh_model` and discovery-driven descriptive
   refreshes published no event, so a successful switch (or the parser's
   re-confirmation of the real model) reached only one process's memory;
   other clients never converged.

Constraints: the core contains zero agent-specific logic; services
communicate via events; adapters declare capabilities rather than being
probed; additive payload fields do not bump event versions.

## Decision

1. **Models are declared in `AdapterMetadata`** (`models: list[ModelInfo]`,
   default empty), like capabilities: declarations, not queries. `ModelInfo`
   is `{id, label, default}` with adapter-native ids. Free-form ids stay
   legal at every API that accepts a model — the catalog feeds pickers, it
   does not validate. No `ADAPTER_API_VERSION` bump: the field is additive
   on a model the adapter constructs, and the Protocol is unchanged.
2. **`GET /api/adapters`** returns every *started* adapter as an
   `AdapterInfo` (name, version, capabilities, models), served by
   `AdapterManager.describe_adapters()`. `AdapterInfo` lives in
   `prodeo.adapters.interface` so non-dashboard clients (Mjolnir) import the
   same model. The `adapter.loaded` payload gains `models` (additive, v1).
3. **The adapter asserts controllability in every descriptor**: the
   claude-code adapter writes `controlled` = whether the session is in its
   owned set, on every discovery pass, so a restart's first discovery
   overwrites the stale launch-time `true`. `controlled` is documented as a
   well-known metadata key with generic semantics ("this adapter instance
   can control this session right now"); the core never computes it, only
   relays it.
4. **New event `session.updated` (v1)**, published only by the
   SessionRegistry when *descriptive* fields materially change (model,
   title, permission mode, metadata) — from discovery upserts (diff-gated),
   `refresh_model`, and `refresh_permission_mode`. Its fold during rebuild
   copies descriptive fields only; state authority stays with
   `session.state_changed`.
5. **Switching stays optimistic-then-corrected**: the immediate
   `refresh_model` after a successful adapter call fans out via
   `session.updated`, and the transcript parser's later re-confirmation
   corrects every client if the agent silently normalized or refused the
   model. `AdapterOperationError` maps to HTTP 502 so adapter-side refusals
   are distinguishable from server bugs.

## Consequences

- The dashboard renders real model dropdowns (new-session form and the
  Composer) from adapter declarations; new aliases ship as adapter releases,
  not frontend edits. Controls are capability-gated per adapter.
- After a restart, observed-but-unowned sessions stop offering controls that
  can only fail; `controlled` is truthful within one discovery tick.
- Every client converges on model changes through `session.updated`; the
  event log now records descriptive drift, which also makes rebuilds
  faithful for model/title changes.
- Cost: discovery must diff before publishing (or it would flood the log at
  one event per session per tick), and the fold rule (descriptive-only) must
  be respected by every future consumer or rebuilds could resurrect stale
  states.

## Alternatives Considered

- **A `list_models()` Protocol method** — forces ADAPTER_API_VERSION 6,
  touches `ObserveOnlyAdapter` and the conformance suite for what is a
  static list; the underlying SDK offers nothing dynamic to query anyway.
- **A `/api/models` endpoint** — models are meaningless without the adapter
  that accepts them; returning adapters-with-models also serves capability
  gating and the voice client's adapter discovery with one endpoint.
- **Frontend-only fix (add `fable` to the datalist)** — leaves the list
  duplicated and static in the frontend; rejected by the user in favor of
  the catalog (the quick fix rides along inside the adapter's declaration).
- **Core-computed controllability** — the core cannot know an adapter's
  ownership; computing it there would smuggle agent-specific logic into
  `src/prodeo`.
