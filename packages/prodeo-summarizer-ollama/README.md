# prodeo-summarizer-ollama

Summarizer plugin for [Prodeo Command Center](../../README.md): turns the
daily activity digest into a few sentences of prose using a local
[Ollama](https://ollama.com) model. No cloud, no API keys.

## Install & configure

```bash
uv pip install prodeo-summarizer-ollama   # inside this repo: already a workspace member
ollama pull llama3.2                      # or any model you prefer
```

```bash
export PRODEO_PLUGINS='{"ollama": {"base_url": "http://localhost:11434", "model": "llama3.2"}}'
# deliver the summary somewhere visible (it always goes to the event log too):
export PRODEO_NOTIFY_RULES='{"summary.generated": ["ntfy"], ...}'
```

Config schema (validated at startup by the Plugin Host):

| key | default | |
|---|---|---|
| `base_url` | `http://localhost:11434` | Ollama server |
| `model` | `llama3.2` | any pulled model |
| `timeout_s` | `120` | per-call HTTP timeout |
| `options` | `{}` | Ollama options passthrough (`temperature`, `num_ctx`, ...) |

The Summary Service works without this plugin — the digest is still built and
published as `summary.generated`; this plugin only adds the prose. Failures
(Ollama down, model missing) are contained by the core and reported in the
event's `summarizer_error` field.
