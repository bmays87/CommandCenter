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
| `stt` | `SpeechToText` | — (`prodeo-stt-fasterwhisper` reference; `-parakeet` for accuracy) |
| `tts` | `TextToSpeech` | — (`prodeo-tts-piper` reference) |
| `wakeword` | `WakeWordDetector` | — (`prodeo-wakeword-openwakeword` reference) |
| `eventstore` | `EventStore` | SQLite (see ADR-0003; contract suite is the gate) |
| `statestore` | `StateStore` | SQLite |

Phase 3 status: the formal Plugin Host ships, loading `adapter`, `notifier`,
and `summarizer` kinds via manifests. The built-in notification channels
(`log`, `ntfy`, `desktop`) remain config-selected; third-party channels load
as plugins alongside them. The cron **scheduler** shipped as a core service
(`prodeo.scheduler`), not a plugin kind — no second implementation is on the
horizon, and speculative seams are against the house rules; the table row was
removed until substitution is real. `eventstore`/`statestore` kinds remain
planned.

Phase 4 status: the voice kinds (`wakeword`/`stt`/`tts`) are real. They share
the entry-point group and manifest contract, but their **host is the voice
client process** (`prodeo-mjolnir`), where their Protocols also live
(`prodeo_mjolnir.engines`) — the server's Plugin Host recognizes and skips
them, so co-installing engines next to the server is harmless (ADR-0010).
Unlike the server host, the engine loader fails fast: a voice client without
its ears or voice has nothing to contain into.

## Extensions: two classes over one host

"Plugin" is the mechanism; **extension** is what a user installs. There are two
classes (ADR-0014):

| Class | Runs | Examples | Managed by |
|---|---|---|---|
| `plugin` | in-process, via an entry point | adapters, notifiers, summarizers, voice engines | a Plugin Host |
| `app` | its own process, an API client | Mjölnir | (later) an installer + supervisor |

An `app` is deliberately **not** a new `PluginKind`. Kinds describe what a
plugin is *to its host*, and a separate process has no host — Mjölnir talks to
the core over the same HTTP/WS API as the dashboard. Forcing it into
`factory(config)` would break the property that makes it correct.

`prodeo.extensions` is a presentation and configuration layer only: it reads the
inventory `PluginHost.load()` recorded and never loads anything itself. The
inventory includes entry points this process did not host — voice engines appear
as `hosted_by_client`, failures as `failed` with their error — because the
manager's job is to show what is installed, not only what worked here.

Config has two layers: environment variables are the base, and what the manager
saves to `<PRODEO_DATA_DIR>/extensions.json` overlays them per key. Validation
runs against the merged result, so editing one field of a plugin whose required
config comes from the environment works. The host loads once at boot, so saved
changes apply on the next restart — the API says `restart_required` rather than
pretending otherwise.

Installation is still `uv pip install` plus a restart; the manager browses and
configures, it does not yet install.

## Security Posture

Plugins are ordinary Python running in-process: installing one is executing code.
v1 is honest about this — the security boundary is "only install plugins you trust,"
identical to pip itself. A curated plugin index with signing is a roadmap item; a
subprocess/WASM sandbox is explicitly out of scope until real demand exists
(see ADR-0005). What we do enforce now: plugins receive a scoped context object, not
the service container, so casual misuse of internals is at least inconvenient.

The extensions manager does not change this posture: it browses and configures,
and nothing in it runs an installer. The one write it does allow —
`PUT /api/extensions/{name}/config` — is refused when `PRODEO_API_TOKEN` is
unset, because saved values become plugin constructor arguments and an open
server should not offer that. When installation does land, signing and publisher
identity land with it, not after.
