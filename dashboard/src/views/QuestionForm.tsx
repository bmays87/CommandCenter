import { useState } from "react";

import type { QuestionGroup } from "../api/types";

/** Structured multi-part question UI (ADR-0022): one fieldset per group —
 * radios for single-select, checkboxes for multi-select — and a single
 * Submit, enabled once every group has at least one selection. Posts generic
 * `selections` (group id -> chosen labels); only the adapter that opened the
 * interaction maps them to its agent's native input.
 */
export function QuestionForm({
  questions,
  busy,
  onSubmit,
}: {
  questions: QuestionGroup[];
  busy: boolean;
  onSubmit: (selections: Record<string, string[]>) => void;
}) {
  const [selections, setSelections] = useState<Record<string, string[]>>({});

  const choose = (groupId: string, label: string, multi: boolean, checked: boolean) => {
    setSelections((prev) => {
      const current = prev[groupId] ?? [];
      if (!multi) return { ...prev, [groupId]: [label] };
      return {
        ...prev,
        [groupId]: checked ? [...current.filter((l) => l !== label), label] : current.filter((l) => l !== label),
      };
    });
  };

  const complete = questions.every((group) => (selections[group.id] ?? []).length > 0);

  return (
    <form
      className="question-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete) onSubmit(selections);
      }}
    >
      {questions.map((group) => (
        <fieldset key={group.id} className="question-group" disabled={busy}>
          <legend>
            {group.prompt}
            {group.multi_select ? <span className="question-hint"> (choose all that apply)</span> : null}
          </legend>
          {group.options.map((option) => {
            const chosen = (selections[group.id] ?? []).includes(option.label);
            return (
              <label key={option.label} className="question-option">
                <input
                  type={group.multi_select ? "checkbox" : "radio"}
                  name={group.id}
                  checked={chosen}
                  onChange={(e) => choose(group.id, option.label, group.multi_select ?? false, e.target.checked)}
                />
                <span className="question-label">{option.label}</span>
                {option.description ? (
                  <span className="question-description">{option.description}</span>
                ) : null}
              </label>
            );
          })}
        </fieldset>
      ))}
      <button className="btn approve" type="submit" disabled={busy || !complete}>
        {busy ? "Sending…" : "Submit"}
      </button>
    </form>
  );
}
