import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";

/**
 * The "Download CCAN Installer" button and the list it reveals.
 *
 * Installers are produced by this hub (each carries the hub's public
 * certificate, so the CCAN it installs answers only this Command Center).
 * With one platform-agnostic installer the list is a single link; with
 * platform-specific builds it lists them all; while none exist yet the
 * server's note explains why.
 */
export function CcanInstallers() {
  const [open, setOpen] = useState(false);
  const installers = useQuery({
    queryKey: ["ccan-installers"],
    queryFn: api.ccanInstallers,
    enabled: open,
  });

  return (
    <div className="installer-section">
      <button type="button" onClick={() => setOpen(!open)}>
        Download CCAN Installer
      </button>
      {open ? (
        installers.data ? (
          installers.data.installers.length > 0 ? (
            <ul className="installer-list">
              {installers.data.installers.map((i) => (
                <li key={i.id}>
                  <a href={i.url} download>
                    {i.label}
                  </a>{" "}
                  <span className="installer-platform">({i.platform})</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="field-help">{installers.data.note}</p>
          )
        ) : installers.error ? (
          <div className="notice error">{String(installers.error)}</div>
        ) : (
          <p className="field-help">Loading…</p>
        )
      ) : null}
    </div>
  );
}
