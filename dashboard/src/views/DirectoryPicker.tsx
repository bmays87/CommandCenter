import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";

/**
 * Browses directories on the *server's* filesystem.
 *
 * It has to work this way round: the value being picked is a path on the
 * machine the server runs on, and the browser cannot supply one —
 * `webkitdirectory` yields names without a path and `showDirectoryPicker`
 * yields an opaque handle (and only in Chromium). Listing server-side also
 * means one component works on Windows and Linux without knowing which it is.
 */
export function DirectoryPicker({
  initialPath,
  onSelect,
  onCancel,
}: {
  initialPath: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}) {
  // "" is the root listing: drive letters on Windows, / on POSIX.
  const [path, setPath] = useState(initialPath);
  const [newFolder, setNewFolder] = useState("");

  const listing = useQuery({
    queryKey: ["browse", path],
    queryFn: () => api.browse(path),
    // A path the user typed by hand may not exist; the error is the answer,
    // and retrying it three times just delays showing them.
    retry: false,
  });

  const entries = listing.data?.entries ?? [];
  const parent = listing.data?.parent ?? null;
  const here = listing.data?.path ?? "";
  const separator = here.includes("\\") ? "\\" : "/";
  const chosen = newFolder.trim() ? `${here.replace(/[\\/]+$/, "")}${separator}${newFolder.trim()}` : here;

  return (
    <div className="picker-backdrop" role="dialog" aria-modal="true" aria-label="Choose a folder">
      <div className="picker">
        <div className="picker-head">
          <h3>Choose a folder</h3>
          <code className="picker-path">{here || "This computer"}</code>
        </div>

        <div className="picker-nav">
          <button type="button" onClick={() => setPath("")} disabled={!here}>
            Roots
          </button>
          <button type="button" onClick={() => setPath(parent ?? "")} disabled={!here}>
            Up
          </button>
        </div>

        {listing.isLoading ? <div className="notice">Loading…</div> : null}
        {listing.error ? (
          <div className="notice error">{String((listing.error as Error).message)}</div>
        ) : null}

        <ul className="picker-list">
          {entries.length === 0 && !listing.isLoading && !listing.error ? (
            <li className="picker-empty">No sub-folders here.</li>
          ) : null}
          {entries.map((entry) => (
            <li key={entry.path}>
              <button type="button" onClick={() => setPath(entry.path)}>
                {entry.name}
              </button>
            </li>
          ))}
        </ul>

        {/* Model storage usually wants a folder that does not exist yet; making
            the user leave and create it by hand would defeat the picker. */}
        <div className="form-row">
          <label htmlFor="picker-new">New sub-folder (optional)</label>
          <input
            id="picker-new"
            type="text"
            value={newFolder}
            placeholder="e.g. models"
            onChange={(e) => setNewFolder(e.target.value)}
          />
          <p className="field-help">
            Created by whatever writes there first; nothing is created by choosing it.
          </p>
        </div>

        <div className="picker-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" onClick={() => onSelect(chosen)} disabled={!chosen}>
            Select {chosen ? <code>{chosen}</code> : null}
          </button>
        </div>
      </div>
    </div>
  );
}
