# docker/

`Dockerfile` builds a server-only image. There is **no `compose.yaml`** and none
is planned; earlier docs promised one, which was a documentation bug.

Docker is a convenience, never a requirement (goals-and-non-goals.md) — and for
this project it is a thin one. **Containerizing the core fights its own premise.**

Command Center supervises agent sessions by observing **host-local** state:

- the claude-code adapter watches `~/.claude/projects`, codex watches
  `~/.codex/sessions`, aider watches project directories — none of which exist
  inside a container unless you bind-mount the host's home directory;
- launching or controlling a session needs the agent's own binary and the
  user's credentials on that machine;
- the `desktop` notification channel shells out to `notify-send` on the host
  session bus;
- Mjölnir needs real audio devices (and, for GPU speech-to-text, the NVIDIA
  container toolkit).

Mount enough of the host to fix all of that and the container has stopped
isolating anything — you have added a layer without buying separation. Running
`uv run prodeo-server` directly on the machine you want supervised is the
supported path, and the one the maintainer uses.

The image is still useful for the narrow case of a **headless hub that only
receives events from elsewhere** rather than discovering local sessions. That
case belongs to the Many Machines phase and is not exercised today, so treat the
Dockerfile as unverified for anything beyond "it builds and boots".
