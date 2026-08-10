# ADR-0017: An app with incomplete setup cannot be started

- **Status**: Accepted
- **Date**: 2026-08-10
- **Extends**: ADR-0015 (apps as supervised processes), ADR-0016

## Context

The first real user to install Mjölnir through the dashboard clicked Start
before running the setup wizard. The client requires `engines.piper.voice_path`
and no voice had been downloaded, so it exited with a pydantic validation
error; the supervisor retried five times and gave up. What the user saw was
"exited with code 1" — the supervisor pipes the child's stderr to `DEVNULL`, so
the actual reason never reached them.

Every layer behaved as designed. ADR-0015 §4 even predicted the failure ("a
startup crash is nearly always misconfiguration"). The design fault was
*allowing the start at all*: the setup wizard's own Start button was gated on
asset presence, but the Apps panel card, the raw API, and autostart were not.
Three of the four doors led straight to the crash-loop.

## Decision

### 1. Readiness is derived from catalog data, not app knowledge

An asset already declares which app it configures (`config_app`) and at which
key (`config_pointer`) — that is how the download gets wired into config
(ADR-0015 §5). `AssetProvisioner.unmet_for_app(app)` reads the same
declarations in reverse: for every asset in the catalog claiming this app, the
app is ready only when the pointed-at config value exists **and names a file
that exists**. Three states fall out:

- pointer unset → "Download X in the setup wizard";
- pointer set, file gone → reported as missing from disk. This is the sharp
  case: Piper starts cleanly without its voice file and is silently mute, so a
  deleted model must block a start just as firmly as a never-downloaded one;
- pointer set, file present → satisfied.

Core still knows no app field names — the check walks declared pointers.

### 2. The supervisor refuses, everywhere, with the steps in the message

`start_app` raises `AppNotReadyError` (HTTP 412, detail lists the steps) while
gaps remain. Autostart skips the app and records the same message as its
`last_error` with state `failed`, instead of crash-looping into the durable
log. The readiness probe is injected (`setup_gaps_fn`), keeping the
supervisor's zero-knowledge property; the dashboard's disabled button is a
courtesy, the supervisor's refusal is the guarantee — any client hitting the
API gets the same protection.

### 3. Environment-configured apps are exempt, precisely

The saved-config check would wrongly block an app configured through the
server's environment (the child inherits `os.environ`; ADR-0015 §3). A gap is
therefore dismissed when the environment provides its value: for pointer
`engines.piper.voice_path` under prefix `MJOLNIR_`, the supervisor reads
`MJOLNIR_ENGINES`, parses the JSON, and walks the remaining path — the exact
rendering `config_to_env` uses, in reverse. Unparsable JSON gets the benefit
of the doubt: whoever hand-set an env var on the server process is past the
wizard's audience, and a wrong guess must not make their app unstartable.

### 4. The probe fails open

If the readiness check itself throws (unreadable catalog, broken store), the
app is treated as ready and the crash-loop guard remains the backstop — the
pre-this-ADR behaviour. A broken probe must degrade to the old world, never
to "nothing can be started".

### 5. The API carries the steps to the user

`AppStatus` gains `unmet_setup: list[str]`, filled at query time by the API
layer (the check is async; the supervisor's queries are not). The dashboard
disables Start while it is non-empty and lists the steps on the card, next to
the "Set up…" button that resolves them.

## Consequences

- Clicking Start can no longer produce a bare "exited with code 1" from
  missing setup — the failure mode is now a labelled, disabled button.
- The readiness check runs on every `/api/apps` poll (one catalog read, one
  store read, a few `stat` calls per app every 5s per client). Accepted;
  it matches what the catalog endpoint already does per request.
- A stopped app whose model file is deleted later shows a gap and refuses to
  start — previously it would have started mute. A *running* app is untouched;
  gaps only gate starts.
- The child's stderr is still discarded (`DEVNULL`), so genuinely novel crash
  reasons remain invisible. Capturing the tail of stderr into `last_error` is
  the natural next step and deliberately out of scope here.

## Alternatives Considered

- **Gate only in the dashboard.** Rejected: the API and autostart remain
  doors to the crash-loop, and every future client re-implements the check.
- **Let the app declare a `readiness_command` to probe itself.** Rejected:
  running app code to decide whether to run app code, and a second
  code-execution surface for no gain over the declarative pointer check.
- **Validate the app's full `config_model` before start.** Rejected: the
  supervisor would need the model's required fields satisfied from config
  alone, but apps legitimately read defaults and environment the supervisor
  cannot see; the asset pointer is the part the catalog actually knows.
- **Fail closed when the probe breaks.** Rejected: a corrupt catalog file
  would then disable every app on the machine.
