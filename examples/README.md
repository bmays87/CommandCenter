# examples/

- `adapter-skeleton/` — starting point for new agent adapters: a complete,
  conformance-tested observe-only adapter (a uv workspace member, so CI keeps
  it compiling). Copy it, rename it, replace the file-watching with your
  agent's session source. See `docs/development/plugin-packaging.md`.

For a notifier-plugin example, the packaging guide contains the full pattern;
`packages/prodeo-summarizer-ollama` shows a config-validated HTTP plugin of a
different kind and translates directly.
