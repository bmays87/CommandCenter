# Claude Code sandbox

This devcontainer is the only place in this project where Claude Code runs
with `bypassPermissions` (no tool-call prompts, including for destructive
commands). It's set on container creation, in the container's own
`~/.claude/settings.json` — not in the repo, and not on the host.

Why: bypass mode is only safe when Claude can't reach anything outside a
disposable sandbox. This container mounts nothing but this repo (VS Code's
default Dev Containers behavior), so a bad command or a prompt-injected file
can't touch the rest of the machine.

Usage: open this repo in VS Code, run "Dev Containers: Reopen in Container",
then run `claude` inside the integrated terminal. On the host, outside this
container, Claude Code still prompts as before — that's intentional.
