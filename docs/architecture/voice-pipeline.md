# Voice Pipeline: Mjölnir (Phase 4)

The voice client is named **Mjölnir**. Voice is a **client**, not a subsystem of
the core. It runs as a separate process (`prodeo-mjolnir`), possibly on
different hardware (a Raspberry Pi satellite), and talks to the server over the
same WebSocket + REST API as the dashboard.

*Shipped (phase 4).* `packages/prodeo-mjolnir` plus the engine plugins
(`prodeo-wakeword-openwakeword`, `prodeo-stt-fasterwhisper`,
`prodeo-tts-piper`, and `prodeo-stt-parakeet` for GPU boxes). Engines are
plugins in the shared `prodeo.plugins` group, hosted by the mjolnir process
(ADR-0010). Deployment runbook: `docs/deployment/satellite-pi.md`.

## Pipeline

```
mic ─► WakeWordDetector ─► VAD ─► SpeechToText ─► Intent Router ─► REST commands
                                                        │
speaker ◄── TextToSpeech ◄── Response Composer ◄── event stream (WebSocket)
```

Reference engines (all local, all replaceable behind interfaces):

- **Wake word**: OpenWakeWord
- **STT**: NVIDIA Parakeet (GPU) or faster-whisper (CPU fallback) — see note below
- **TTS**: Piper
- **Summaries** (optional): Ollama via the `summarizer` plugin

### Wake word

The default wake word is **"mjölnir", spoken with its proper Norse
pronunciation** (approximately "MYOL-neer") — the client answers to its name.
That requires a custom-trained OpenWakeWord model shipped with the client;
until it lands, a stock pretrained model serves as the development fallback.
The wake word is user-configurable: a `wake_word` setting selects any other
OpenWakeWord model (stock or custom-trained), so nothing hard-codes the
default.

### Note on Parakeet

Parakeet's NeMo dependency chain is heavy (multi-GB, CUDA-bound) and must never be
pulled in by the core install. STT engines are plugins precisely so
`prodeo-stt-parakeet` can have brutal dependencies while `prodeo-stt-fasterwhisper`
stays lightweight. The voice client works CPU-only out of the box.

## Intent Handling

Intents route through a `Router` seam with two implementations behind it:

1. **Deterministic grammar (default, always first).** Pattern/grammar based:
   "status", "what happened overnight", "approve the permission for <session>",
   "stop <session>", and numbered answering — "you have two: … approve number
   two", "respond to one with <text>". Instant, predictable, fully offline.
2. **Constrained LLM classifier (optional fallback).** With
   `MJOLNIR_INTENT_ROUTER=llm`, an Ollama model is consulted **only** when the
   grammar returns `UnknownIntent`, so known phrasings never pay LLM latency.
   The model is a *classifier over a closed intent set, never an executor*: it
   picks one frozen intent (plus a free-text target *hint*), can emit nothing
   outside an allowlist (`MJOLNIR_LLM_INTENTS`, read-only by default), and fails
   closed to "didn't understand" if Ollama is unreachable, slow, or malformed.
   Target resolution and the ambiguity guard stay in the handlers against live
   data — the LLM never names an id. See
   [ADR-0012](../adr/0012-llm-intent-router.md).

**Echo suppression.** The pipeline is half-duplex — it does not listen while
speaking — but a real mic keeps buffering during playback, so TTS can bleed
speaker→mic and self-trigger the wake word. After every spoken response Mjölnir
drains those buffered frames, resets the wake detector, and mutes wake scoring
for `MJOLNIR_ECHO_COOLDOWN_S` (default 0.4 s) so it cannot hear itself.

## Persona

Mjölnir has a configurable persona, designed in from day one rather than
bolted on. Personality lives in four independently swappable places, ordered
from free to optional:

1. **Address/honorific config.** Every response template carries a persona
   slot; `honorific: "sir"` (or "ma'am", a name, or empty) is interpolated by
   the Response Composer. Pure config.
2. **Persona template packs.** The Response Composer's phrasing is a template
   set, not hard-coded strings: the default pack is neutral ("Session
   terminated."), and packs can restyle it ("As you wish, sir. The session has
   been terminated."). Packs are deterministic text — they keep v1's offline
   guarantee and latency budget untouched.
3. **Voice selection.** The speaking voice is the TTS plugin's config; Piper's
   stock catalogue already covers the calm-British-AI register
   (`en_GB-alan`, `en_GB-northern_english_male`, ...). More expressive engines
   (XTTS-class) arrive as separate `tts` plugin packages with their own heavy
   dependencies — the same isolation rule as Parakeet above.
4. **Optional LLM persona layer** (plugin). A rephraser that renders
   *non-time-critical* responses in persona — the morning briefing, daily
   summaries — via the same local-model path as the `summarizer` plugin. It is
   never in the loop for interaction confirmations ("approved", "stopped"):
   those stay deterministic templates, because a permission answer must be
   fast, predictable, and impossible to garble.

**Boundary:** persona voices must be original, stock, or licensed. Cloning a
real person's voice without their consent (an actor, a colleague) is out of
scope for this project — not a plugin opportunity.

## Interaction Flow Example

1. Agent asks a question → adapter reports → `interaction.requested` on the bus.
2. Notifier speaks: "Claude on project X asks: may it run the database migration?"
   (only if voice is the user's active/attentive client per routing rules).
3. User: "yes, approve it" → intent router → `POST /interactions/{id}/answer`.
4. Mediation service resolves the interaction exactly once (a simultaneous dashboard
   click loses gracefully and is told so) → adapter delivers the answer.

## Attention (how "the client the user is watching" works)

A voice exchange marks the user *attentive* for `MJOLNIR_ATTENTIVE_WINDOW_S`
(default 120 s). Two consumers act on that state:

- **The client itself** speaks server notifications only while attentive
  (`MJOLNIR_SPEAK_NOTIFICATIONS=attentive`, the default; `always`/`never`
  override) — an interaction request is announced out loud to someone who
  was just talking to the satellite, not to an empty kitchen.
- **The server** hears about it through presence heartbeats
  (`PUT /api/presence/{client_id}`, TTL-expired). Channels listed in
  `PRODEO_NOTIFY_AWAY_ONLY_CHANNELS` (e.g. `ntfy` phone push) are suppressed
  while *any* client is attentive, producing `notification.suppressed`
  instead of a redundant buzz in the user's pocket.

Presence is deliberately ephemeral — see the note in event-model.md.

## Latency Budget

Wake-to-acknowledgement under 1.5 s; command-to-spoken-response under 3 s for cached
state queries. These budgets are why voice reads from the event-stream-fed local
cache rather than issuing cold queries.
