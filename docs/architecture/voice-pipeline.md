# Voice Pipeline: Mjölnir (Phase 4)

The voice client is named **Mjölnir**. Voice is a **client**, not a subsystem of
the core. It runs as a separate process (`prodeo-mjolnir`), possibly on
different hardware (a Raspberry Pi satellite), and talks to the server over the
same WebSocket + REST API as the dashboard.

*Shipped (phase 4).* `packages/prodeo-mjolnir` plus the engine plugins
(`prodeo-wakeword-openwakeword`, `prodeo-stt-fasterwhisper`,
`prodeo-tts-piper`, and `prodeo-stt-parakeet`). Engines are
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
- **STT**: faster-whisper (default) or NVIDIA Parakeet — both CPU-capable, both
  faster on a GPU; see note below
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

Parakeet used to be the argument for engine isolation: its NeMo chain pulled
PyTorch and ~148 packages, so it had to stay out of any default install. Since
`prodeo-stt-parakeet` 0.2.0 it runs the same weights through ONNX Runtime
(`onnx-asr`) — one package, CPU-capable, and in the workspace dev group like
every other engine.

The isolation rule still holds; it is simply no longer *this* package that
tests it. A future engine with a heavy stack (XTTS-class TTS, say) is exactly
why STT and TTS are separate plugin packages. The voice client works CPU-only
out of the box.

**GPU is optional, and automatic when it is there.** The default engines detect
what they can use and degrade quietly: faster-whisper picks `cuda`/`float16`
only when CTranslate2 sees a device *and* the CUDA 12 runtime is actually
present (else `cpu`/`int8`), Piper enables the CUDA execution provider only
when onnxruntime exposes it, and both fall back to CPU with a warning if a GPU
load fails. Ollama manages its own GPU (ADR-0013).

Which GPU route is available depends on the engine, and they are not
interchangeable:

| Engine | GPU route | Needs CUDA |
|---|---|---|
| faster-whisper | CTranslate2 | **Yes** — its only GPU path |
| parakeet | ONNX Runtime, incl. `DmlExecutionProvider` | **No** — DirectML drives any DX12 GPU |
| piper | upstream exposes only `use_cuda` | CPU otherwise; irrelevant for a 63MB voice |

So the cheap way to use a GPU is Parakeet on DirectML, not CUDA. For 2–5 second
utterances CPU is fast enough that neither is required — the environment view
(`GET /api/system/environment`) reports which routes exist rather than implying
CUDA is mandatory.

## Intent Handling

Intents route through a `Router` seam with two implementations behind it:

1. **Deterministic grammar (default, always first).** Pattern/grammar based:
   "status", "what happened overnight", "approve the permission for <session>",
   "stop <session>", and numbered answering — "you have two: … approve number
   two", "respond to one with <text>". Instant, predictable, fully offline.
2. **Constrained LLM classifier (default; `MJOLNIR_INTENT_ROUTER=llm`).** An
   Ollama model is consulted **only** when the grammar returns `UnknownIntent`,
   so known phrasings never pay LLM latency and the client still runs offline on
   the grammar alone (`patterns` mode disables it entirely). The model is a
   *classifier over a closed intent set, never an executor*: it picks one frozen
   intent (plus a free-text target *hint*), can emit nothing outside the
   allowlist (`MJOLNIR_LLM_INTENTS`, which by default includes the
   `approve`/`deny`/`stop` actions), and fails closed to "didn't understand" if
   Ollama is unreachable, slow, or malformed. Target resolution and the
   single-match ambiguity guard stay in the handlers against live data — the LLM
   never names an id, and confirmations remain deterministic. See
   [ADR-0012](../adr/0012-llm-intent-router.md) and
   [ADR-0013](../adr/0013-ollama-default-brain.md).
3. **Grounded question answering (default; `MJOLNIR_QUESTION_ANSWERING=llm`).**
   When *neither* router produces an intent, the utterance is treated as a
   question rather than dead-ending in "didn't understand". The same LLM
   identity answers it from a frame of reference (who Mjölnir is, what agents,
   sessions, and permission requests are, which spoken commands exist) plus a
   live state snapshot rendered from the local cache — so "what's an agent?",
   "why is it asking me that?", and "what are you for?" get real answers with
   context already attached. The engine is a talker, never an actor: it can
   produce nothing but speech, actions still flow only through the intent enum,
   and any failure falls back to the deterministic "didn't understand" template.
   See [ADR-0018](../adr/0018-grounded-question-answering.md).

**Echo suppression.** The pipeline is half-duplex — it does not listen while
speaking — but a real mic keeps buffering during playback, so TTS can bleed
speaker→mic and self-trigger the wake word. After every spoken response Mjölnir
drains those buffered frames, resets the wake detector, and mutes wake scoring
for `MJOLNIR_ECHO_COOLDOWN_S` (default 0.4 s) so it cannot hear itself.

## Follow-up listening and dialogs (ADR-0023)

The mic has three modes, owned by the pipeline:

1. **Wake-gated** (default): frames are scored for the wake word only.
2. **Follow-up window**: after Mjölnir speaks something that *invites a
   reply* — a clarifying question, a launch confirmation, or a permission/
   question announcement — the next utterance is captured directly for
   `MJOLNIR_FOLLOWUP_WINDOW_S` (default 8 s): no wake word, no ack, same
   exchange `correlation_id` (so a correlation chain may begin at
   `voice.command_received` with no preceding `voice.wake_word_detected`).
   Silence in the window is a declined invitation, not an error: Mjölnir says
   nothing and returns to wake-gated mode.
3. **Capture**: an endpointer is collecting one utterance.

**Ordering rule (load-bearing):** the echo-cooldown check runs *before* the
follow-up check, and the window is anchored *after* `mute_until` — capture can
never open while TTS tail may still be in the mic buffer, so Mjölnir cannot
answer its own question.

Conversation semantics live in the handlers as a **dialog slot** (TTL
`MJOLNIR_DIALOG_TTL_S`, default 90 s): a pending clarification consumes the
next utterance before intent routing. Dialog kinds:

- **Choose** — an ambiguous target ("approve api" matching two requests) no
  longer dead-ends: Mjölnir reads the candidates with ordinals and the reply
  ("two", "the first one", the name) picks one. While a choose dialog is
  pending, ordinals bind to *its* list, never to the last pending readout; a
  reply contradicting the action ("deny number two" mid-approve) is re-routed
  as a fresh intent instead of mis-executing. Still ambiguous → one re-ask,
  then cancel.
- **Slot-filling + confirm-first launch** — "start a session on X to do Y"
  fills missing slots by asking (project, then task), resolves the adapter
  from `GET /api/adapters` (config override `MJOLNIR_LAUNCH_ADAPTER`; several
  launch-capable adapters → a choose dialog), then reads the launch back.
  **A launch executes only on an explicit spoken yes** — a "no" cancels, and
  anything else routes as a fresh intent, never a launch. Voice launches are
  restricted to projects already seen in session history (dictating a
  filesystem path by voice is not viable; start new projects once from the
  dashboard).
- **Announced-question context** — after announcing a question interaction,
  the follow-up utterance is matched against its options (exact, unique
  containment, ordinal) and posted as the answer. Routing stays normal-first:
  approve/deny/respond intents work as always; only an utterance no intent
  matched is tried against the options, and one matching nothing falls
  through to grounded QA (preserving ADR-0018's order). Multi-part or
  multi-select questions are pointed at the dashboard (`needs_dashboard`) —
  one utterance cannot answer them (ADR-0022).

Cancel phrases ("never mind", "cancel") always clear a dialog.

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
4. **LLM persona layer (default-on).** A rephraser that renders
   *non-time-critical* responses in persona — the morning briefing, daily
   summaries — via the local-model path (`prodeo-summarizer-ollama`, a
   dependency of the client; `MJOLNIR_PERSONA_REPHRASER=ollama` by default,
   empty to disable). It shares Mjölnir's single LLM identity
   (`MJOLNIR_LLM_BASE_URL`/`MJOLNIR_LLM_MODEL`) with the intent router
   ([ADR-0013](../adr/0013-ollama-default-brain.md)). It is never in the loop
   for interaction confirmations ("approved", "stopped"): those stay
   deterministic templates, because a permission answer must be fast,
   predictable, and impossible to garble. Degrades to the deterministic text if
   the model is unreachable or times out.

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
