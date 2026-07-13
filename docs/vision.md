# Vision

## The Problem

AI coding agents have crossed the threshold where one developer can productively run
several of them in parallel. What has not kept pace is the tooling to *supervise* them.
Today, running five concurrent agents means five terminal windows, five sets of
permission prompts you might miss, five streams of output nobody is watching, and no
unified record of what happened.

Every agent vendor ships its own UI, its own logs, its own conventions. None of them
interoperate. The person running the agents has become the bottleneck — not because
the agents are slow, but because attention doesn't scale.

## The Idea

Command Center is an **operating system for AI agents**: a single headless platform
that discovers, monitors, orchestrates, and mediates communication with any number of
agents from any vendor, through any client the user prefers — a web dashboard, a voice
interface, a phone, a script, or another automated system.

An operating system does not care which programs run on it. Command Center does not
care which agents it supervises. The agent-specific knowledge lives entirely in
**adapters**; the core provides the universal services every supervised agent needs:

- **Discovery** — find running and historical agent sessions on this machine.
- **Monitoring** — a live, unified view of every agent's state and activity.
- **Mediation** — surface permission requests and questions to whichever client the
  user is currently paying attention to, and route the answer back.
- **Orchestration** — start, stop, schedule, and chain agent work.
- **Memory** — a durable, queryable event history of everything that happened.
- **Notification** — proactively tell the user what needs them, where they are.

## What Success Looks Like

A developer starts their morning by asking, out loud, "what happened overnight?" and
hears a summary of the three agents that ran while they slept — one finished, one is
blocked on a permission request, one failed with a test regression. They approve the
permission from their phone on the train. At their desk, the dashboard shows all
active sessions across their laptop and their homelab box. When an agent needs input,
they get a notification in the channel they actually watch. Nothing about this
workflow depends on which vendor's agents they run.

## Why Open Source

An orchestration layer only becomes a standard if it is neutral. A platform owned by
one agent vendor will never be trusted to supervise a competitor's agents. Neutrality,
plus an adapter interface anyone can implement, is the entire strategic position.
