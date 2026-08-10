# ADR-0014: Extensions are a manager over the plugin host; apps are a second class

- **Status**: Accepted
- **Date**: 2026-08-09

## Context

The Onboarding & Extensibility phase calls for installing and extending Command Center from the dashboard
rather than by hand-assembling `MJOLNIR_*` env vars — an experience closer to
VS Code's extensions view than to `uv pip install` plus a restart. ADR-0005
accepted in-process plugins with no isolation and named the trigger for
revisiting that: *"if a plugin marketplace materializes"*. It has.

Three facts shaped the design:

1. **The plugin host already carries the metadata a manager needs.**
   `PluginManifest` declares `name`, `kind`, `version`, and — critically — an
   optional Pydantic `config_model` whose JSON Schema can generate a settings
   form. What was missing was any way to *see* it: the API layer had no
   knowledge that the plugin system existed.
2. **Config was write-only from the user's side.** `PRODEO_PLUGINS` and friends
   are single JSON env blobs. Changing one plugin's setting means rewriting the
   blob in a shell profile — exactly the ritual this phase exists to kill.
3. **Mjölnir is not an in-process plugin.** It is a separate process that talks
   to the server over HTTP/WS and says so in its own client module: *"it is a
   client, not a subsystem"*. Its engines are plugins; Mjölnir itself is not.

## Decision

1. **The extensions manager is a presentation layer, not a second loader.**
   `prodeo.extensions` reads what `PluginHost` discovered and never loads or
   runs anything itself. The host stays the single place plugins are resolved
   and instantiated. `PluginHost.load()` records an `ExtensionInfo` per entry
   point — so the inventory costs no second import pass — and the service reads
   it through a callable, because the composition root wires the API before the
   host has loaded.

2. **Inventory includes what this process did not host.** Status is `loaded`,
   `failed`, or `hosted_by_client`. Voice engines are installed software the
   user should see and reason about; hiding them because *this* process skipped
   them would make the manager lie about what is on the machine.

3. **Two extension classes, one catalog.** `plugin` is in-process (the existing
   kinds). `app` is an out-of-process client or service — Mjölnir, and any
   future client. An `app` is not a new `PluginKind`: kinds describe what a
   plugin *is to the host*, and an app has no host. Forcing Mjölnir into the
   plugin model would break the property that makes it correct. This milestone
   ships the catalog entry and the class distinction; installing and supervising
   an app comes later.

4. **Config precedence: environment first, saved overlay on top, per key.**
   The environment layer keeps CI, containers, and headless deploys
   reproducible; the manager owns what it writes. Validation runs against the
   **merged** result, never the overlay alone — editing one field of a plugin
   whose required config comes from the environment must not fail. The overlay
   persists to `<PRODEO_DATA_DIR>/extensions.json`, deliberately outside the
   virtualenv, because `uv sync` deletes anything in `.venv` the lock does not
   name. This is the `prodeo.toml` promise in `config.py` landing, in JSON to
   match the existing env encoding.

5. **Restart to activate, stated plainly.** The host loads once at boot and has
   no `unload()`. Config responses carry `restart_required`; the UI says so.
   Faking hot reload would be a lie about when a setting takes effect.

6. **Config writes are refused on an unauthenticated server.** Saved values
   become plugin constructor arguments — a materially larger blast radius than
   the rest of this read-mostly API, which is open by default when
   `PRODEO_API_TOKEN` is unset. Reads stay open; `PUT` returns 403 with the
   remedy in the message.

## Consequences

- Adding presentation metadata (`description`, `publisher`, `homepage`,
  `license`, `categories`) to `PluginManifest` is backward compatible: all
  optional and defaulted, so `PLUGIN_API_VERSION` stays 1 and no existing
  plugin needs republishing.
- Surfacing `license` matters more than it looks: `prodeo-tts-piper` is GPL-3.0
  (ADR-0010), and a user deserves to see that before installing.
- The catalog ships bundled as JSON. Whether the production index is a reviewed
  file in a git repo or PyPI filtered by the `prodeo-<kind>-*` naming
  convention is **still open** — that choice defines what "sanctioned" means and
  should be decided on its own merits, not defaulted into.
- Installation is still `uv pip install` plus a restart. Nothing here executes
  an installer, so this milestone adds **no new code-execution surface** beyond
  the config write above.
- The subprocess-isolation question ADR-0005 deferred is *still* deferred, but
  it is now concrete rather than speculative: it belongs with the app class and
  the process supervisor, alongside signing and publisher identity.
- `docs/deployment/running-the-system.md` currently frames everything in
  `packages/` as either "a process you launch" or "an in-process plugin". When
  app-class extensions become installable that dichotomy needs revising — the
  server becomes a launcher of the first category.

## Alternatives considered

- **A `client` plugin kind for Mjölnir.** Rejected: kinds are contracts with a
  host, and there is no host for a separate process. It would have put a
  process launcher behind an interface designed for `factory(config)`.
- **Config in the event store.** Rejected: `EventStore` is an append-only log
  (ADR-0003) and mutable settings are not events. A separate store keeps both
  honest.
- **A JSON-Schema form library (`@rjsf`).** Rejected for now: every shipped
  `config_model` is flat scalars, and the dashboard has no router, UI kit, form
  library, or state library. A ~150-line renderer with a JSON textarea fallback
  covers them without changing that.
