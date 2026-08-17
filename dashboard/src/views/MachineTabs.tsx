import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import type { Machine } from "../api/types";
import { useLiveEvents } from "../live";
import { AddMachineDialog } from "./AddMachineDialog";

/** The machine a `#/m/<id>` route selects, defaulting to the first tab. */
export function selectMachine(machines: Machine[], machineId: string | null): Machine | undefined {
  return machines.find((m) => m.id === machineId) ?? machines[0];
}

/**
 * One tab per connected machine, directly below the header nav.
 *
 * Tab titles are the hub-side display names (renameable via the pencil on
 * the active tab — every dashboard client sees the rename); "+" opens the
 * Add Machine pairing dialog. With no machines at all the strip yields to
 * the fleet view's Add Machine empty state.
 */
export function MachineTabs({ machineId }: { machineId: string | null }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const { data } = useQuery({ queryKey: ["machines"], queryFn: api.machines });
  useLiveEvents("machine.*", () => {
    void queryClient.invalidateQueries({ queryKey: ["machines"] });
  });

  const rename = useMutation({
    mutationFn: (args: { id: string; name: string }) => api.renameMachine(args.id, args.name),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["machines"] }),
  });

  const machines = data?.machines ?? [];
  const selected = selectMachine(machines, machineId);
  if (machines.length === 0) return null;

  const commit = () => {
    setEditing(false);
    const name = draft.trim();
    if (selected && name && name !== selected.name) rename.mutate({ id: selected.id, name });
  };

  return (
    <div className="tabbar">
      {machines.map((m) => {
        const isSelected = selected?.id === m.id;
        return (
          <span key={m.id} className={`tab ${isSelected ? "tab-active" : ""}`}>
            {isSelected && editing ? (
              <input
                className="tab-rename"
                value={draft}
                autoFocus
                aria-label="Machine tab name"
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commit}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commit();
                  if (e.key === "Escape") setEditing(false);
                }}
              />
            ) : (
              <>
                <a className="tab-label" href={`#/m/${m.id}`}>
                  {m.name}
                </a>
                {isSelected ? (
                  <button
                    type="button"
                    className="tab-rename-btn"
                    title="Rename this machine's tab"
                    onClick={() => {
                      setDraft(m.name);
                      setEditing(true);
                    }}
                  >
                    ✎
                  </button>
                ) : null}
              </>
            )}
          </span>
        );
      })}
      <button
        type="button"
        className="tab-add"
        title="Add Machine"
        onClick={() => setAdding(true)}
      >
        +
      </button>
      {adding ? <AddMachineDialog onClose={() => setAdding(false)} /> : null}
    </div>
  );
}
