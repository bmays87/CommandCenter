# Dashboard Architecture

The web dashboard is the reference client: React + TypeScript, talking only to the
public API. If the dashboard needs data the API cannot supply, the fix is an API
change — never a private backdoor.

## Stack

- **Build**: Vite. **UI**: React 18+, TypeScript strict.
- **Server state**: TanStack Query for REST queries; a thin WebSocket layer feeds
  live events into the query cache (targeted invalidation/patching by `session_id`).
- **Client state**: kept minimal (Zustand); server is the source of truth.
- **API types**: generated from the server's OpenAPI schema in CI — drift between
  server and client types fails the build.

## Views (Milestone 1 → 2)

1. **Fleet view** — every session as a card: agent kind, project, state, last
   activity, attention flag. Sessions needing a human float to the top.
2. **Session view** — timeline of that session's events, live output tail, pending
   interactions with answer controls (rendered per adapter capabilities).
3. **Interaction inbox** — all unanswered permissions/questions across sessions.
4. **Event explorer** (phase 2) — filterable raw event history.

## Live Updates

The dashboard subscribes to `session.*`, `interaction.*`, `tool.*` over WebSocket
with a ULID cursor; on reconnect it replays missed events via
`GET /events?after=<ulid>`. Rendering dozens of concurrent sessions is a stated
requirement, so the fleet view virtualizes its list and event handling is batched
per animation frame.

## Distribution

The built dashboard is embedded in the Python package and served by FastAPI at `/`
(single-process deployment). Developing against a running server uses Vite's proxy.
