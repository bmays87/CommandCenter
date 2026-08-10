import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { CatalogEntry, ExtensionSummary } from "../api/types";
import { AppsPanel } from "./AppsPanel";
import { DirectoryPicker } from "./DirectoryPicker";
import { EnvironmentPanel } from "./EnvironmentPanel";
import { RestartButton } from "./RestartButton";
import { SchemaForm } from "./SchemaForm";

function StatusBadge({ status }: { status: string }) {
  const text =
    status === "hosted_by_client"
      ? "voice client"
      : status === "failed"
        ? "failed"
        : status === "disabled"
          ? "disabled"
          : "loaded";
  return <span className={`badge ext-${status}`}>{text}</span>;
}


function ExtensionCard({
  extension,
  selected,
  onSelect,
}: {
  extension: ExtensionSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`card ext-card ${selected ? "card-active" : ""}`}
      onClick={onSelect}
    >
      <div className="card-top">
        {extension.kind ? <span className="agent-chip">{extension.kind}</span> : null}
        <StatusBadge status={extension.status} />
      </div>
      <div className="card-title">{extension.name}</div>
      {extension.description ? <p className="card-meta">{extension.description}</p> : null}
      <div className="card-footer">
        {extension.version ? <span>v{extension.version}</span> : null}
        {extension.license ? <span className="model">{extension.license}</span> : null}
      </div>
    </button>
  );
}

function AvailableCard({ entry, onInstalled }: { entry: CatalogEntry; onInstalled: () => void }) {
  const [error, setError] = useState("");
  const install = useMutation({
    mutationFn: () => api.installExtension(entry.name),
    onSuccess: (result) => {
      if (result.ok) {
        setError("");
        onInstalled();
      } else {
        setError(result.error || "install failed");
      }
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  const paid = entry.tier === "paid";
  // Server-computed against this machine: a non-empty list means installing
  // would only waste a large download.
  const unmet = entry.unmet ?? [];
  const blocked = unmet.length > 0;
  return (
    <div className="card ext-card ext-available">
      <div className="card-top">
        <span className="agent-chip">{entry.kind || entry.extension_class}</span>
        <span className={`badge ${blocked ? "ext-blocked" : `ext-tier-${entry.tier}`}`}>
          {blocked ? "unsupported here" : paid ? "paid" : entry.tier === "bundled" ? "bundled" : "not installed"}
        </span>
      </div>
      <div className="card-title">{entry.name}</div>
      {entry.description ? <p className="card-meta">{entry.description}</p> : null}
      <div className="card-footer">
        <code>{entry.package}</code>
        {entry.license ? <span className="model"> {entry.license}</span> : null}
      </div>
      {entry.requires?.note ? <p className="field-help">{entry.requires.note}</p> : null}
      {entry.tier_note ? <p className="field-help">{entry.tier_note}</p> : null}
      {blocked ? (
        <ul className="ext-unmet">
          {unmet.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
      <button
        type="button"
        onClick={() => install.mutate()}
        disabled={install.isPending || blocked}
        title={blocked ? unmet.join("; ") : undefined}
      >
        {install.isPending
          ? "Installing…"
          : blocked
            ? "Unavailable"
            : paid
              ? "Install (licensed)"
              : "Install"}
      </button>
      {install.isPending ? (
        <p className="field-help">
          First install from a source checkout builds the workspace wheels — this takes a minute.
        </p>
      ) : null}
      {/* Both plugins and apps are discovered once, at boot, so a successful
          install changes nothing visible until the server comes back. Saying so
          here is what stops a working install from reading as a failed one. */}
      {install.isSuccess && install.data?.ok ? (
        <>
          <div className="notice">Installed. It appears after the server restarts.</div>
          <RestartButton />
        </>
      ) : null}
      {error ? <div className="notice error">{error}</div> : null}
    </div>
  );
}

function ManagerSettings() {
  const queryClient = useQueryClient();
  const [models, setModels] = useState<string | null>(null);
  const [licence, setLicence] = useState<string | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const settings = useQuery({ queryKey: ["extension-settings"], queryFn: api.extensionSettings });
  const modelsDir = models ?? settings.data?.models_dir ?? "";

  const save = useMutation({
    mutationFn: () =>
      api.saveExtensionSettings({
        autostart: settings.data?.autostart ?? [],
        models_dir: models ?? settings.data?.models_dir ?? "",
        license_key: licence ?? settings.data?.license_key ?? "",
      }),
    onSuccess: () => {
      setModels(null);
      setLicence(null);
      void queryClient.invalidateQueries({ queryKey: ["extension-settings"] });
    },
  });

  return (
    <section className="ext-detail">
      <h2>Settings</h2>
      <div className="form-row">
        <label htmlFor="models-dir">Models directory</label>
        <div className="input-with-button">
          <input
            id="models-dir"
            type="text"
            value={modelsDir}
            placeholder="e.g. F:\prodeo\models"
            onChange={(e) => setModels(e.target.value)}
          />
          <button type="button" onClick={() => setBrowsing(true)}>
            Browse…
          </button>
        </div>
        <p className="field-help">
          Where downloaded models, voices, and weights are written. These run to gigabytes — put
          them somewhere with room, not necessarily the system drive. Empty uses each
          engine&rsquo;s own default.
        </p>
      </div>
      {browsing ? (
        <DirectoryPicker
          initialPath={modelsDir}
          onCancel={() => setBrowsing(false)}
          onSelect={(path) => {
            setModels(path);
            setBrowsing(false);
          }}
        />
      ) : null}
      <div className="form-row">
        <label htmlFor="licence-key">Licence key</label>
        <input
          id="licence-key"
          type="password"
          value={licence ?? settings.data?.license_key ?? ""}
          placeholder="required for paid extensions"
          onChange={(e) => setLicence(e.target.value)}
        />
        <p className="field-help">Unlocks paid extensions such as Mjölnir.</p>
      </div>
      <button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
        {save.isPending ? "Saving…" : "Save"}
      </button>
    </section>
  );
}

function ConfigPanel({ name, status }: { name: string; status: string }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const detail = useQuery({ queryKey: ["extension", name], queryFn: () => api.extension(name) });
  const config = useQuery({
    queryKey: ["extension-config", name],
    queryFn: () => api.extensionConfig(name),
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["extensions"] });
    void queryClient.invalidateQueries({ queryKey: ["extension", name] });
  };

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.saveExtensionConfig(name, values),
    onSuccess: () => {
      setError("");
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ["extension-config", name] });
    },
    onError: (err: unknown) => {
      setSaved(false);
      setError(err instanceof Error ? err.message : String(err));
    },
  });

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.setExtensionEnabled(name, enabled),
    onSuccess: refresh,
  });

  const uninstall = useMutation({
    mutationFn: () => api.uninstallExtension(name),
    onSuccess: refresh,
  });

  if (detail.isLoading || config.isLoading) return <div className="notice">Loading…</div>;
  if (detail.error) return <div className="notice error">{String(detail.error)}</div>;

  const schema = detail.data?.config_schema;
  const disabled = status === "disabled";
  return (
    <section className="ext-detail">
      <h2>{name}</h2>
      {detail.data?.homepage ? (
        <p>
          <a href={detail.data.homepage} target="_blank" rel="noreferrer">
            {detail.data.homepage}
          </a>
        </p>
      ) : null}

      <div className="ext-actions">
        <button type="button" onClick={() => toggle.mutate(disabled)} disabled={toggle.isPending}>
          {disabled ? "Enable" : "Disable"}
        </button>
        <button
          type="button"
          className="danger"
          onClick={() => uninstall.mutate()}
          disabled={uninstall.isPending}
        >
          {uninstall.isPending ? "Removing…" : "Uninstall"}
        </button>
      </div>
      {toggle.isSuccess || uninstall.isSuccess ? <RestartButton /> : null}
      {uninstall.data && !uninstall.data.ok ? (
        <div className="notice error">{uninstall.data.error}</div>
      ) : null}

      {detail.data?.status === "failed" ? (
        <div className="notice error">Failed to load: {detail.data.error}</div>
      ) : null}
      {detail.data?.hosted_by_client ? (
        <div className="notice">
          Loaded by the Mjölnir voice client, not the server. Configure it through the voice
          client&rsquo;s own settings.
        </div>
      ) : null}
      {saved ? <RestartButton /> : null}
      {schema ? (
        <SchemaForm
          schema={schema}
          values={config.data?.values ?? {}}
          sources={config.data?.sources ?? {}}
          busy={save.isPending}
          error={error}
          onSave={(values) => save.mutate(values)}
        />
      ) : (
        <p className="notice">This extension has no settings.</p>
      )}
    </section>
  );
}

export function ExtensionsView() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);

  const installed = useQuery({ queryKey: ["extensions"], queryFn: api.extensions });
  const catalog = useQuery({ queryKey: ["extension-catalog"], queryFn: api.extensionCatalog });
  // Apps are a second extension class, not a plugin kind (ADR-0015), so they
  // are absent from /api/extensions entirely. Without them an app-class catalog
  // entry can never leave "Available" and re-offers Install after a successful
  // install. Same query key as AppsPanel, so this shares one cache entry.
  const apps = useQuery({ queryKey: ["apps"], queryFn: api.apps });

  if (installed.isLoading) return <div className="notice">Loading extensions…</div>;
  if (installed.error) return <div className="notice error">{String(installed.error)}</div>;

  const extensions = installed.data?.extensions ?? [];
  const installedNames = new Set([
    ...extensions.map((e) => e.name),
    ...(apps.data?.apps ?? []).map((a) => a.name),
  ]);
  const available = (catalog.data?.entries ?? []).filter((e) => !installedNames.has(e.name));
  const current = extensions.find((e) => e.name === selected);

  return (
    <div className="extensions">
      <AppsPanel />
      <h2>Installed</h2>
      {extensions.length === 0 ? (
        <div className="notice">Nothing installed yet.</div>
      ) : (
        <div className="grid">
          {extensions.map((e) => (
            <ExtensionCard
              key={e.name}
              extension={e}
              selected={selected === e.name}
              onSelect={() => setSelected(selected === e.name ? null : e.name)}
            />
          ))}
        </div>
      )}

      {current ? <ConfigPanel name={current.name} status={current.status} /> : null}

      {available.length > 0 ? (
        <>
          <h2>Available</h2>
          <div className="grid">
            {available.map((e) => (
              <AvailableCard
                key={e.name}
                entry={e}
                onInstalled={() => {
                  void queryClient.invalidateQueries({ queryKey: ["extensions"] });
                  void queryClient.invalidateQueries({ queryKey: ["apps"] });
                }}
              />
            ))}
          </div>
        </>
      ) : null}

      <EnvironmentPanel />
      <ManagerSettings />
    </div>
  );
}
