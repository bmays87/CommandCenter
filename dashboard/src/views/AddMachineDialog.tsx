import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { CcanInstallers } from "./CcanInstallers";

/**
 * Pair with a CCAN by FQDN/IP and register its machine as a new tab.
 *
 * Until the CCAN package lands the server answers 501 with an explanation,
 * which surfaces here as the error notice — the flow is final, the pairing
 * behind it arrives with Phase 6 workstream B.
 */
export function AddMachineDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [address, setAddress] = useState("");
  const [error, setError] = useState("");

  const add = useMutation({
    mutationFn: () => api.addMachine(address.trim()),
    onSuccess: (machine) => {
      void queryClient.invalidateQueries({ queryKey: ["machines"] });
      onClose();
      window.location.hash = `#/m/${machine.id}`;
    },
    onError: (err: unknown) => setError(err instanceof Error ? err.message : String(err)),
  });

  return (
    <div className="picker-backdrop" role="dialog" aria-modal="true" aria-label="Add Machine">
      <div className="picker">
        <div className="picker-head">Add Machine</div>
        <div className="add-machine-body">
          <div className="form-row">
            <label htmlFor="am-address">Machine address</label>
            <input
              id="am-address"
              value={address}
              autoFocus
              placeholder="e.g. workstation-02.lan or 192.168.1.40"
              onChange={(e) => setAddress(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && address.trim()) add.mutate();
              }}
            />
            <p className="field-help">
              The FQDN or IP address of a machine already running CCAN. A CCAN answers only
              the Command Center whose installer set it up — install one with the button
              below if this machine doesn&apos;t run it yet.
            </p>
          </div>
          {error ? <div className="notice error">{error}</div> : null}
          <div className="ext-actions">
            <button
              type="button"
              onClick={() => add.mutate()}
              disabled={add.isPending || !address.trim()}
            >
              {add.isPending ? "Pairing…" : "Add Machine"}
            </button>
            <button type="button" onClick={onClose}>
              Cancel
            </button>
          </div>
          <CcanInstallers />
        </div>
      </div>
    </div>
  );
}
