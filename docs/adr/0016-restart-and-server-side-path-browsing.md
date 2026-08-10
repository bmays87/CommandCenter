# ADR-0016: Restarting from the dashboard, and browsing paths server-side

- **Status**: Accepted
- **Date**: 2026-08-10
- **Extends**: ADR-0015 (installation and apps), ADR-0014 (extensions manager)

## Context

ADR-0015 shipped installation and made Phase 5's exit criterion — adding the
voice client without opening a terminal — nearly true. Two gaps kept it from
being true in practice, both found by installing Mjölnir through the UI.

**The install looked like it failed.** The extensions manager joined the
catalog against `GET /api/extensions`, which is the *plugin* inventory. ADR-0015
§3 deliberately made apps a second extension class with their own entry-point
group and their own endpoint, so an app-class entry was never in that set: it
stayed under "Available" after a successful install and re-offered the Install
button, with nothing on screen changing. A working install was indistinguishable
from a broken one.

**And the manager could only ask.** Both the Plugin Host and the app supervisor
discover entry points once, at start, so every install ended at a notice reading
"restart the server for this to take effect" — sending the user to the terminal
that the phase exists to avoid. The same applied to enable, disable, and config
saves.

Separately, the models directory — the one setting that decides where gigabytes
of weights land — was a bare text box. Typing an absolute path from memory,
correctly, on the first try, is not a thing users do.

## Decision

### 1. `POST /api/system/restart`, and the re-exec happens outside the loop

`Server` carries `shutdown` (an `asyncio.Event`) and `restart_wanted`. Signals
and the endpoint set the same event, so there is exactly one shutdown path.
`run()` returns whether a restart was wanted; `main()` re-execs **after**
`asyncio.run()` returns.

That ordering is the whole decision. Re-execing inside the request handler, or
anywhere in the loop, would replace the process image with supervised children
still running, the event store open, and the API port still bound. By the time
`asyncio.run()` returns, `Server.stop()` has already killed the children first
and closed the store last (ADR-0015 §4), so the replacement process starts
against a consistent database and a free port.

The endpoint schedules the shutdown as a FastAPI background task so the response
is flushed first. A client that gets a dropped connection cannot tell a
successful restart from a crash.

### 2. `boot_id` on `/api/health`

Health gained a ULID generated once per process. Without it a client cannot tell
"the server came back" from "it has not gone down yet" — for the first moments
after the POST, the old process is still answering, and a reachability check
calls that success immediately. The dashboard records `boot_id` before the POST
and polls until it changes.

Health stays unauthenticated, which is what lets the poll work while the token
holder is mid-restart.

### 3. Path picking is a server concern, not a browser one

The value being chosen is a directory on the machine the **server** runs on. The
browser cannot supply one: `webkitdirectory` yields file names without a path,
and `showDirectoryPicker` yields an opaque handle and only in Chromium. Neither
knows anything about the server's filesystem when it is not localhost.

So `GET /api/system/browse?path=` lists sub-directories, with the roots
(drive letters via `os.listdrives()` on Windows, `/` elsewhere, plus home on
both) when `path` is empty. Doing it server-side also makes the same component
work on Windows and POSIX without knowing which it is — the platform difference
lives in one function.

An unreadable child is skipped, not fatal. `C:\System Volume Information` sits
in the root of the drive this project tells people to put bulk data on; failing
the listing there would make the picker useless exactly where it is needed.

### 4. Both endpoints are token-gated writes

`_require_write()` guards both, so each is refused when `PRODEO_API_TOKEN` is
unset. Restarting a process is at least as consequential as the config writes
that already require a token. Browsing is nominally a read, but enumerating the
filesystem is a materially larger disclosure than the rest of this read-mostly
API, and the API is open by default when no token is set — the same reasoning
ADR-0015 §1 applied to installs.

## Consequences

- The Phase 5 exit criterion is reachable end to end: install, restart, and set
  up the voice client without a terminal.
- **`os.execv` on Windows is spawn-then-exit, not a true image replacement.**
  The launching shell sees the original process exit and prints a prompt while
  the new server keeps running on the same console. Logs still land there, but
  the prompt appears mid-stream and Ctrl-C from that shell no longer reaches the
  server. Accepted over a detached `Popen`, which loses console output entirely.
- A restart is not offered when `restart_fn` is unwired (schema export, focused
  tests); the endpoint answers 503 rather than pretending.
- The browse endpoint gives any token holder a directory listing of the whole
  machine. That is a real widening of what the token buys, bounded by it being
  read-only and directory-only.
- The installed/available join now spans both extension classes. Anything added
  as a third class has to be added there too — the join is the one place that
  has to know the full set.

## Alternatives Considered

- **A "restart" that only stops the server**, leaving a supervisor or the user
  to start it. Rejected: on a desktop session there is no supervisor, and a
  button that shuts your server off is not the button anyone wanted.
- **Re-exec from inside the request handler.** Rejected: see decision 1.
- **Reachability polling instead of `boot_id`.** Rejected: it reports success
  before the old process has even begun shutting down.
- **`showDirectoryPicker` in the browser.** Rejected: Chromium-only, and it
  cannot yield a server-side path even there.
- **Reusing `restart_required` in the response and leaving the button out.**
  Rejected: that is the status quo this ADR exists to fix.
