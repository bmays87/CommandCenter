import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";

/** How long to keep waiting for the replacement process before giving up. */
const WAIT_TIMEOUT_MS = 90_000;
const POLL_INTERVAL_MS = 750;

/** Queries whose answers are decided at boot, so a restart can change them. */
const BOOT_SCOPED_QUERIES = [
  ["extensions"],
  ["apps"],
  ["extension-catalog"],
  ["environment"],
  ["machines"],
];

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * The boot_id of whatever is answering right now, or null if nothing is.
 *
 * Deliberately not `api.health()`: while the server is down `fetch` rejects,
 * and that is the expected state here rather than an error to surface.
 */
async function currentBootId(): Promise<string | null> {
  try {
    const resp = await fetch("/api/health");
    if (!resp.ok) return null;
    const body = (await resp.json()) as { boot_id?: string };
    return body.boot_id ?? null;
  } catch {
    return null;
  }
}

/**
 * Restarts the server and waits for it to come back.
 *
 * Installing or enabling an extension only takes effect at the next boot: both
 * the Plugin Host and the app supervisor read entry points once, at start. This
 * is the button that finishes the job the manager started.
 *
 * Completion is detected by boot_id changing, not by the server being
 * reachable — for the first moment after the POST the *old* process is still
 * answering, and a reachability check would call that success immediately.
 */
export function RestartButton({ label = "Restart server" }: { label?: string }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const restart = async () => {
    setBusy(true);
    setError("");
    try {
      const before = (await api.restartServer()).boot_id;
      const deadline = Date.now() + WAIT_TIMEOUT_MS;
      for (;;) {
        await sleep(POLL_INTERVAL_MS);
        const now = await currentBootId();
        if (now !== null && now !== before) break;
        if (Date.now() > deadline) {
          setError(
            "The server did not come back within 90 seconds. Check the terminal it was started from.",
          );
          return;
        }
      }
      for (const key of BOOT_SCOPED_QUERIES) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="restart-row">
      <button type="button" onClick={() => void restart()} disabled={busy}>
        {busy ? "Restarting…" : label}
      </button>
      <span className="field-help">
        {busy ? "Waiting for the server to come back…" : "Needed for this to take effect."}
      </span>
      {error ? <div className="notice error">{error}</div> : null}
    </div>
  );
}
