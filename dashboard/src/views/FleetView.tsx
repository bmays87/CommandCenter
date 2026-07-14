import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { isActive, type Session } from "../api/types";
import { projectName, timeAgo } from "../format";
import { useLiveEvents } from "../live";

function StateBadge({ state }: { state: string }) {
  return <span className={`badge state-${state}`}>{state.replace(/_/g, " ")}</span>;
}

function SessionCard({ session }: { session: Session }) {
  return (
    <a className={`card ${isActive(session.state ?? "") ? "card-active" : ""}`}
       href={`#/session/${session.id}`}>
      <div className="card-top">
        <span className="agent-chip">{session.adapter}</span>
        <StateBadge state={session.state ?? "discovered"} />
      </div>
      <div className="card-title">{session.title || session.native_id}</div>
      <div className="card-meta">
        <span className="project">{projectName(session.project ?? "")}</span>
        {session.model ? <span className="model">{session.model}</span> : null}
      </div>
      <div className="card-footer">{timeAgo(session.last_activity_at)}</div>
    </a>
  );
}

export function FleetView() {
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["sessions"],
    queryFn: api.sessions,
    refetchInterval: 15_000, // fallback; WS invalidation is the fast path
  });

  useLiveEvents("session.*", () => {
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
  });

  if (isLoading) return <div className="notice">Loading sessions…</div>;
  if (error) return <div className="notice error">{String(error)}</div>;

  const sessions = data?.sessions ?? [];
  const active = sessions.filter((s) => isActive(s.state ?? ""));
  const historical = sessions.filter((s) => !isActive(s.state ?? ""));

  return (
    <div className="fleet">
      {sessions.length === 0 ? (
        <div className="notice">
          No sessions yet. Start a Claude Code session and it will appear here.
        </div>
      ) : null}
      {active.length > 0 ? (
        <>
          <h2>Active</h2>
          <div className="grid">
            {active.map((s) => (
              <SessionCard key={s.id} session={s} />
            ))}
          </div>
        </>
      ) : null}
      {historical.length > 0 ? (
        <>
          <h2>History</h2>
          <div className="grid">
            {historical.map((s) => (
              <SessionCard key={s.id} session={s} />
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
