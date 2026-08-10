import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { AppStatus } from "../api/types";
import { timeAgo } from "../format";
import { SetupWizard } from "./SetupWizard";

/**
 * App-class extensions: separate processes the server supervises, as opposed
 * to in-process plugins. Mjölnir is the only one today.
 */

function StateBadge({ app }: { app: AppStatus }) {
  const running = app.state === "running";
  // "running" is about the process; "present" is about the client actually
  // heartbeating. A process that is up but silent is worth distinguishing.
  const label = running && !app.present ? "running (no heartbeat)" : app.state;
  return <span className={`badge app-${app.state}`}>{label}</span>;
}

function AppCard({ app, onSetup }: { app: AppStatus; onSetup: () => void }) {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["apps"] });

  const start = useMutation({ mutationFn: () => api.startApp(app.name), onSuccess: refresh });
  const stop = useMutation({ mutationFn: () => api.stopApp(app.name), onSuccess: refresh });
  const restart = useMutation({ mutationFn: () => api.restartApp(app.name), onSuccess: refresh });
  const autostart = useMutation({
    mutationFn: (on: boolean) => api.setAppAutostart(app.name, on),
    onSuccess: refresh,
  });

  const busy = start.isPending || stop.isPending || restart.isPending;
  const live = app.state === "running" || app.state === "starting";
  // Server-computed (ADR-0017): setup steps still missing. The server refuses
  // a start while any remain, so offering the button would only offer a 412.
  const gaps = app.unmet_setup ?? [];
  const notReady = gaps.length > 0;

  return (
    <div className="card ext-card">
      <div className="card-top">
        <span className="agent-chip">app</span>
        <StateBadge app={app} />
      </div>
      <div className="card-title">{app.name}</div>
      {app.description ? <p className="card-meta">{app.description}</p> : null}
      <div className="card-footer">
        {app.pid ? <span>pid {app.pid}</span> : null}
        {app.started_at ? <span>up {timeAgo(app.started_at)}</span> : null}
        {app.restarts > 0 ? <span className="model">{app.restarts} restarts</span> : null}
      </div>
      {app.last_error && !live ? <div className="notice error">{app.last_error}</div> : null}
      {notReady && !live ? (
        <div className="notice app-gaps">
          Complete setup before starting:
          <ul>
            {gaps.map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {start.error ? <div className="notice error">{String(start.error.message)}</div> : null}

      <div className="ext-actions">
        {live ? (
          <button type="button" onClick={() => stop.mutate()} disabled={busy}>
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={() => start.mutate()}
            disabled={busy || notReady}
            title={notReady ? gaps.join("; ") : undefined}
          >
            Start
          </button>
        )}
        <button type="button" onClick={() => restart.mutate()} disabled={busy || !live}>
          Restart
        </button>
      </div>

      <label className="app-autostart">
        <input
          type="checkbox"
          checked={app.autostart}
          onChange={(e) => autostart.mutate(e.target.checked)}
        />
        Start automatically with the server
      </label>
      <button type="button" className="wizard-link" onClick={onSetup}>
        Set up…
      </button>
    </div>
  );
}

export function AppsPanel() {
  const [setupFor, setSetupFor] = useState<string | null>(null);
  const { data, error, isLoading } = useQuery({
    queryKey: ["apps"],
    queryFn: api.apps,
    // Process state changes without an event to hang off, and presence lags
    // its TTL, so a modest poll is the honest way to keep this current.
    refetchInterval: 5_000,
  });

  if (isLoading) return null;
  if (error) return <div className="notice error">{String(error)}</div>;

  const apps = data?.apps ?? [];
  if (apps.length === 0) return null;
  const wizardApp = apps.find((a) => a.name === setupFor);

  return (
    <>
      <h2>Apps</h2>
      <p className="field-help">
        Separate processes the server can run for you. Audio-using apps inherit this
        server&rsquo;s session — start the server from the desktop you want them to listen in.
      </p>
      <div className="grid">
        {apps.map((app) => (
          <AppCard
            key={app.name}
            app={app}
            onSetup={() => setSetupFor(setupFor === app.name ? null : app.name)}
          />
        ))}
      </div>
      {wizardApp ? <SetupWizard app={wizardApp} /> : null}
    </>
  );
}
