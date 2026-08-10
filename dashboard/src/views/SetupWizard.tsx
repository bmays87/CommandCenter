import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { AppStatus, AssetStatus } from "../api/types";

/**
 * Guided voice setup: choose where models live, fetch the ones that do not
 * download themselves, then start the client — the shell ritual this phase
 * exists to replace.
 *
 * The steps are derived from real state (settings, asset presence, app state)
 * rather than a stored wizard position, so it is always correct to reopen and
 * it never claims a step is done when the file is not there.
 */

function Step({
  n,
  title,
  done,
  children,
}: {
  n: number;
  title: string;
  done: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className={`wizard-step ${done ? "wizard-done" : ""}`}>
      <div className="wizard-head">
        <span className="wizard-num">{done ? "✓" : n}</span>
        <strong>{title}</strong>
      </div>
      <div className="wizard-body">{children}</div>
    </li>
  );
}

export function SetupWizard({ app }: { app: AppStatus }) {
  const queryClient = useQueryClient();
  const [dir, setDir] = useState<string | null>(null);
  const [error, setError] = useState("");

  const settings = useQuery({ queryKey: ["extension-settings"], queryFn: api.extensionSettings });
  // Piper is the engine whose model does not fetch itself, and without it the
  // client exits at startup with a voice_path validation error.
  const assets = useQuery({
    queryKey: ["assets", "piper"],
    queryFn: () => api.extensionAssets("piper"),
  });

  const saveDir = useMutation({
    mutationFn: (models_dir: string) =>
      api.saveExtensionSettings({
        autostart: settings.data?.autostart ?? [],
        license_key: settings.data?.license_key ?? "",
        models_dir,
      }),
    onSuccess: () => {
      setDir(null);
      void queryClient.invalidateQueries({ queryKey: ["extension-settings"] });
      void queryClient.invalidateQueries({ queryKey: ["assets", "piper"] });
    },
  });

  const download = useMutation({
    mutationFn: (id: string) => api.downloadAsset("piper", id),
    onSuccess: (result) => {
      setError(result.ok ? "" : result.error || "download failed");
      void queryClient.invalidateQueries({ queryKey: ["assets", "piper"] });
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  const start = useMutation({
    mutationFn: () => api.startApp(app.name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["apps"] }),
  });

  const modelsDir = settings.data?.models_dir ?? "";
  const list: AssetStatus[] = assets.data?.assets ?? [];
  const allPresent = list.length > 0 && list.every((a) => a.present);
  const running = app.state === "running" || app.state === "starting";

  return (
    <section className="ext-detail">
      <h2>Set up {app.name}</h2>
      <p className="field-help">
        Everything below runs on this machine. No terminal required.
      </p>
      <ol className="wizard">
        <Step n={1} title="Choose where models are stored" done={Boolean(modelsDir)}>
          <div className="form-row">
            <input
              type="text"
              value={dir ?? modelsDir}
              placeholder="e.g. F:\prodeo\models"
              onChange={(e) => setDir(e.target.value)}
            />
            <p className="field-help">
              Speech and voice models run to hundreds of megabytes. Pick a drive with room —
              this is also used for the speech-to-text caches.
            </p>
          </div>
          <button
            type="button"
            onClick={() => saveDir.mutate(dir ?? modelsDir)}
            disabled={saveDir.isPending}
          >
            {saveDir.isPending ? "Saving…" : "Save"}
          </button>
        </Step>

        <Step n={2} title="Download the voice" done={allPresent}>
          {list.length === 0 ? (
            <p className="field-help">No downloadable assets found for the Piper engine.</p>
          ) : (
            list.map((asset) => (
              <div key={asset.id} className="wizard-asset">
                <div>
                  {asset.label} {asset.approx_mb ? <span>· ~{asset.approx_mb} MB</span> : null}
                </div>
                {asset.present ? (
                  <code className="field-help">{asset.produces?.[0] ?? ""}</code>
                ) : (
                  <button
                    type="button"
                    onClick={() => download.mutate(asset.id)}
                    disabled={download.isPending || !modelsDir}
                  >
                    {download.isPending ? "Downloading…" : "Download"}
                  </button>
                )}
              </div>
            ))
          )}
          {error ? <div className="notice error">{error}</div> : null}
          <p className="field-help">
            The path is written into the client&rsquo;s config for you. Both the model and its
            companion <code>.onnx.json</code> must be present — with only one of them the client
            starts and is silently mute.
          </p>
        </Step>

        <Step n={3} title="Start the voice client" done={running}>
          {running ? (
            <p className="field-help">
              Running{app.present ? " and heartbeating." : " — waiting for its first heartbeat."}
            </p>
          ) : (
            <button
              type="button"
              onClick={() => start.mutate()}
              disabled={!allPresent || start.isPending}
              title={allPresent ? undefined : "Download the voice first"}
            >
              {start.isPending ? "Starting…" : "Start"}
            </button>
          )}
          {app.last_error && !running ? (
            <div className="notice error">{app.last_error}</div>
          ) : null}
        </Step>
      </ol>
    </section>
  );
}
