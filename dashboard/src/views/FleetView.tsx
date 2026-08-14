import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { isActive, type Session } from "../api/types";
import { projectName, timeAgo } from "../format";
import { useLiveEvents } from "../live";
import { DirectoryPicker } from "./DirectoryPicker";
import { ModelPicker } from "./ModelPicker";
import { ModePicker, modeLabel } from "./ModePicker";

function StateBadge({ state }: { state: string }) {
  return <span className={`badge state-${state}`}>{state.replace(/_/g, " ")}</span>;
}

/** Start a new agent run on a chosen project (POST /api/sessions). */
function NewSessionForm({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [adapter, setAdapter] = useState("claude-code");
  const [project, setProject] = useState("");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("default");
  const [browsing, setBrowsing] = useState(false);
  const [error, setError] = useState("");

  // Started adapters with capabilities and model catalogs; only launch-capable
  // ones are offered (an observe-only adapter cannot start a session).
  const adaptersQuery = useQuery({ queryKey: ["adapters"], queryFn: api.adapters });
  const adapters = (adaptersQuery.data?.adapters ?? []).filter((a) => a.capabilities.launch);
  const selected = adapters.find((a) => a.name === adapter);

  const launch = useMutation({
    mutationFn: () =>
      api.launchSession({ adapter, project, prompt, model, permission_mode: mode }),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      onClose();
      window.location.hash = `#/session/${session.id}`;
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  const openEditor = useMutation({
    mutationFn: () => api.openEditor(project),
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  return (
    <section className="ext-detail new-session">
      <h2>New session</h2>
      <div className="form-row">
        <label htmlFor="ns-adapter">Agent</label>
        <select id="ns-adapter" value={adapter} onChange={(e) => setAdapter(e.target.value)}>
          {adapters.length === 0 ? <option value="claude-code">claude-code</option> : null}
          {adapters.map((a) => (
            <option key={a.name} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <div className="form-row">
        <label htmlFor="ns-project">Project directory</label>
        <div className="input-with-button">
          <input
            id="ns-project"
            type="text"
            value={project}
            placeholder="e.g. F:\SourceCode\my-app"
            onChange={(e) => setProject(e.target.value)}
          />
          <button type="button" onClick={() => setBrowsing(true)}>
            Browse…
          </button>
        </div>
      </div>
      <div className="form-row">
        <label htmlFor="ns-model">Model</label>
        <ModelPicker id="ns-model" value={model} onChange={setModel} models={selected?.models ?? []} />
        <p className="field-help">
          Models the agent declares; pick &quot;Custom model id…&quot; for any other id, or
          &quot;Agent default&quot; to let the agent decide.
        </p>
      </div>
      <div className="form-row">
        <label htmlFor="ns-mode">Permission mode</label>
        <ModePicker id="ns-mode" value={mode} onChange={setMode} />
        <p className="field-help">
          How the agent handles permissions. You can change it live once it&apos;s running.
        </p>
      </div>
      <div className="form-row">
        <label htmlFor="ns-prompt">Prompt</label>
        <textarea
          id="ns-prompt"
          rows={4}
          value={prompt}
          placeholder="What should the agent do?"
          onChange={(e) => setPrompt(e.target.value)}
        />
        <p className="field-help">
          Runs headless under the server; watch it and answer its permission prompts from the
          Inbox. Note: a launched agent has no terminal, so avoid interactive question tools —
          those are answerable only from a session you run yourself (e.g. in VS Code).
        </p>
      </div>
      <div className="ext-actions">
        <button
          type="button"
          onClick={() => launch.mutate()}
          disabled={launch.isPending || !project.trim() || !prompt.trim()}
        >
          {launch.isPending ? "Starting…" : "Start session"}
        </button>
        <button
          type="button"
          onClick={() => openEditor.mutate()}
          disabled={openEditor.isPending || !project.trim()}
          title="Open this project in a new VS Code window"
        >
          Open in VS Code
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
      {error ? <div className="notice error">{error}</div> : null}
      {browsing ? (
        <DirectoryPicker
          initialPath={project}
          onCancel={() => setBrowsing(false)}
          onSelect={(path) => {
            setProject(path);
            setBrowsing(false);
          }}
        />
      ) : null}
    </section>
  );
}

function SessionCard({ session, onArchive }: { session: Session; onArchive?: () => void }) {
  const attention = session.state === "waiting_on_user";
  return (
    <div
      className={`card ${isActive(session.state ?? "") ? "card-active" : ""} ${
        attention ? "card-attention" : ""
      }`}
    >
      <a className="card-link" href={`#/session/${session.id}`}>
        <div className="card-top">
          <span className="agent-chip">{session.adapter}</span>
          <StateBadge state={session.state ?? "discovered"} />
        </div>
        <div className="card-title">{session.title || session.native_id}</div>
        <div className="card-meta">
          <span className="project">{projectName(session.project ?? "")}</span>
          {session.model ? <span className="model">{session.model}</span> : null}
          {session.permission_mode ? (
            <span className="model">{modeLabel(session.permission_mode)}</span>
          ) : null}
        </div>
      </a>
      <div className="card-footer">
        <span>{timeAgo(session.last_activity_at)}</span>
        {onArchive ? (
          <button
            type="button"
            className="card-archive"
            title="Remove this finished session from the fleet (kept in history)"
            onClick={onArchive}
          >
            Archive
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function FleetView() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const { data, error, isLoading } = useQuery({
    queryKey: ["sessions"],
    queryFn: api.sessions,
    refetchInterval: 15_000, // fallback; WS invalidation is the fast path
  });

  useLiveEvents("session.*", () => {
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  });

  // All hooks must run every render (Rules of Hooks): keep this above the
  // loading/error early returns, never below them.
  const archive = useMutation({
    mutationFn: (id: string) => api.archiveSession(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });

  if (isLoading) return <div className="notice">Loading sessions…</div>;
  if (error) return <div className="notice error">{String(error)}</div>;

  const sessions = data?.sessions ?? [];
  // Sessions needing a human float to the top of the active grid. Archived
  // ones are "cleaned up" — hidden from the fleet, still in the event log.
  const active = sessions
    .filter((s) => isActive(s.state ?? ""))
    .sort((a, b) => Number(b.state === "waiting_on_user") - Number(a.state === "waiting_on_user"));
  const historical = sessions.filter(
    (s) => !isActive(s.state ?? "") && s.state !== "archived",
  );

  return (
    <div className="fleet">
      <div className="fleet-toolbar">
        <button type="button" onClick={() => setCreating(!creating)}>
          {creating ? "Close" : "New session"}
        </button>
      </div>
      {creating ? <NewSessionForm onClose={() => setCreating(false)} /> : null}
      {sessions.length === 0 ? (
        <div className="notice">
          No sessions yet. Start a Claude Code session and it will appear here — or launch one
          with New session.
        </div>
      ) : null}
      {active.length > 0 ? (
        <>
          <h2>Active</h2>
          <div className="grid">
            {active.map((s) => (
              <SessionCard key={s.id} session={s} />
            ))}
          </div>
        </>
      ) : null}
      {historical.length > 0 ? (
        <>
          <h2>History</h2>
          <div className="grid">
            {historical.map((s) => (
              <SessionCard key={s.id} session={s} onArchive={() => archive.mutate(s.id)} />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
