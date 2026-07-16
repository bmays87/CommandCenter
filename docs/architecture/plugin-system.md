# Plugin System

Everything replaceable in Command Center is a plugin: agent adapters, notification
channels, STT/TTS/wake-word engines, storage backends, schedulers, and memory
systems. The core ships with sensible defaults; plugins override or extend them.

## Mechanism

- **Discovery**: Python entry points under the group `prodeo.plugins`. Installing a
  plugin package (`uv pip install prodeo-adapter-aider`) is all that is required for
  the Plugin Host (`prodeo.plugins`) to find it.
- **Contract**: each plugin exposes a `PluginManifest` (name, kind, version,
  `plugin_api_version`, config schema as a Pydantic model). The host refuses to load
  plugins built against an incompatible API version — with a clear error, not a crash.
  (Bare zero-arg adapter factories, the Phase 1 form, still load for compatibility.)
- **Configuration**: user config (env vars via Pydantic Settings; `prodeo.toml`
  later) is validated against the plugin's declared schema *before* the plugin is
  instantiated. Misconfiguration is reported at startup, not mid-flight.
- **Isolation**: a plugin exception is contained; the host emits
  `system.plugin_failed` and continues. Adapters additionally run their watch tasks
  under supervision with exponential-backoff restarts.

See `docs/development/plugin-packaging.md` for the author-facing how-to.

## Plugin Kinds and Their Interfaces

| Kind | Interface | Default implementation |
|---|---|---|
| `adapter` | `AgentAdapter` | — (claude-code, aider, codex ship separately) |
| `notifier` | `NotificationChannel` | log channel (ntfy + desktop built in) |
| `summarizer` | `Summarizer` | — (optional; `prodeo-summarizer-ollama` reference) |
| `stt` | `SpeechToText` | — (phase 4; faster-whisper reference) |
| `tts` | `TextToSpeech` | — (phase 4; Piper reference) |
| `wakeword` | `WakeWordDetector` | — (phase 4; OpenWakeWord reference) |
| `eventstore` | `EventStore` | SQLite (see ADR-0003; contract suite is the gate) |
| `statestore` | `StateStore` | SQLite |

Phase 3 status: the formal Plugin Host ships, loading `adapter`, `notifier`,
and `summarizer` kinds via manifests. The built-in notification channels
(`log`, `ntfy`, `desktop`) remain config-selected; third-party channels load
as plugins alongside them. The cron **scheduler** shipped as a core service
(`prodeo.scheduler`), not a plugin kind — no second implementation is on the
horizon, and speculative seams are against the house rules; the table row was
removed until substitution is real. `eventstore`/`statestore`/voice kinds
remain planned.

## Security Posture

Plugins are ordinary Python running in-process: installing one is executing code.
v1 is honest about this — the security boundary is "only install plugins you trust,"
identical to pip itself. A curated plugin index with signing is a roadmap item; a
subprocess/WASM sandbox is explicitly out of scope until real demand exists
(see ADR-0005). What we do enforce now: plugins receive a scoped context object, not
the service container, so casual misuse of internals is at least inconvenient.
