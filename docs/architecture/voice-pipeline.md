# Voice Pipeline (Phase 4)

Voice is a **client**, not a subsystem of the core. It runs as a separate process
(`prodeo-voice`), possibly on different hardware (a Raspberry Pi satellite), and
talks to the server over the same WebSocket + REST API as the dashboard.

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

### Note on Parakeet

Parakeet's NeMo dependency chain is heavy (multi-GB, CUDA-bound) and must never be
pulled in by the core install. STT engines are plugins precisely so
`prodeo-stt-parakeet` can have brutal dependencies while `prodeo-stt-fasterwhisper`
stays lightweight. The voice client works CPU-only out of the box.

## Intent Handling

v1 intents are **deterministic** (pattern/grammar based): "status", "what happened
overnight", "approve the permission for <session>", "stop <session>". An LLM-based
intent router is a plugin upgrade, not a prerequisite — deterministic intents keep
latency low and behavior predictable, and they work fully offline.

## Interaction Flow Example

1. Agent asks a question → adapter reports → `interaction.requested` on the bus.
2. Notifier speaks: "Claude on project X asks: may it run the database migration?"
   (only if voice is the user's active/attentive client per routing rules).
3. User: "yes, approve it" → intent router → `POST /interactions/{id}/answer`.
4. Mediation service resolves the interaction exactly once (a simultaneous dashboard
   click loses gracefully and is told so) → adapter delivers the answer.

## Latency Budget

Wake-to-acknowledgement under 1.5 s; command-to-spoken-response under 3 s for cached
state queries. These budgets are why voice reads from the event-stream-fed local
cache rather than issuing cold queries.
