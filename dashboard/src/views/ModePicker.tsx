/** Permission-mode selector: the four ways Claude Code handles permissions.
 *
 * Human labels here, adapter-native values on the wire — the mapping mirrors
 * the server's PERMISSION_MODES. Used both for a launch (choose the starting
 * mode) and for a running session (switch it live, ADR: set_permission_mode).
 */
export const MODES: { value: string; label: string; hint: string }[] = [
  { value: "default", label: "Manual", hint: "Ask me before each action" },
  { value: "plan", label: "Plan", hint: "Plan only — no tools run" },
  { value: "acceptEdits", label: "Edit Automatically", hint: "Auto-accept file edits" },
  { value: "bypassPermissions", label: "Auto", hint: "Bypass all permission checks" },
];

export function modeLabel(value: string): string {
  return MODES.find((m) => m.value === value)?.label ?? value;
}

export function ModePicker({
  id,
  value,
  onChange,
  disabled,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const current = MODES.find((m) => m.value === value);
  return (
    <select
      id={id}
      className="mode-picker"
      value={value}
      disabled={disabled}
      title={current?.hint}
      onChange={(e) => onChange(e.target.value)}
    >
      {MODES.map((m) => (
        <option key={m.value} value={m.value}>
          {m.label}
        </option>
      ))}
    </select>
  );
}
