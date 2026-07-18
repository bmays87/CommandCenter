# ADR-0011: Interactive-session permissions via a presence-gated hook

- **Status**: Accepted
- **Date**: 2026-07-17

## Context
Permission prompts from *interactive* Claude Code sessions never reached the
inbox — interactions were only created for sessions Command Center launches
itself (ADR-0008), the deliberate Phase 2 deferral. The required behavior:
answering at the terminal must work immediately when the user is at their
station; when away, the same prompt must be enumerable and answerable from the
dashboard, voice, or phone.

Claude Code's terminal prompt has no external API, but its `PermissionRequest`
hook fires exactly when a prompt would show, and ADR-0007 already anticipated
"a blocked hook HTTP request" as a delivery path. Live verification on Claude
Code 2.1.212 confirmed the contract — stdin carries `session_id`/`tool_name`/
`tool_input`, exit 0 with no output falls through to the normal prompt, and a
`decision` of allow/deny (with optional `updatedInput`/`message`) resolves the
permission — with one surprise: **2.1.212 draws the terminal dialog while the
hook is still running**; the first surface to answer wins, and a terminal
answer kills the hook process. Older releases were documented to run the hook
*before* drawing the prompt.

## Decision
Two pieces, meeting at a new API seam:

1. **External interaction API.** `AdapterManager.open_external_interaction()`
   opens a normal mediated interaction with *no capability gate* —
   `adapter.respond()` is never called because the external requester itself
   carries the answer back. `POST /api/interactions/external` long-polls until
   the interaction leaves `pending` and returns `{interaction_id, status,
   answer}`; a requester disconnect (its human answered locally, or the hook
   died) withdraws the pending card. Timed-out permission interactions
   deliberately leave the session `waiting_on_user`: the human is now being
   prompted at the terminal, and transcript activity resumes the session
   naturally.

2. **A presence-gated hook** (`prodeo-claude-hook`, in the claude-code adapter
   package). Recent local input (default < 90 s, `GetLastInputInfo` on
   Windows; "unknown" elsewhere) → instant passthrough, the terminal prompt
   wins. Otherwise the hook long-polls the external API and, while blocked,
   watches for input resuming — the return-to-station case aborts the request
   (withdrawing the card) and falls through to the terminal. The posture is
   strictly **fail-open**: server down, timeout, malformed anything → exit 0
   with no output, i.e. the normal prompt. The hook never exits non-zero
   (exit 2 means deny).

Timeout layering keeps the server authoritative: interaction timeout 570 s
(`PRODEO_HOOK_TIMEOUT_S`) < hook HTTP read timeout (570+15 s) < Claude Code's
600 s hook cap.

SDK-launched sessions set `PRODEO_MANAGED=1` in the CLI environment; the hook
passes through when it sees the marker, so a launched session's permission is
mediated once (through `can_use_tool`, ADR-0008), never twice.

On ≥ 2.1.212 the concurrent dialog makes the gating *conservative rather than
load-bearing*: even while the hook mediates, the terminal prompt is visible
and answerable, and a terminal answer kills the hook — whose dropped HTTP
request withdraws the card. On older versions (the Windows host PATH still
reports 2.0.45) the prompt is blocked while the hook runs, and the presence
gate is what keeps at-station terminals instant. The design is correct under
both contracts.

## Consequences
Interactive sessions now park as `waiting_on_user` with a real inbox card, so
the existing dashboard and Mjölnir intents (`PendingIntent` / `ApproveIntent` /
`DenyIntent`) work unchanged. The permission is answerable from whichever
surface the user is at, and fail-open means the hook can never strand an
agent. Costs: presence is machine-wide (typing in any app counts as
"at station", so a prompt can show on a terminal the user isn't looking at —
mitigable by routing `interaction.requested` to a notifier), and the hook
contract is Claude-Code-version-sensitive — the README pins the verified
minimum and Step-0-style verification should be repeated on upgrades.

## Alternatives Considered
**PreToolUse hook** — fires on *every* tool call, pre-empting allowlists and
adding latency everywhere; PermissionRequest fires only when a human would be
asked. **Adapter-hosted listener** (adapter polls hooks' files) — inverts the
delivery direction ADR-0007 already designed for and adds a second IPC
mechanism. **Alert-only** (notify but keep answering in the terminal) — fails
the away case entirely, which is the whole point.
