# Repository Layout

Monorepo with a `src` layout and colocated first-party plugins. Independent packages
keep dependency boundaries honest (the core must not import adapter code), while a
uv workspace keeps development friction low.

```
command-center/
├── README.md
├── LICENSE
├── pyproject.toml               # uv workspace root
├── docs/
│   ├── vision.md
│   ├── goals-and-non-goals.md
│   ├── architecture/            # this documentation set
│   ├── development/
│   ├── adr/                     # Architecture Decision Records
│   ├── contributing.md
│   └── roadmap.md
├── src/
│   └── prodeo/                  # core package: `prodeo`
│       ├── events/              # event schemas + envelope (the contract)
│       ├── bus/                 # EventBus interface + in-process impl
│       ├── sessions/            # Session Registry + state machine
│       ├── adapters/            # AdapterManager, AdapterContext, testing kit
│       ├── mediation/           # interactions (permissions/questions)
│       ├── persistence/         # EventStore/StateStore interfaces + SQLite impl
│       ├── plugins/             # Plugin Host (manifests, entry-point loading)
│       ├── notify/              # Notifier service + channel interface
│       ├── scheduler/           # cron engine + Scheduler service
│       ├── summary/             # daily digest service + Summarizer interface
│       ├── api/                 # FastAPI app: REST + WebSocket
│       ├── config.py            # Pydantic Settings
│       └── server.py            # composition root (DI wiring lives here only)
├── packages/                    # first-party plugins, separately installable
│   ├── prodeo-adapter-claude-code/
│   ├── prodeo-adapter-aider/
│   ├── prodeo-adapter-codex/
│   ├── prodeo-summarizer-ollama/
│   └── prodeo-storage-mongodb/  # optional backend (deferred)
├── dashboard/                   # React + TypeScript client
├── tests/                       # cross-package integration tests
│   ├── integration/
│   └── fixtures/                # incl. recorded agent transcripts
├── docker/
│   ├── Dockerfile
│   └── compose.yaml
├── examples/
│   └── adapter-skeleton/        # copyable adapter starting point (workspace member)
└── scripts/                     # dev-env bootstrap, release, codegen
```

Conventions:

- Unit tests live next to the code they test (`src/prodeo/bus/tests/`); only
  cross-cutting integration tests live in the top-level `tests/`.
- `server.py` is the **only** place concrete implementations are wired together.
  Everything else depends on interfaces.
- The dashboard build artifact is packaged into the wheel by the release script.
