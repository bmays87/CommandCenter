/** Free-text model picker with common alias suggestions.
 *
 * Adapters take the value natively (an alias or a full model id); the empty
 * string means "the agent's default". Suggestions are hints only — anything
 * the adapter accepts can be typed.
 */
const SUGGESTIONS = ["sonnet", "opus", "haiku"];

export function ModelInput({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const listId = `${id}-suggestions`;
  return (
    <>
      <input
        id={id}
        type="text"
        list={listId}
        value={value}
        placeholder="agent default"
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {SUGGESTIONS.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
    </>
  );
}
