# Interactive-session permission mediation (presence-gated hook)

> **Execution notes (added at export, 2026-07-17):** this plan was approved on
> the Windows workstation; it is self-contained (all file paths/line numbers
> are repo-relative facts, verified against the current tree). If executing in
> a Linux dev container:
>
> 1. **Step 0 must run against the container's own `claude` binary** — and
>    note: the Windows host's CLI on PATH reports **2.0.45**, while sessions
>    here run under agent version 2.1.211 (IDE-bundled). Verify the
>    `PermissionRequest` hook exists on whatever version will actually run the
>    hooks before building anything.
> 2. The `presence.py` seam intentionally returns `None` (= "unknown", always
>    mediate) on non-Windows — correct for the container, but it means the
>    **at-station instant-passthrough and return-to-station E2E steps (2 and 4
>    in Verification) can only be exercised on the Windows host**, where
>    `GetLastInputInfo` exists. Container E2E covers the away path, timeout,
>    and fail-open cases.
> 3. `--install` writes to `~/.claude/settings.json` *inside the container* —
>    host sessions are unaffected until the same install is run on the host.
> 4. Delete this file once the work is merged; the durable design record is
>    ADR-0011 (Step 6).

## Context

Permission prompts from *interactive* Claude Code sessions never reach the
dashboard/voice inbox — interactions are only created for sessions Command
Center launches itself via the Agent SDK. This was the deliberate Phase 2
deviation ("blocking-hook mediation of interactive sessions deferred",
docs/roadmap.md:25-27); the user has now un-deferred it.

**Required behavior (user's words):** when an agent is waiting on a
permission, answering at the terminal must work *immediately* if they're at
their station; if away, asking mjolnir "any agents waiting?" must enumerate
pending prompts and let them approve/deny by voice (or dashboard/phone).

**Key constraint:** Claude Code's built-in terminal prompt has no external
API, and its `PermissionRequest` hook runs *before* the prompt is drawn — so
at any instant only one surface can answer. **Resolution: a presence-gated
hook** — whichever surface the user is at wins:

- Recent local keyboard/mouse input → hook passes through instantly → terminal
  prompt appears with no perceptible delay.
- No recent input (away) → hook long-polls Command Center → interaction opens
  in mediation → dashboard card + mjolnir enumeration/approval (voice intents
  `PendingIntent`/`ApproveIntent`/`DenyIntent` already exist —
  packages/prodeo-mjolnir/src/prodeo_mjolnir/intents.py:24-39 — zero voice
  changes needed).
- User returns to the terminal mid-mediation → hook detects input resuming,
  aborts its HTTP request (server withdraws the card), passes through →
  terminal prompt appears within ~1s of touching the keyboard.

Facts verified during exploration:
- Dashboard inbox is fully generic (InboxView/InteractionCard render any
  adapter's interaction); no dashboard changes, but OpenAPI types must be
  regenerated (CI diffs them).
- No external-interaction creation path exists today; ADR-0007 already
  anticipates "a blocked hook HTTP request" as a delivery path and its
  cancel-on-restart semantics fit.
- `PermissionRequest` hook event exists (fires only when a prompt would show;
  exit 0 + no output = fall through to the normal prompt; exit 2 = deny — the
  hook must never exit non-zero). Contract verified against current docs by
  the Plan agent; **Step 0 re-verifies live on Claude Code 2.1.211.**
- Load-bearing signatures confirmed: `SessionRegistry.resolve(adapter,
  native_id)` (src/prodeo/sessions/registry.py:58), `MediationService.get` /
  `cancel_native` (src/prodeo/mediation/service.py:80,182), deliver-callback
  contract incl. timeout auto-deny with status TIMED_OUT
  (service.py:281-304), `_open_interaction` (src/prodeo/adapters/manager.py:422-478).

User decisions: auto-install command approved (`--install` merges into
`~/.claude/settings.json` with timestamped backup, idempotent). Away-window
timeout: default **570s** (~9.5 min, under Claude Code's 600s hook cap),
configurable — with presence gating the timeout only governs the away case.

## Step 0 — Live hook-contract verification (gate)

Register a throwaway `PermissionRequest` hook that tees stdin to a file and
exits 0; trigger a permission prompt in a scratch interactive session.
Confirm: fires only at prompt time; stdin has `session_id`, `tool_name`,
`tool_input`, `hook_event_name`; exit-0-no-output shows the normal prompt;
echoing `{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
"decision": {"behavior": "allow"}}}` runs the tool unprompted. Adjust the
deny `message` mapping to whatever the live contract accepts. If the event
doesn't exist in 2.1.211, stop and report (do NOT fall back to PreToolUse —
it fires on every tool call and would pre-empt allowlists).

## Step 1 — Core: external-interaction seam (`src/prodeo/adapters/manager.py`)

Refactor `_open_interaction`'s shared parts into `_open_mediated(session,
request, deliver)` (mediation.open + transition to WAITING_ON_USER, suppressing
IllegalTransitionError) and `_resume(session_id, reason)`. Existing capability
gate + adapter-respond deliver closure stay in `_open_interaction`.

New methods (external caller is the delivery path; **no capability gate**,
`adapter.respond()` never called):

- `open_external_interaction(*, adapter, session_native_id, native_id, kind,
  title, body, options, timeout_s) -> tuple[Interaction, asyncio.Future[Interaction]]`
  - Resolve via `self._resolve(adapter, session_native_id)`; on miss, one
    targeted re-discovery then re-resolve (mirrors the retry at
    manager.py:346-351); still missing → `UnknownSessionError` (→ 404).
  - deliver closure: `resolved.set_result(interaction)` (guard `done()`);
    resume session only when `status is ANSWERED`. On TIMED_OUT deliberately
    stay `waiting_on_user` — the human is now being prompted in the terminal;
    transcript activity resumes the session naturally.
- `withdraw_external_interaction(interaction_id, *, reason)` — if still
  PENDING, `mediation.cancel_native(...)`; no-op otherwise.

No `MediationService` changes; no new event types (reuse `interaction.*`).

## Step 2 — Core: API route (`src/prodeo/api/app.py`)

Models: `ExternalInteractionRequest` {adapter, session_native_id,
kind=permission, title, body="", options=[], native_id="" (server ULID if
empty), timeout_s required `gt=0 le=3600`} and `ExternalInteractionResponse`
{interaction_id, status, answer: Answer | None}.

`POST /api/interactions/external` (auth dependency like siblings): open via
manager, then loop `asyncio.wait({resolved}, timeout=0.5)`; on each tick check
`mediation.get(id).status != PENDING` → return {id, status, answer}
(timed_out/cancelled → `answer=None`; the caller passes through, never denies).
Poll `await request.is_disconnected()` and catch `asyncio.CancelledError` —
both → `withdraw_external_interaction(reason="requester_disconnected")`.

Regenerate generated API types: `uv run python scripts/export_openapi.py`,
then `npm run generate` in `dashboard/` (CI diffs both). No dashboard
component changes.

## Step 3 — Adapter package: shared formatting + managed guard

- New `packages/prodeo-adapter-claude-code/src/prodeo_adapter_claude_code/format.py`:
  `permission_prompt(tool_name, input_data) -> (title, body)` — "Allow
  {tool}?" + `json.dumps(input, indent=2, default=str)[:4000]`. Use it in
  `_on_sdk_interaction` (adapter.py:164-178), drop `_INTERACTION_BODY_CHARS`.
- `launcher.py` `default_client_factory`: set `env["PRODEO_MANAGED"]="1"` on
  `ClaudeAgentOptions` (verify the field exists in the pinned SDK) so hooks
  inside CC-launched sessions pass through — prevents double mediation.

## Step 4 — Adapter package: the hook CLI

New `hook.py` + `presence.py` in the adapter package; console script
`prodeo-claude-hook = "prodeo_adapter_claude_code.hook:main"`; add `httpx` to
its pyproject deps (now imported directly); bump adapter version to 0.4.0.

`presence.py` — the platform seam (per environment.md: platform variance
isolated behind an interface): `seconds_since_input() -> float | None`.
Windows impl via stdlib ctypes `GetLastInputInfo`; other platforms → `None`
("unknown" → mediate). Injectable for tests.

`hook.py` (sync, testable pure functions + `run(stdin, stdout, env, *,
transport=None, since_input=None, clock=None) -> int`):
1. Parse stdin; passthrough (exit 0, no output) if `PRODEO_MANAGED=1`, wrong
   `hook_event_name`, or malformed/missing fields.
2. **Presence gate:** `seconds_since_input() < threshold` (default 90s,
   `PRODEO_PRESENT_THRESHOLD_S`) → passthrough immediately (at-station case).
3. Else POST `/api/interactions/external` (adapter="claude-code",
   session_native_id=stdin `session_id`, title/body via `permission_prompt`,
   native_id=`hook-{ULID}`, timeout_s from `PRODEO_HOOK_TIMEOUT_S` default
   570) — request runs in a worker thread; HTTP read timeout = timeout_s + 15
   so the server always resolves first.
4. **Return-to-station watch:** while the request is pending, poll
   `seconds_since_input()` ~1/s; if input resumes → close the client (server
   withdraws the card via disconnect) → passthrough.
5. Map resolution: answered+allow → decision behavior allow (+`updatedInput`
   from `answer.updated_input`); answered+deny → behavior deny + message
   (`answer.text`); timed_out / cancelled / HTTP error / connect error / any
   exception → passthrough. **Never exit non-zero** (exit 2 means deny).
6. `--print-config` prints the settings.json snippet; `--install [--settings
   PATH]` merges `{"hooks": {"PermissionRequest": [{"hooks": [{"type":
   "command", "command": "\"<sys.executable>\" -m
   prodeo_adapter_claude_code.hook"}]}]}}` into `~/.claude/settings.json`
   with a timestamped backup, idempotent, preserving unrelated keys
   (JSON-escaped Windows paths). Server URL/token come from env
   (`PRODEO_SERVER_URL`, `PRODEO_API_TOKEN`); README documents how to set
   them and never writes the token into settings.json.

Env summary: `PRODEO_SERVER_URL` (default http://127.0.0.1:8600),
`PRODEO_API_TOKEN`, `PRODEO_HOOK_TIMEOUT_S` (570), `PRODEO_PRESENT_THRESHOLD_S`
(90), `PRODEO_MANAGED` (set by launcher only).

## Step 5 — Tests

- `src/prodeo/adapters/tests/test_manager.py`: external open works on an
  observe-only adapter (no capability gate) and parks session; unknown session
  re-discovers then 404s; answer resolves future + resumes session + adapter
  `respond` NOT called; short-timeout resolves future with TIMED_OUT and stays
  `waiting_on_user`; withdraw cancels pending / no-ops resolved; existing
  `_open_interaction` tests stay green post-refactor.
- `src/prodeo/api/tests/test_rest.py`: long-poll answered (task + answer via
  existing `POST /api/interactions/{id}/answer` → status answered + decision);
  timeout → `{status: timed_out, answer: null}`; 404/401/422; client-cancel →
  interaction cancelled.
- New `packages/prodeo-adapter-claude-code/tests/test_hook.py`
  (httpx.MockTransport, fake `since_input`): stdin→request mapping; managed
  guard (zero requests); wrong-event/malformed → passthrough; presence gate
  passthrough (recent input, zero requests); input-resume abort → passthrough
  + card withdrawal request observed; allow/deny/timed_out/cancelled/HTTP
  errors/connect-error mappings; always exit 0; `--print-config` content;
  `--install` merge/backup/idempotency on a tmp settings file; format parity
  with the SDK path via shared `permission_prompt`.
- `test_control.py`: launcher sets `PRODEO_MANAGED=1`.
- New `tests/integration/test_interactive_hook_flow.py` (mirror
  test_observe_flow.py: real server, tmp projects_dir, fixture transcript,
  fast discovery): hook `run()` in a thread with away-presence fake → pending
  interaction appears (title "Allow Bash?", session `waiting_on_user`) →
  answer allow via REST → hook stdout parses to behavior allow, exit 0. Plus
  timeout→empty-stdout and server-down→fast-passthrough cases.

## Step 6 — Docs (same change, per CLAUDE.md)

- New `docs/adr/0011-interactive-mediation-via-permission-hook.md`: external
  interaction API + presence-gated `PermissionRequest` hook; fail-open
  posture; timeout layering (interaction 570s < HTTP 585s < CC hook 600s);
  `PRODEO_MANAGED` guard; references ADR-0007/0008; alternatives rejected
  (PreToolUse spam, adapter-hosted listener, alert-only).
- `docs/architecture/mediation.md`: "External interactions" section (second
  entry path, no capability gate, timed_out-vs-answered on the response,
  disconnect withdrawal, why timed-out permissions stay `waiting_on_user`).
- `docs/architecture/adapter-specification.md`: note the external submission
  seam.
- `docs/roadmap.md`: amend the Phase 2 deviation (shipped via ADR-0011).
- Adapter README: setup (`--install`), env table, presence semantics, token
  handling, minimum CC version.

## Verification

```bash
uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .
uv run python scripts/export_openapi.py && (cd dashboard && npm run generate && npm run build)
```

Manual E2E (server already running on :8600 with token `change-me`):
1. `uv run prodeo-claude-hook --install`; set `PRODEO_SERVER_URL`/`PRODEO_API_TOKEN` in env.
2. At-station: interactive `claude`, trigger a non-allowlisted command while
   typing normally → terminal prompt appears immediately (no mediation).
3. Away: trigger a permission, hands off keyboard/mouse ≥90s → card appears
   in dashboard inbox + fleet view shows `waiting_on_user`; ask mjolnir
   "anything need me?" → it enumerates; say "approve it" → tool runs, no
   terminal prompt.
4. Return-to-station: while a card is pending, touch the keyboard → terminal
   prompt appears ~1s later; card flips to cancelled.
5. Kill the server mid-prompt → instant terminal prompt (fail-open).
6. CC-launched (SDK) session permission → exactly one inbox entry (managed
   guard suppressed the hook).

Optional config note for the user (no code): route `interaction.requested` to
ntfy so their phone pings when away —
`PRODEO_NOTIFY_RULES='{"interaction.requested": ["ntfy","log"]}'`.

## Risks

- Hook contract drift across CC releases — gated by Step 0; README pins
  minimum version.
- `GetLastInputInfo` is machine-wide: typing in another app counts as
  "at station" → terminal prompt shows while you're in another window. The
  ntfy/desktop notification config covers that gap; documented.
- Discovery race for brand-new sessions — targeted re-discovery; residual
  misses fail open to the terminal.
- Disconnect semantics differ between uvicorn and test transports — both the
  `is_disconnected()` poll and `CancelledError` path implemented and tested;
  worst case a zombie card resolves at timeout_s.
