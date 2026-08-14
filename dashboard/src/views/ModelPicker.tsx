import { useState } from "react";

import type { ModelInfo } from "../api/types";

/** Sentinel option that switches the picker into free-text mode. */
const CUSTOM = "__custom__";

/** Model picker fed by the adapter's declared catalog (GET /api/adapters).
 *
 * Renders a dropdown of the adapter's models plus "Custom model id…" for
 * anything the adapter accepts natively (full ids stay legal everywhere).
 * The empty value means "the agent's default". With no catalog declared,
 * degrades to a plain free-text input.
 */
export function ModelPicker({
  id,
  value,
  onChange,
  models,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  models: ModelInfo[];
  disabled?: boolean;
}) {
  // A value the catalog doesn't know (e.g. a full model id) starts in custom mode.
  const known = value === "" || models.some((m) => m.id === value);
  const [custom, setCustom] = useState(!known);

  if (models.length === 0 || custom) {
    return (
      <span className="model-picker">
        <input
          id={id}
          type="text"
          value={value}
          placeholder="agent default"
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
        {models.length > 0 ? (
          <button
            type="button"
            disabled={disabled}
            title="Back to the model list"
            onClick={() => {
              setCustom(false);
              if (!models.some((m) => m.id === value)) onChange("");
            }}
          >
            List
          </button>
        ) : null}
      </span>
    );
  }
  return (
    <select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        if (e.target.value === CUSTOM) setCustom(true);
        else onChange(e.target.value);
      }}
    >
      <option value="">Agent default</option>
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.label || m.id}
        </option>
      ))}
      <option value={CUSTOM}>Custom model id…</option>
    </select>
  );
}
