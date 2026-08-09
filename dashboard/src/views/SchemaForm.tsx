import { useState } from "react";

/**
 * A deliberately small renderer for the JSON Schema pydantic emits from a
 * plugin's `config_model`. Every config model shipped so far is flat scalars,
 * so string/number/boolean/enum covers them; anything richer (objects, arrays)
 * falls back to a JSON textarea rather than pulling a form library into a tree
 * that has no router, UI kit, or state library.
 */

interface FieldSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  anyOf?: FieldSchema[];
}

interface ObjectSchema {
  properties?: Record<string, FieldSchema>;
  required?: string[];
}

/** Unwrap `str | None`, which pydantic emits as `anyOf: [{string}, {null}]`. */
function effective(field: FieldSchema): FieldSchema {
  if (!field.anyOf) return field;
  const concrete = field.anyOf.find((f) => f.type !== "null");
  return concrete ? { ...concrete, ...field, anyOf: undefined } : field;
}

function label(name: string, field: FieldSchema): string {
  return field.title ?? name;
}

function Field({
  name,
  schema,
  value,
  required,
  onChange,
}: {
  name: string;
  schema: FieldSchema;
  value: unknown;
  required: boolean;
  onChange: (next: unknown) => void;
}) {
  const field = effective(schema);
  const shown = value ?? field.default ?? "";
  const common = { id: `cfg-${name}`, name };

  let input;
  if (field.enum) {
    input = (
      <select
        {...common}
        value={String(shown)}
        onChange={(e) => onChange(e.target.value)}
      >
        {field.enum.map((option) => (
          <option key={String(option)} value={String(option)}>
            {String(option)}
          </option>
        ))}
      </select>
    );
  } else if (field.type === "boolean") {
    input = (
      <input
        {...common}
        type="checkbox"
        checked={Boolean(value ?? field.default ?? false)}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  } else if (field.type === "integer" || field.type === "number") {
    input = (
      <input
        {...common}
        type="number"
        step={field.type === "integer" ? 1 : "any"}
        value={String(shown)}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  } else if (field.type === "object" || field.type === "array") {
    // No sensible widget; let the user edit the JSON directly.
    input = (
      <textarea
        {...common}
        rows={3}
        defaultValue={JSON.stringify(value ?? field.default ?? (field.type === "array" ? [] : {}))}
        onBlur={(e) => {
          try {
            onChange(JSON.parse(e.target.value));
          } catch {
            /* leave the last valid value; the server validates on save too */
          }
        }}
      />
    );
  } else {
    input = (
      <input
        {...common}
        type="text"
        value={String(shown)}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div className="form-row">
      <label htmlFor={`cfg-${name}`}>
        {label(name, field)}
        {required ? <span className="required"> *</span> : null}
      </label>
      {input}
      {field.description ? <p className="field-help">{field.description}</p> : null}
    </div>
  );
}

export function SchemaForm({
  schema,
  values,
  sources,
  busy,
  error,
  onSave,
}: {
  schema: ObjectSchema;
  values: Record<string, unknown>;
  sources: Record<string, string>;
  busy: boolean;
  error: string;
  onSave: (values: Record<string, unknown>) => void;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(values);
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);

  const fields = Object.entries(properties);
  if (fields.length === 0) return <p className="notice">This extension has no settings.</p>;

  return (
    <form
      className="schema-form"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(draft);
      }}
    >
      {fields.map(([name, field]) => (
        <div key={name}>
          <Field
            name={name}
            schema={field}
            value={draft[name]}
            required={required.has(name)}
            onChange={(next) => setDraft({ ...draft, [name]: next })}
          />
          {sources[name] === "environment" ? (
            <p className="field-help">Currently set by an environment variable.</p>
          ) : null}
        </div>
      ))}
      {error ? <div className="notice error">{error}</div> : null}
      <button type="submit" disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
