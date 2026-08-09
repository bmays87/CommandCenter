import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { CatalogEntry, ExtensionSummary } from "../api/types";
import { SchemaForm } from "./SchemaForm";

function StatusBadge({ extension }: { extension: ExtensionSummary }) {
  const status = extension.status;
  const text =
    status === "hosted_by_client" ? "voice client" : status === "failed" ? "failed" : "loaded";
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
        <StatusBadge extension={extension} />
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

function AvailableCard({ entry }: { entry: CatalogEntry }) {
  return (
    <div className="card ext-card ext-available">
      <div className="card-top">
        <span className="agent-chip">{entry.kind || entry.extension_class}</span>
        <span className="badge ext-available-badge">not installed</span>
      </div>
      <div className="card-title">{entry.name}</div>
      {entry.description ? <p className="card-meta">{entry.description}</p> : null}
      <div className="card-footer">
        <code>uv pip install {entry.package}</code>
      </div>
    </div>
  );
}

function ConfigPanel({ name }: { name: string }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const detail = useQuery({
    queryKey: ["extension", name],
    queryFn: () => api.extension(name),
  });
  const config = useQuery({
    queryKey: ["extension-config", name],
    queryFn: () => api.extensionConfig(name),
  });

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

  if (detail.isLoading || config.isLoading) return <div className="notice">Loading…</div>;
  if (detail.error) return <div className="notice error">{String(detail.error)}</div>;

  const schema = detail.data?.config_schema;
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
      {detail.data?.status === "failed" ? (
        <div className="notice error">Failed to load: {detail.data.error}</div>
      ) : null}
      {detail.data?.hosted_by_client ? (
        <div className="notice">
          Loaded by the Mjölnir voice client, not the server. Configure it through the voice
          client&rsquo;s own settings.
        </div>
      ) : null}
      {saved ? (
        <div className="notice">Saved. Restart the server for this to take effect.</div>
      ) : null}
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
  const [selected, setSelected] = useState<string | null>(null);

  const installed = useQuery({ queryKey: ["extensions"], queryFn: api.extensions });
  const catalog = useQuery({ queryKey: ["extension-catalog"], queryFn: api.extensionCatalog });

  if (installed.isLoading) return <div className="notice">Loading extensions…</div>;
  if (installed.error) return <div className="notice error">{String(installed.error)}</div>;

  const extensions = installed.data?.extensions ?? [];
  const installedNames = new Set(extensions.map((e) => e.name));
  const available = (catalog.data?.entries ?? []).filter((e) => !installedNames.has(e.name));

  return (
    <div className="extensions">
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

      {selected ? <ConfigPanel name={selected} /> : null}

      {available.length > 0 ? (
        <>
          <h2>Available</h2>
          <p className="notice">
            Installing from the dashboard is not wired up yet — install the package, then restart
            the server and it appears above.
          </p>
          <div className="grid">
            {available.map((e) => (
              <AvailableCard key={e.name} entry={e} />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
