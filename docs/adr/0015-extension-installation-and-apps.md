# ADR-0015: Installing extensions, and apps as supervised processes

- **Status**: Accepted
- **Date**: 2026-08-10
- **Extends**: ADR-0014 (which shipped browse + configure and deferred the rest)

## Context

ADR-0014 established the extensions manager as a presentation layer with
deliberately **no new code-execution surface**, and parked installation,
supervision, and trust. Phase 5 needed all three: its exit criterion is a user
adding the voice client and approving a permission by voice *without opening a
terminal*, which is impossible if the server can neither install nor launch
anything.

ADR-0005 accepted in-process plugins with no isolation and named the trigger for
revisiting it — *"if a plugin marketplace materializes"*. It has.

## Decision

### 1. Installation exists, and is restricted to catalog names

`POST /api/extensions/{name}/install` resolves **a name from the sanctioned
catalog** to a package spec. The caller never supplies a spec, so the endpoint
cannot be used to fetch arbitrary code; an unknown name is a 404. Installs run
`uv pip install --target <data_dir>/extensions/lib` (falling back to
`python -m pip`, since the satellite runbook uses a plain venv) via
`create_subprocess_exec` with an argv list — never a shell.

`--target` keeps user-installed extensions outside `.venv`, where `uv sync`
would delete them. The directory is **appended** to `sys.path`, never prepended,
so an extension can extend the server but not shadow it.

**Every state-changing endpoint is refused when `PRODEO_API_TOKEN` is unset.**
Config values become plugin constructor arguments and installs execute code;
an open-by-default server must not offer either. Reads stay open.

### 2. First-party packages install from a locally built wheel index

The in-repo extensions are unpublished *and* depend on each other, so a registry
install can never resolve them — `prodeo-adapter-aider` needs `prodeo`,
`prodeo-stt-parakeet` needs `prodeo-mjolnir`. On the first install from a
workspace checkout the server runs `uv build --all-packages` into
`<data_dir>/extensions/local-index` and passes `--find-links`. Cached after the
first build; a no-op for an ordinary deployment, where published names resolve
from the index as normal.

### 3. Apps are a second extension class, not a plugin kind

An `AppManifest` in the `prodeo.apps` entry-point group describes a
separate-process client. This is deliberately **not** a new `PluginKind`: a kind
is a contract with a host, and a process that talks to the server over HTTP has
no host to be loaded by. Forcing Mjölnir through `factory(config)` would break
the property that makes it correct.

> **Amended by ADR-0016.** Being a second class means an app is absent from
> `GET /api/extensions` entirely, which the dashboard's installed-vs-available
> join originally missed: an app-class catalog entry never left "Available" and
> re-offered Install after a successful install. Any code deciding "is this
> installed?" must consider **both** classes.

`env_prefix` is what keeps core generic. An app that reads settings through
pydantic-settings declares its prefix, and the supervisor renders the whole
child environment from `(prefix, saved config)` — strings verbatim, everything
else JSON, exactly what pydantic-settings parses back. Core knows no field name
of any app. `server_url_field`/`api_token_field` let the app *name* the fields
the server should fill in, so a locally supervised client is never asked where
its own server is.

### 4. Supervisor semantics

Modelled on `AdapterManager._supervised_watch` — per-app task, backoff
1s → ×2 → 60s, health reset after a run exceeding 60s, `CancelledError`
re-raised first — with two deliberate divergences:

- **A clean child exit is not terminal.** For an adapter watch a clean return
  means the work finished; for a supervised process it means the thing the user
  wants running has stopped, so it comes back. The loop ends only on an explicit
  stop.
- **A crash loop gives up.** Five consecutive starts that never stay up long
  enough to look healthy, and the supervisor stops and reports. This was not
  theoretical: a Mjölnir with no configured voice wrote a
  `system.app_started`/`system.app_exited` pair into the durable log every
  backoff interval, indefinitely. A startup crash is nearly always
  misconfiguration, and that does not fix itself.

`start()` never raises. `Server.start()` has no exception handling and sits
outside the `try/finally` in `run()`, so a service that throws there kills the
process and leaks every task started before it.

Started **last** (after `api.start()`, since children need the resolved port and
`api_port` may be 0) and stopped **first**. On stop the supervisor clears
presence itself: Windows `terminate()` is a hard kill, so the client never sends
its goodbye and would otherwise linger for its full TTL.

`system.app_started` / `system.app_exited` are `system.*`, not `voice.*` — that
namespace is reserved for what a client reports about *itself* (ADR-0010),
whereas these are the server's observations of a process.

### 5. Assets are catalog data, verified by what they produce

An asset declares a command (only `{python}` and `{models_dir}` substituted) and
the files it must produce. **An asset is present only when every produced file
exists**, and a download that exits 0 without producing them is a failure. This
is not pedantry: Piper loads `<voice>.onnx` *and* its `.onnx.json` sibling, and
swallows synthesis errors — so a half-download starts the client cleanly and
leaves it silently mute forever. The downloaded path is written into the app's
config at a declared dotted pointer, so core wires `engines.piper.voice_path`
without knowing what Piper is.

### 6. The paid tier is a placeholder, and is labelled as one

> **Amended 2026-08-10.** Mjölnir is now `free`, not `paid` — the voice client
> is part of the open-source project and gating it discouraged contributors.
> The paid-tier *mechanism* described below is retained (nothing in the bundled
> catalog uses it today); it stays as the shape a future paid extension would
> take. The historical text stands as the record of that decision.

Catalog entries carry `tier` (`bundled`/`free`/`paid`); Mjölnir was `paid`, and
installing a paid extension without a licence key returns **402 Payment
Required**. The check is presence of a non-empty key and *nothing more*. It is a
real, testable behaviour confined to one method, so a verified entitlement
replaces it without touching anything else — but it is **not a security
control** and must never be described as one. Anyone who can edit
`extensions.json` bypasses it.

This is the kind of speculative seam the house rules forbid ("speculative seams
are against the house rules" — the scheduler plugin kind was removed for exactly
this). It is admitted because it is a stated product requirement, not an
anticipated one.

### 7. Host probes read the platform registry before globbing paths

The CUDA probe globbed `%ProgramFiles%\NVIDIA\CUDNN` and spent a session
insisting a fully-installed cuDNN 9.25 was missing, because it had been
installed to `F:\Program Files` — which is exactly what this project instructs
users to do with bulk data. The probe ignored the rule the product enforces.

Discovery now reads `InstallLocation` from the Windows uninstall keys first,
which is drive-agnostic by construction, and searches all three bin layouts in
the wild (`bin`, `bin/<ver>`, `bin/<ver>/x64`). A side benefit that turned out
to matter: it is immune to a stale process environment, so a server started from
a shell that predates the install still finds it.

## Consequences

- Installation is no longer theoretical, and neither is the risk. The blast
  radius is bounded by the catalog and the token, not by sandboxing — there is
  still none, per ADR-0005.
- `--target` installs cannot see the venv, so each extension brings its own copy
  of shared dependencies. Disk cost only; `sys.path` order means the venv's copy
  always wins.
- **Restart-to-activate remains true** for plugin install/enable/disable: the
  Plugin Host loads once at boot. Only apps start and stop live, and the API
  says `restart_required` rather than pretending otherwise. (ADR-0016 keeps this
  true but makes the restart itself a button rather than an instruction.)
- A supervised child inherits the server's session. Voice therefore needs the
  server run from a desktop session; as a Windows service it has no microphone.
  A real limit of server-launched voice, not a bug.
- Signing and publisher identity are still absent. When a third-party index
  materializes they land with it, not after — `plugin-system.md` already
  commits to that.

## Alternatives considered

- **Arbitrary package specs on the install endpoint.** Rejected: it turns a
  bounded action into a general-purpose remote-code-execution API guarded only
  by a bearer token kept in browser local storage.
- **A `client` plugin kind for Mjölnir.** Rejected: see decision 3.
- **Per-extension virtualenvs.** Better isolation, but in-process plugins must
  be importable by the server, so it only helps app-class extensions — and adds
  an environment to manage per extension for a benefit nothing yet needs.
