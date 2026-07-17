# ADR-0010: Voice engines are plugins hosted by the Mjölnir client process

- **Status**: Accepted
- **Date**: 2026-07-17

## Context

Phase 4 ships the Mjölnir voice client (`prodeo-mjolnir`) with replaceable
wake word / STT / TTS engines. The plugin table always planned `stt`, `tts`,
and `wakeword` kinds, but two questions were open:

1. Where do the engine interfaces and their host live — in the core, or in
   the voice client?
2. How does attention state ("the user is talking to the satellite") reach
   the core's notification routing without coupling the two?

Constraints: voice is a client, not a subsystem (voice-pipeline.md); the
core must never depend on engine stacks (the Parakeet rule — NeMo is
multi-GB and CUDA-bound); services never import each other.

## Decision

1. **One entry-point group, two hosts.** Voice engines are ordinary Prodeo
   plugins — `prodeo.plugins` entry points with the same `PluginManifest`
   contract and `PLUGIN_API_VERSION`. The `PluginKind` literal gains
   `stt`/`tts`/`wakeword`, but the **server's PluginHost skips those kinds**
   (debug log, no `system.plugin_failed`); the engine loader in
   `prodeo_mjolnir.plugins` is their host. Engine Protocols
   (`WakeWordDetector`, `SpeechToText`, `TextToSpeech`) live in
   `prodeo_mjolnir.engines`, so engine packages depend on `prodeo-mjolnir`,
   not on core internals. Unlike the server host, the engine loader fails
   fast — a voice client without ears or voice has nothing to contain into.
2. **Voice events are ingested, not bussed.** The client reports
   `voice.*` events through `POST /api/voice/events` — the only externally
   writable namespace in the log (guarded server-side). Envelope: `source =
   voice:<client_id>`, `node` = the satellite's machine, one
   `correlation_id` per exchange.
3. **Presence is an ephemeral core service, not events.** Clients heartbeat
   `PUT /api/presence/{client_id}` (`attentive`, TTL); the tracker prunes
   lazily and is injected into the Notifier as a narrow `AttentionSource`
   Protocol (services still don't import each other). Channels listed in
   `PRODEO_NOTIFY_AWAY_ONLY_CHANNELS` are suppressed while anyone is
   attentive, producing `notification.suppressed`. Heartbeats never touch
   the event log: they are liveness, not facts, and would drown it.
4. **The persona rephraser reuses the `summarizer` kind.** The optional LLM
   persona layer for the overnight briefing is any installed
   summarizer-kind plugin (`MJOLNIR_PERSONA_REPHRASER=ollama`) — same
   `(instructions, content) -> str` contract, same local-model path, no new
   plugin kind. Confirmations ("Approved.") never pass through it.

## Consequences

- Installing engines next to the server is harmless; installing the client
  on a Pi pulls `prodeo` (for the manifest contract and event/session
  models) but never an engine stack it didn't ask for.
- `prodeo-stt-parakeet` exists as a thin wrapper but stays **out of the
  workspace dev group** — CI never downloads NeMo; its tests skip when the
  package isn't installed and stub NeMo when it is.
- A future standalone-manifest split (freeing satellites from the `prodeo`
  dependency entirely) remains possible: the loader only needs the manifest
  shape, not the host.
- Piper's current upstream (`piper1-gpl`) is GPL-3.0; it stays isolated in
  the optional `prodeo-tts-piper` package and never becomes a dependency of
  core or client.
