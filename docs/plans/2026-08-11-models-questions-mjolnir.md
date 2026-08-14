Research is complete. Here is the implementation plan.

---

# Implementation Plan: Model Catalog, Reliable Switching, Question Dialogs, Conversational Mjolnir

All paths absolute under `F:\SourceCode\CommandCenter`. Versioning rules applied throughout: **additive optional payload fields do not bump event versions** (docs/architecture/event-model.md, "Additive evolution"); **ADAPTER_API_VERSION bumps only for incompatible Protocol changes** (src/prodeo/adapters/interface.py:18-24, currently 5). None of the four workstreams requires an adapter API bump or an event payload version bump — every schema change below is additive-with-default. Three new ADRs: 0021 (A+B), 0022 (C), 0023 (D).

**Recommended sequencing: A → B → C → D.** B's dashboard gating and D's adapter discovery both consume A's new endpoint. C is independent of A/B server-side but shares the OpenAPI regen; do its regen in its own change.

**OpenAPI regen rule (applies to A, B, C):** within each workstream, land the FastAPI/Pydantic model changes in `src/prodeo/api/app.py` (and core models) first, then run `uv run python scripts/export_openapi.py` and `npm run generate` in `dashboard/`, then write dashboard code against the regenerated `dashboard/src/api/schema.d.ts`. Commit `dashboard/openapi.json` + `schema.d.ts` in the same change — CI diffs them. Never hand-edit schema.d.ts to "get ahead" of the generator.

---

## Workstream A: Adapter-declared model catalog

### Design decisions

1. **Models are declared in `AdapterMetadata`, not the Protocol.** Add a `ModelInfo` Pydantic model and a `models: list[ModelInfo] = Field(default_factory=list)` field to `AdapterMetadata` (src/prodeo/adapters/interface.py:27-30). This is an additive, defaulted field on a model the adapter constructs — old adapters keep working, so **no ADAPTER_API_VERSION bump** (the version history at interface.py:18-24 bumps only for Protocol/control-surface changes). A Protocol method (`list_models()`) was considered and rejected: it would force v6, touch `ObserveOnlyAdapter` and the conformance suite for a static list, and the SDK offers nothing dynamic to query anyway.

   ```python
   class ModelInfo(BaseModel):
       """One model an adapter can launch/switch to. ``id`` is adapter-native
       (an alias or a full model id); free-form ids remain legal everywhere."""
       id: str
       label: str = ""
       default: bool = False
   ```

2. **New endpoint `GET /api/adapters`** (not `/api/models`): returns loaded adapters with name, version, capabilities, and models. This single endpoint also serves Workstream B (capability-gated dashboard controls) and Workstream D (Mjolnir picking a launch-capable adapter). Add an `AdapterInfo` model **in `src/prodeo/adapters/interface.py`** (composing `AdapterCapabilities` + `list[ModelInfo]`) so both the API layer and Mjolnir's client can import it — mirroring how `ClientPresence` lives in `prodeo.presence` and is reused.

3. **`adapter.loaded` gains a `models` payload field** — additive, version stays 1.

4. **Core stays agent-agnostic:** the catalog is pure adapter declaration; core only relays it. No model names appear anywhere in `src/prodeo`.

### Files to modify

- `src/prodeo/adapters/interface.py`: add `ModelInfo`, `AdapterInfo(BaseModel): name, version, capabilities: AdapterCapabilities, models: list[ModelInfo]`; add `models` field to `AdapterMetadata`.
- `src/prodeo/adapters/manager.py`: add public query `describe_adapters(self) -> list[AdapterInfo]` returning one entry per adapter in `self._started`; extend the `ADAPTER_LOADED` publish (manager.py:122-133) with `"models": [m.model_dump() for m in adapter.metadata.models]`.
- `src/prodeo/api/app.py`: `class AdapterListResponse(BaseModel): adapters: list[AdapterInfo]`; `@app.get("/api/adapters", response_model=AdapterListResponse, dependencies=[auth])` delegating to `manager.describe_adapters()`. Place next to the extensions endpoints (~line 758).
- `packages/prodeo-adapter-claude-code/src/prodeo_adapter_claude_code/adapter.py`: module-level `MODELS: Final = [ModelInfo(id="sonnet", label="Sonnet", default=True), ModelInfo(id="opus", label="Opus"), ModelInfo(id="haiku", label="Haiku"), ModelInfo(id="fable", label="Fable")]`; pass `models=MODELS` in the `AdapterMetadata` constructor (adapter.py:67). Declare models unconditionally (even for observe-only builds — the dashboard gates the *controls* on capabilities, not the catalog).
- Dashboard (after regen):
  - `dashboard/src/api/client.ts`: `adapters: () => get<AdapterListResponse>("/api/adapters")`.
  - `dashboard/src/api/types.ts`: export `AdapterInfo`, `ModelInfo`.
  - `dashboard/src/views/ModelInput.tsx`: rewrite as `ModelPicker({ id, value, onChange, models, disabled })`. Render a `<select>`: option `""` = "Agent default", one option per `ModelInfo` (label falling back to id), and a final `custom` sentinel option "Custom model id…" that switches to a free-text input (pre-filled with the current value). If `value` is non-empty and not in `models`, start in custom mode. Delete the hardcoded `SUGGESTIONS` (line 7).
  - `dashboard/src/views/FleetView.tsx` `NewSessionForm` (lines 17-140): replace the extensions-based adapter dropdown (lines 29-30, 52-60) with `useQuery({ queryKey: ["adapters"], queryFn: api.adapters })`, filtered to `capabilities.launch`; pass the selected adapter's `models` to `ModelPicker`; update the help text at lines 80-83 to stop enumerating aliases (the list is now live).
  - `dashboard/src/views/Composer.tsx` `ModelControl` (lines 10-54): same `["adapters"]` query; pass `models` for `session.adapter`; render nothing if that adapter's `capabilities.set_model` is false (part of B but wired here).

### Tests

- `src/prodeo/api/tests/test_rest.py`: `/api/adapters` returns started adapters with capabilities and models; unstarted adapters absent.
- `src/prodeo/adapters/tests/test_manager.py`: `adapter.loaded` payload carries `models`; `describe_adapters` reflects only started adapters.
- `src/prodeo/adapters/testing/suite.py`: add one conformance test — `test_declared_models_are_valid`: every `metadata.models` entry has a non-empty `id`, ids unique, at most one `default=True`. (Passes trivially for empty lists, so existing adapters conform unchanged.)
- `packages/prodeo-adapter-claude-code/tests/test_adapter.py`: metadata declares the four aliases with sonnet as default.
- Dashboard: `npm run build` (tsc strict) is the gate.

### Docs

- `docs/architecture/adapter-specification.md`: new "Model catalog" subsection under the interface — models are declarations like capabilities; empty means "the adapter takes free-form ids only"; the id is adapter-native; free-form ids stay legal at every API that accepts a model.
- `docs/architecture/event-model.md`: no table change (adapter namespace unchanged); mention `models` in the adapter.loaded description only if the payload is documented there (it is not — leave it to the adapter spec).
- ADR-0021 (shared with B, below).

---

## Workstream B: Reliable model switching

### The two actual defects

1. **Stale controllability.** `metadata["controlled"]="true"` is written once at launch (manager.py:186) and never revoked. After a server restart the `SdkLauncher._sessions` dict is empty, `ClaudeCodeAdapter._owned` is empty, so `set_model` → `_require_owned` (adapter.py:182-186) raises — but the dashboard still shows the Composer because the rebuilt `session.discovered` payload carries `controlled=true` (SessionView.tsx:217). Observed (VS Code) sessions correctly never get a Composer today; the restart case is the lie.
2. **Invisible state.** `registry.refresh_model` (registry.py:188-197) publishes no event, and `upsert_discovered`'s descriptive refresh (registry.py:74-78) publishes no event either — so even the parser's re-confirmation of the real model (parser.py:150-154 → `consume_meta_dirty` → `SessionObservation`) reaches only the memory of one process. Other clients never converge; the caller's client converges only via optimistic `setQueryData`.

### Design decisions

1. **The adapter asserts controllability in every descriptor.** In `ClaudeCodeAdapter._descriptor` (adapter.py:267-289), add to metadata: `"controlled": "true" if native_id in self._owned else "false"` — unconditionally, so discovery after a restart *overwrites* the stale `true` via `existing.metadata.update(desc.metadata)` (registry.py:78). Document `controlled` as a **well-known metadata key** in the adapter spec (generic semantics: "this adapter instance can control this session right now"), keeping core agent-agnostic — core never computes it, only relays it. The manager's launch-time seed (manager.py:186) stays as the immediate signal before the first discovery tick.

2. **New event `session.updated` (v1).** Published by the SessionRegistry (still the only writer of `session.*`) whenever *descriptive* fields materially change: payload `{"session": <full dump>, "fields": ["model", ...]}`. Emitted from three places:
   - `upsert_discovered` for existing sessions when title/model/metadata actually changed (compute the diff before mutating; skip publish when nothing changed — this bounds event volume since discovery runs every 10s),
   - `refresh_model`,
   - `refresh_permission_mode` (both become `async def` and are `await`ed at their call sites: manager.py:192, 233, 252).

   **Fold semantics (subtle):** `SessionRegistry._apply` (registry.py:227-240) handles `session.updated` by copying *descriptive fields only* (title, project, model, permission_mode, metadata) onto the existing record — never `state`/`ended_at`. State authority stays with `session.state_changed`; a `session.updated` that raced a transition must not resurrect an old state during rebuild. Orphan `session.updated` (no prior `discovered`) is warned and skipped like orphan state events. Retention already never deletes `session.*` (event-model.md "Retention"), so the fold input is complete.

   This is a new event *type*, not a payload change — additive to the taxonomy, v1, no upcast. Event-model.md table gets `updated` in the session row; coding-standards rule 4 says breaking changes need an ADR — this is non-breaking, but ADR-0021 covers it anyway since it changes rebuild behavior.

3. **Confirmation semantics: optimistic-then-corrected.** Keep the immediate `refresh_model` after a successful adapter call (the POST response already returns the refreshed Session), now with `session.updated` fanning it out to all clients. The transcript parser's model re-confirmation then flows `SessionObservation → upsert_discovered → session.updated` and *corrects* every client if the SDK silently normalized/refused the model. No new adapter round-trip needed.

4. **Error surfacing:** add `AdapterOperationError: 502` to `_ERROR_STATUS` (app.py:90-105) so an adapter-side failure ("session … was not launched by this server") is distinguishable from a server bug; the Composer already renders `err.message` (Composer.tsx:23, 51). Belt-and-braces only — with (1) the dashboard stops offering the control in the first place.

5. **Dashboard gating:** SessionView's existing `metadata.controlled === "true"` gate (SessionView.tsx:217) becomes truthful automatically. Additionally: hide `ModelControl` when the adapter lacks `set_model`, hide `ModeControl` when it lacks `set_permission_mode` (from `/api/adapters`). FleetView/SessionView already invalidate on `session.*` WS events (FleetView.tsx:190, SessionView.tsx:130-141), so `session.updated` propagates with zero new client plumbing — verify the event's `type.startsWith("session.")` paths and leave `TIMELINE_TYPES` as-is (`session.*` already matches). Add a `session.updated` case to `EventRow` (SessionView.tsx:21-107) rendering e.g. "model → sonnet" from `fields`, so it doesn't show as raw "session.updated".

### Files to modify

- `src/prodeo/events/types.py`: `SESSION_UPDATED: Final = "session.updated"`.
- `src/prodeo/sessions/registry.py`: diff-and-publish in `upsert_discovered`; `refresh_model`/`refresh_permission_mode` → async + publish; `_apply` handles `SESSION_UPDATED` (descriptive-only).
- `src/prodeo/adapters/manager.py`: await the two refreshers (lines 192, 233, 252).
- `src/prodeo/api/app.py`: `_ERROR_STATUS[AdapterOperationError] = 502`.
- `packages/prodeo-adapter-claude-code/src/prodeo_adapter_claude_code/adapter.py`: `controlled` in `_descriptor` metadata.
- `dashboard/src/views/Composer.tsx`, `dashboard/src/views/SessionView.tsx`: capability gating + EventRow case.

### Tests

- `src/prodeo/sessions/tests/test_registry.py`: `session.updated` published on model refresh and on discovery-driven metadata change; NOT published when nothing changed; rebuild folds `session.updated` descriptively (state untouched); orphan updated skipped.
- `src/prodeo/adapters/tests/test_manager.py`: `set_model` success publishes `session.updated`; failure raises `AdapterOperationError` and publishes `adapter.error`, no `session.updated`.
- `packages/prodeo-adapter-claude-code/tests/test_adapter.py`: descriptors carry `controlled=false` for observed sessions, `true` for launched; a fresh adapter instance (simulated restart) reports `false` for a previously-owned transcript.
- `tests/integration/test_server_restart.py`: after restart + one discovery pass, the session's `controlled` metadata is false and `session.updated` was logged.
- Conformance suite: no change required (descriptor metadata is free-form).

### Docs

- `docs/architecture/event-model.md`: `updated` added to the session row + a paragraph under the state-machine section (descriptive facts vs state facts; fold rule).
- `docs/architecture/adapter-specification.md`: well-known `controlled` metadata key; note that `set_model` callers should treat the registry model as descriptive until the adapter re-confirms.
- **ADR-0021: "Adapter model catalogs and truthful session control"** — covers A's metadata declaration + endpoint and B's `session.updated` + controlled-key convention.

---

## Workstream C: Real question dialogs (multi-question, multiSelect)

### Design decisions

1. **Generic structured questions in core.** Extend `src/prodeo/mediation/model.py`:

   ```python
   class QuestionOption(BaseModel):
       label: str
       description: str = ""

   class QuestionGroup(BaseModel):
       """One question in a (possibly multi-part) question-kind interaction."""
       id: str                       # stable key the answer's selections use
       prompt: str
       options: list[QuestionOption]
       multi_select: bool = False

   # Interaction and InteractionRequest each gain:
   questions: list[QuestionGroup] = Field(default_factory=list)

   # Answer gains:
   selections: dict[str, list[str]] | None = None   # group id -> chosen labels
   ```

   This is agent-agnostic ("the agent asks one or more grouped multiple-choice questions") — nothing Claude-specific. All fields are optional-with-default → `interaction.requested` stays **payload v1**; old stored events (no `questions`) validate through `Interaction.model_validate` in `MediationService._apply` (service.py:221) and the registry-restart rebuild with `questions=[]`, and the dashboard falls back to the flat-options rendering for them. No upcast function needed.

2. **The dashboard posts generic `selections`, never `updated_input`.** ADR-0019 already rejected browser-built `updatedInput`; that holds. The adapter alone maps `selections` → the `AskUserQuestion` `updatedInput` contract. `AnswerRequest` (app.py:145-151) gains `selections`; `answer_interaction` (app.py:501-504) passes it into `Answer`. (`updated_input` stays on the API for the permission-edit path — unchanged.)

3. **Adapter classifies every well-formed AskUserQuestion as a question.** Rework `packages/prodeo-adapter-claude-code/src/prodeo_adapter_claude_code/format.py`:
   - `interaction_content` (line 36) returns a 5th element `questions: list[QuestionGroup]` (change return type to a small named tuple/dataclass to keep call sites readable — callers: adapter.py:192, hook.py:169). Any `AskUserQuestion` whose `questions` is a non-empty list (1–4 entries) of well-formed dicts (each with non-empty labelled options) becomes kind `question`; **`multiSelect` and multi-question no longer fall back to permission** (delete the fallback at line 48-51's `_single_question` gate; keep the permission fallback for genuinely malformed input).
   - `QuestionGroup.id` = the exact question text (that is the key Claude Code's `answers` map uses, ADR-0019 §2); on duplicate question texts, suffix `" #2"`, `" #3"` for the id only (and un-suffix when mapping back by index).
   - `title` = single question → its text; multiple → `"{first question} (+N more)"`. `body` keeps the speakable "N. label — description" listing for *all* groups (the voice readout). `options` (flat labels) stays populated **only** for the single-question single-select shape — preserving one-click/voice ordinal answering exactly as today.
   - New `questions_updated_input(input_data, selections) -> dict | None`: for each question, take `selections[id]`, match every chosen string against that question's labels via the existing `_match_label` (exact → casefold → ordinal, format.py:102-116); require ≥1 match per question; multiSelect questions map to the answers value by joining matched labels — **verify the join format against a real multiSelect transcript fixture before hardcoding `", "`** (the `answers` contract is Claude-Code-owned; format.py is its single follower per ADR-0019 §Consequences). Any unmatched question → return `None` (never fabricate).
   - `question_updated_input` (single-text path, line 62) stays for voice/typed answers to single-select single questions.

4. **Both delivery paths:**
   - SDK bridge: `launcher.py` `can_use_tool` (lines 82-98) — before the existing text branch add: non-deny answer with `selections` for `QUESTION_TOOL` → `questions_updated_input(...)` → `PermissionResultAllow(updated_input=...)` or `PermissionResultDeny(message="selections did not match the offered options")`.
   - Hook: `hook.py` — the external payload (lines 170-179) gains `"questions": [g.model_dump() for g in content.questions]` (`ExternalInteractionRequest` in app.py:154-166 gains the field); `decision_output` (lines 83-125) gains the selections branch mirroring the launcher, before the bare-text branch at line 113.
   - `adapter.py` `_on_sdk_interaction` (lines 188-203) and `observations.py` `InteractionObservation` (lines 75-91) pass `questions` through; `manager._open_interaction` (manager.py:524-535) copies it into `InteractionRequest`.

5. **Dashboard `InteractionCard`:** when `interaction.questions?.length > 0`, render a new `QuestionForm` instead of the option-button row (InteractionCard.tsx:77-108): one `<fieldset>` per group — radios for single-select, checkboxes for `multi_select` — plus a single **Submit** button, disabled until every group has ≥1 selection, posting `{ selections }`. Keep the free-text "type an answer…" row only for the single-single shape (that is the only shape the adapters' text path can map). Interactions with empty `questions` keep today's rendering (old events, other adapters, permissions). The `<pre>` body stays hidden when `questions` render (it duplicates them); show it for the legacy path.

6. **ADR-0019's "SDK-launched sessions never surface AskUserQuestion" limitation (§4) is untouched** — this workstream fixes the *presentation and answering*, primarily benefiting the hook path (interactive sessions), and is correct on the SDK path if/when the SDK surfaces the tool.

### Files to modify/create

- `src/prodeo/mediation/model.py`, `src/prodeo/adapters/observations.py`, `src/prodeo/adapters/manager.py`, `src/prodeo/api/app.py` (AnswerRequest, ExternalInteractionRequest).
- `packages/prodeo-adapter-claude-code/src/prodeo_adapter_claude_code/{format,launcher,hook,adapter}.py`.
- `dashboard/src/views/InteractionCard.tsx` (+ optional new `dashboard/src/views/QuestionForm.tsx`), `dashboard/src/api/types.ts` (export QuestionGroup), regen artifacts.
- Mjolnir (small): `packages/prodeo-mjolnir/src/prodeo_mjolnir/handlers.py` `_respond` (line 251) — if the matched interaction has >1 group or any `multi_select`, return the new `needs_dashboard` template instead of posting free text that cannot map (pack key added in D's parity pass; if C lands first, add the key to both packs here).

### Tests

- `packages/prodeo-adapter-claude-code/tests/test_format.py`: **delete/replace `test_multi_question_and_multiselect_fall_back_to_permission` (lines 52-59)** with: multi-question → question kind with N groups; multiSelect flag round-trips; flat `options` only for single-single; `questions_updated_input` happy path, ordinal/case matching per item, partial-match → None, duplicate-question-text ids, multiSelect join asserted against a fixture.
- `packages/prodeo-adapter-claude-code/tests/test_control.py`: `can_use_tool` selections → allow with merged `answers`; bad selections → deny.
- `packages/prodeo-adapter-claude-code/tests/test_hook.py`: external payload carries questions; selections answer → allow output; unmatched → passthrough.
- `src/prodeo/mediation/tests/test_service.py`: selections survive `interaction.answered` payload and rebuild; an `interaction.requested` payload *without* `questions` (crafted old-format dict) rebuilds cleanly — the explicit compat test.
- `src/prodeo/api/tests/test_rest.py`: answer with selections round-trips.
- Conformance suite: unchanged — observation schema validity now includes `questions` via Pydantic automatically; no ADAPTER_API_VERSION bump (Protocol unchanged; shared models changed additively).

### Docs

- `docs/architecture/adapter-specification.md`: structured questions in the observation contract; `Answer.selections` semantics; the rule that only adapters map selections to native input.
- `docs/architecture/event-model.md`: note the additive `questions`/`selections` fields on interaction payloads (still v1).
- `docs/architecture/mediation.md` and `docs/architecture/dashboard.md`: question UI behavior.
- **ADR-0022: "Structured multi-part agent questions"** — supersedes ADR-0019's §1 "v1 limitation" paragraph (add an "Amended by ADR-0022" line to ADR-0019's header); records the multiSelect join-format risk and the refuse-to-fabricate rule.

---

## Workstream D: Conversational Mjolnir

### Design decisions

1. **Follow-up listening = a bounded no-wake window, owned by the pipeline.** Add to `VoicePipeline` a `_followup_until: float` and `_invite_reply()` setting it to `max(monotonic(), self._mute_until) + settings.followup_window_s`. In `_listen` (pipeline.py:132-164), when `endpointer is None`: keep the **mute check first** (unchanged, lines 140-143 — the echo guard must win), then if `monotonic() < self._followup_until`: clear the window, `_drain_source()`, keep the current exchange's `correlation_id` (or mint one if empty), **skip the ack**, and open an endpointer directly — no wake-word gate. Wake-word scoring continues to work as before outside the window.
   - **Echo-cooldown subtlety (the key risk):** `_speak` ends with `_suppress_echo()` → `mute_until = now + echo_cooldown_s` (pipeline.py:245-250). Because `_invite_reply` anchors the window *after* `mute_until` and the mute branch runs first and drains, TTS tail can neither self-trigger nor leak into the reply clip. Do not open the endpointer inside the mute window under any refactor; add a pipeline test pinning this ordering.
   - **Silence handling:** thread a `followup: bool` through `_handle_utterance`; when true and `heard` is false or the transcript is empty, say nothing (skip the `not_heard` template) and return to wake-word mode — a declined invitation is not an error.
   - Callers of `_invite_reply()`: (a) `_speak_notifications` (pipeline.py:261-269) after speaking an `interaction.requested` notification — and only that type; (b) `_handle_utterance` after speaking any handler reply that requests it (next point).

2. **Handlers return a `Reply`, and own dialog state.** In `handlers.py`, introduce `@dataclass(frozen=True) class Reply: text: str; expect_reply: bool = False`; `CommandHandlers.handle` returns `Reply` (pipeline.py:208-210 adapts: speak `reply.text`, then `_invite_reply()` if `reply.expect_reply`). Add a dialog slot on `CommandHandlers`:

   ```python
   @dataclass
   class _Dialog:
       kind: Literal["confirm_launch", "slot_project", "slot_prompt", "choose", "question_context"]
       created: float
       data: dict          # spec-in-progress / candidate ids / interaction id
   ```

   with `dialog_pending` (property, honoring `dialog_ttl_s` expiry via an injected clock) and `async def resume(text) -> Reply`. The pipeline checks `handlers.dialog_pending` in `_handle_utterance` **before** intent routing and calls `resume(text)` instead of `router.route`. Cancel phrases (reuse the cancel pattern from intents.py:215) always clear the dialog. This keeps mic-mode ownership in the pipeline and conversation semantics in the handlers — matching the existing seam.

3. **Voice launch, confirm-first (hard rule: a launch happens only on an explicit yes to a read-back).**
   - `intents.py`: `LaunchIntent(project: str = "", prompt: str = "")` + grammar patterns before the approve/deny block, e.g. `^(?:start|launch|spin up|kick off)\s+(?:a\s+|an\s+|new\s+)?(?:session|agent|run)(?:\s+(?:on|in|for)\s+(?P<target>.+?))?(?:\s+(?:to|and have it|and)\s+(?P<text>.+))?$` and a "have an agent (?P<text>.+) (?:on|in) (?P<target>.+)$" variant → `LaunchIntent(project=_clean_target(target), prompt=text)`.
   - `llm_router.py`: add `"launch"` as a two-slot intent — extend the JSON contract to `{"intent", "target", "text"}` and `_SYSTEM_PROMPT` accordingly; gate through the existing allowlist intersection (llm_router.py:87). Add `"launch"` to the `llm_intents` default in config.py:77-86 (safe: the classifier only *names* the intent; confirm-first guards execution — the same argument ADR-0013 used for approve/deny).
   - `handlers._launch(intent)` slot filling: resolve `project` against the distinct `session.project` values in the cache via `_match_norm` (handlers.py:325-327). Zero matches → `Reply(compose("unknown_project", query=...), expect_reply=True)` with `slot_project` dialog (voice launches are **restricted to projects already seen in session history** — dictating a Windows path by voice is not viable; the template says to start it once from the dashboard first). Multiple → `which_one` listing with ordinals, `choose` dialog. Missing prompt → `ask_prompt`, `slot_prompt` dialog. All slots filled → `confirm_launch` dialog + `Reply(compose("launch_confirm", project=<speakable tail>, prompt=<prompt>), expect_reply=True)`.
   - `resume` for `confirm_launch`: affirmative (`^(?:yes|yeah|yep|do it|go ahead|proceed|confirm|please do)\b`) → `client.launch(...)` → `launch_started` (name it via `speakable_name`-style tail); negative/cancel → `launch_cancelled`; anything else → clear the dialog and route the utterance as a fresh intent (never launch on a non-yes).
   - **Adapter choice:** new `MJOLNIR_LAUNCH_ADAPTER` setting (default `""`); when empty, `client.list_adapters()` (A's endpoint) and use the single adapter with `capabilities.launch`; several → `choose` dialog "which agent?". Mjolnir stays agent-agnostic — no `claude-code` literal anywhere in the package.
   - `client.py`: add `async def launch(self, *, adapter: str, project: str, prompt: str) -> Session` (POST `/api/sessions`) and `async def list_adapters(self) -> list[AdapterInfo]` (GET `/api/adapters`, validated via the `AdapterInfo` model imported from `prodeo.adapters.interface`). Launch failure → `ServerRequestError` → contained by `handle`'s `MjolnirError` catch (handlers.py:106-112) into the `launch_failed`/`error` template.

4. **Clarify instead of dead-ending.** `_resolve` (handlers.py:232-249), `_respond` (251-268), `_stop` (270-281): the `len(matches) > 1` branches stop returning the terminal `ambiguous` template; instead stash `choose` dialog `{action: ("answer", decision)| ("respond", text) | ("stop",), candidate_ids: [...]}` and return `Reply(compose("which_one", items=<ordinal list>), expect_reply=True)`. `resume` for `choose` matches ordinal words (reuse `_ORDINAL_TO_INT`) or names against the stashed candidates and executes the stored action; a still-ambiguous reply re-asks once, then cancels. The `"approve it"-with-several-pending` path (line 242's `self._pending()`) also becomes `expect_reply=True` so the positional follow-up needs no re-wake — `_last_pending` already anchors positional resolution against what was read out.
   - **Ordinal-collision subtlety:** while a `choose` dialog is pending, `resume` runs before the router, so "two" binds to the dialog's candidate list, never to `_last_pending` — pin with a test.

5. **Announced interactions become answerable in the follow-up window.** `_speak_notifications` calls a new `handlers.note_announced_interaction(interaction_id)` (sets a `question_context` dialog with the notification's TTL) before `_invite_reply()`. Routing inside the window is **normal-first**: approve/deny/respond intents work as today. Only when routing yields `UnknownIntent` does `_unmatched` (handlers.py:138-150) consult the `question_context` *before* the AnswerEngine: if the announced interaction is question-kind and still pending, match the utterance against its flat `options` (exact/casefold/ordinal — client-side generic label matching; it posts `text=<label>` via the existing answer API, **never** any adapter-native structure) → `responded`; multi-group/multiSelect interactions → `needs_dashboard`; no match → fall through to AnswerEngine as before (preserving ADR-0018 ordering).

6. **Config additions** (`config.py`): `followup_window_s: float = 8.0`, `dialog_ttl_s: float = 90.0`, `launch_adapter: str = ""`, plus the `llm_intents` default gaining `"launch"`. No new event types: follow-up exchanges reuse `voice.command_received`/`voice.transcription_completed` without a preceding `voice.wake_word_detected` — document that an exchange's correlation chain may begin at `command_received`.

7. **Pack keys** (both `NEUTRAL` and `STEWARD` in packs.py — the parity test `test_mjolnir_composer.py:14-15` enforces identical key sets): `launch_confirm`, `launch_started`, `launch_cancelled`, `launch_failed`, `ask_prompt`, `unknown_project`, `which_one`, `needs_dashboard`. Update the `help` template to mention starting a session by voice.

### Tests

- `test_mjolnir_pipeline.py`: window opens after `expect_reply` replies and after interaction notifications (not after completed/failed ones); an utterance inside the window is processed without a wake word and without ack; window expiry restores wake-only; **mute-before-followup ordering** (no capture during echo cooldown); silence in the window speaks nothing.
- `test_mjolnir_handlers.py`: full launch flow (slots, confirm yes/no/other, unknown project, ambiguous project, adapter resolution incl. multi-adapter and config override); choose-dialog disambiguation for approve/deny/stop incl. ordinal collision; dialog TTL expiry; question-context answering (label, ordinal, no-match falls to QA, multiSelect → needs_dashboard); confirm-first invariant: no `client.launch` call on any path lacking an explicit yes.
- `test_mjolnir_intents.py` / `test_mjolnir_llm_router.py`: LaunchIntent grammar; LLM launch slot parsing; `launch` absent from allowlist → UnknownIntent.
- `test_mjolnir_composer.py`: parity passes with the new keys (automatic).
- `tests/integration/test_voice_flow.py`: extend with a launch-by-voice happy path against the composed server if the harness supports it; otherwise handler-level with a fake `ServerClient`.

### Docs

- `docs/architecture/voice-pipeline.md`: new "Follow-up listening and dialogs" section (three mic modes: wake-gated, follow-up window, capture; confirm-first rule; dialog TTL; echo-guard ordering) — also reconcile with the echo-cooldown risk note in `docs/plans-voice-fixes.md`.
- **ADR-0023: "Conversational voice: follow-up listening, clarification dialogs, and confirm-first launches"** — extends ADR-0010/0012/0013/0018; records: the LLM router still only names intents (launch execution is confirm-gated), the talker-vs-actor boundary of ADR-0018 is preserved (`AnswerEngine` still never acts), and the project-must-be-known restriction.

---

## Cross-cutting risks and subtleties (consolidated)

1. **Echo cooldown vs follow-up listening (D):** the mute branch must stay first in `_listen`, and `_invite_reply` must anchor after `mute_until`; pin with a test. Also increase nothing about `echo_cooldown_s` — the window starts after it.
2. **Answer shape for multiSelect (C):** `selections: dict[str, list[str]]` is the generic core shape; only `format.py` builds `updatedInput`. The multiSelect `answers` value format is Claude-Code-owned and unverified — capture a real multiSelect transcript fixture before finalizing the join.
3. **Old `interaction.requested` payloads (C):** every new field defaults; `MediationService._apply` and the dashboard must tolerate `questions` absent — covered by an explicit old-payload rebuild test.
4. **`session.updated` fold discipline (B):** descriptive fields only; never touch `state` in `_apply`, or a rebuild could resurrect stale states. Publish only on actual diffs or discovery (10s tick × N sessions) floods the log.
5. **OpenAPI regen ordering (A/B/C):** server models → `export_openapi.py` → `npm run generate` → dashboard code; committed together per workstream; CI diffs.
6. **mypy --strict fallout:** `refresh_model`/`refresh_permission_mode` going async and `CommandHandlers.handle` returning `Reply` change signatures — the compiler finds every call site; fix them all in the same change (notably `manager.py` and `pipeline.py`).
7. **Core purity check:** nothing in `src/prodeo` gains a model name, a question-tool name, or an `answers`-shape assumption; `server.py` needs no new wiring beyond what `create_app` already receives (`manager` is already injected for the new endpoint).

### Critical Files for Implementation

- F:\SourceCode\CommandCenter\src\prodeo\adapters\interface.py (ModelInfo/AdapterInfo/metadata — Workstream A hub)
- F:\SourceCode\CommandCenter\src\prodeo\sessions\registry.py (session.updated publish + fold — Workstream B hub)
- F:\SourceCode\CommandCenter\packages\prodeo-adapter-claude-code\src\prodeo_adapter_claude_code\format.py (question classification + selections mapping — Workstream C hub)
- F:\SourceCode\CommandCenter\packages\prodeo-mjolnir\src\prodeo_mjolnir\handlers.py (Reply/dialog state/launch flow — Workstream D hub)
- F:\SourceCode\CommandCenter\src\prodeo\api\app.py (GET /api/adapters, AnswerRequest.selections, error mapping — every OpenAPI regen flows from here)