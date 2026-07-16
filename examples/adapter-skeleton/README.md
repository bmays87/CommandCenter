# adapter-skeleton

The starting point for a new agent adapter. The "agent" it observes is any
process appending to `*.log` files in a directory — replace that with your
agent's real session source and rename things.

```bash
cp -r examples/adapter-skeleton ../prodeo-adapter-myagent
# rename: pyproject name, entry point, src/prodeo_adapter_skeleton/
uv pip install -e ../prodeo-adapter-myagent   # installing = plugged in
```

Try it as-is:

```bash
export PRODEO_ADAPTERS='{"skeleton": {"logs_dir": "/tmp/agent-logs"}}'
mkdir -p /tmp/agent-logs && echo "thinking..." >> /tmp/agent-logs/demo.log
uv run prodeo-server   # "demo" appears in the dashboard fleet view
```

Read in this order:

1. [adapter.py](src/prodeo_adapter_skeleton/adapter.py) — the four things every
   adapter implements, annotated.
2. [__init__.py](src/prodeo_adapter_skeleton/__init__.py) — the `PluginManifest`
   entry point.
3. [tests/test_conformance.py](tests/test_conformance.py) — inherit the
   conformance suite; it is the floor every adapter must pass.

Then: `docs/development/plugin-packaging.md` for the packaging rules and
`docs/architecture/adapter-specification.md` for the full contract (control
capabilities, interactions, observation types).
