# Goals and Non-Goals

## Goals

1. **Supervise many agents at once.** Dozens of concurrent sessions must be a
   first-class scenario, not a stress case.
2. **Vendor neutrality.** Adding support for a new agent must never require changing
   core code — only writing an adapter.
3. **Headless core.** Every capability is available through the API and event stream;
   no capability exists only in a UI.
4. **Local first.** Full functionality offline on a single Linux machine with local
   models. Cloud services are optional plugins.
5. **Client plurality.** Web dashboard, voice, mobile, REST, webhooks, and automation
   are all peers consuming the same API.
6. **Durable history.** Every event is recorded and queryable; the system is the
   audit log for agent activity.
7. **Extensibility.** STT, TTS, wake word, storage, notification channels, schedulers,
   and memory systems are all replaceable behind interfaces.
8. **Operational simplicity.** A single `pip install` (or `uv tool install`) plus one
   process must be a supported deployment. Docker is offered, never required.

## Non-Goals

1. **Not an AI assistant.** Command Center never generates code, answers questions
   with an LLM, or competes with the agents it supervises. (LLM-powered features like
   daily summaries are optional plugins, not core.)
2. **Not an agent framework.** We do not provide tools, prompts, or reasoning loops
   for building agents. LangChain, CrewAI, etc. are out of scope.
3. **Not a terminal replacement.** Users still interact with agents directly when
   they want to; Command Center supervises, it does not wrap or proxy every keystroke.
4. **Not a CI/CD system.** Scheduling exists to launch agent sessions, not to replace
   Jenkins or GitHub Actions.
5. **Not multi-tenant SaaS (initially).** v1 targets a single user on machines they
   control. Multi-user auth models are deferred, but the API design must not preclude
   them.
6. **Not Kubernetes-native (initially).** Multi-machine orchestration is on the
   roadmap; the first releases are deliberately single-node with clean seams.
