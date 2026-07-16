# Packaging a Plugin

Everything replaceable in Command Center is a plugin (see
`docs/architecture/plugin-system.md` for the philosophy and ADR-0005 for the
mechanism). This is the how-to: what a plugin package looks like, what the
Plugin Host expects from it, and how to keep it honest.

## The shape of a plugin

A plugin is an ordinary Python package that exposes **one entry point** in the
`prodeo.plugins` group, resolving to a `PluginManifest`:

```toml
# pyproject.toml
[project]
name = "prodeo-adapter-myagent"
dependencies = ["prodeo"]

[project.entry-points."prodeo.plugins"]
myagent = "prodeo_adapter_myagent:manifest"
```

```python
# src/prodeo_adapter_myagent/__init__.py
from prodeo.plugins import PluginManifest

def manifest() -> PluginManifest:
    return PluginManifest(
        name="myagent",
        kind="adapter",              # "adapter" | "notifier" | "summarizer"
        version="0.1.0",
        factory=create_adapter,      # see signatures below
        # config_model=MyConfig,     # optional Pydantic schema, see below
    )
```

Installing the package (`uv pip install prodeo-adapter-myagent`) is all it
takes; the host discovers it at the next server start. Load failures — import
errors, version mismatches, bad config — become `system.plugin_failed` events
and are skipped; a broken plugin never prevents the server from booting.

## Kinds and factory signatures

| kind | must produce | factory signature | config source (env) |
|---|---|---|---|
| `adapter` | `AgentAdapter` | `factory()` — zero-arg | `PRODEO_ADAPTERS['<name>']`, delivered via `AdapterContext.config` at `start()` |
| `notifier` | `NotificationChannel` | `factory(config)` | `PRODEO_NOTIFY_CHANNELS['<name>']` |
| `summarizer` | `Summarizer` | `factory(config)` | `PRODEO_PLUGINS['<name>']` |

Adapters are the exception on config: their factories take no arguments
because per-adapter config flows through the `AdapterContext`, like it always
has. For the other kinds, `config` is the user's config dict — or, when the
manifest declares a `config_model`, the **validated model instance**.

## Declaring a config schema

```python
from pydantic import BaseModel

class MyConfig(BaseModel):
    base_url: str = "http://localhost:9000"
    timeout_s: float = 30.0

def create(config: MyConfig) -> MyChannel:
    return MyChannel(config)
```

With `config_model=MyConfig` in the manifest, the host validates the user's
config **before** your factory runs: misconfiguration is a clear startup
error naming your plugin, not a `KeyError` at 3am.

## Versioning

- `PluginManifest.plugin_api_version` is the manifest/host contract
  (currently 1). Leave it defaulted; the host refuses mismatches.
- Adapters additionally declare `adapter_api_version` in their
  `AdapterMetadata` (currently 2 — see the adapter specification). The host
  refuses stale adapters at load time rather than crashing mid-watch.

## What the host guarantees (and expects)

- Your exceptions are contained: a crash in the factory or in adapter watch
  tasks becomes an event, never a dead server. Don't rely on that as flow
  control — fail fast in the factory if you can't operate.
- You get scoped context objects, never the service container. Adapters talk
  to the core exclusively through `ctx.report(...)` (typed observations);
  channels just implement `send`; summarizers just implement `summarize`.
- In-process execution: **installing a plugin is running code** (ADR-0005).
  There is no sandbox; say so in your README if you ship one.

## Testing

- **Adapters must pass the conformance suite** — inherit
  `prodeo.adapters.testing.AdapterConformanceSuite` in your tests and provide
  an `adapter` fixture. It checks lifecycle cleanliness, capability honesty
  (declaring `launch=True` and raising is a failure), descriptor validity,
  and observation production. CI for this repo runs it for every in-tree
  adapter.
- Channels and summarizers: test against their Protocols
  (`prodeo.notify.interface.NotificationChannel`,
  `prodeo.summary.interface.Summarizer`) with fake transports — see
  `packages/prodeo-summarizer-ollama/tests/` for the pattern.

## Reference implementations

| | package | shows |
|---|---|---|
| adapter (full control) | `packages/prodeo-adapter-claude-code` | transcript watching + SDK control, versioned parser, offsets |
| adapter (starting point) | `examples/adapter-skeleton` | the smallest honest adapter; copy this |
| summarizer | `packages/prodeo-summarizer-ollama` | config schema, HTTP plugin, containment-friendly design |

Naming convention: `prodeo-adapter-*`, `prodeo-notifier-*`,
`prodeo-summarizer-*` — the dashboard and docs assume it.
