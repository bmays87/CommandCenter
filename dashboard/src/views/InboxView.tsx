import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import type { Interaction } from "../api/types";
import { InteractionCard } from "./InteractionCard";

/** All unanswered permissions/questions across sessions. */
export function InboxView() {
  const { data, error, isLoading } = useQuery({
    queryKey: ["interactions"],
    queryFn: () => api.interactions(),
    refetchInterval: 15_000, // fallback; WS invalidation is the fast path
  });

  if (isLoading) return <div className="notice">Loading interactions…</div>;
  if (error) return <div className="notice error">{String(error)}</div>;

  const interactions = data?.interactions ?? [];
  const pending = interactions.filter((i) => i.status === "pending");
  const resolved = interactions.filter((i) => i.status !== "pending").slice(-10);

  const bySession = new Map<string, Interaction[]>();
  for (const interaction of pending) {
    const group = bySession.get(interaction.session_id) ?? [];
    group.push(interaction);
    bySession.set(interaction.session_id, group);
  }

  return (
    <div className="inbox">
      {pending.length === 0 ? (
        <div className="notice">Inbox zero — no agent is waiting on you.</div>
      ) : (
        [...bySession.entries()].map(([sessionId, group]) => (
          <section key={sessionId} className="inbox-group">
            {group.map((interaction) => (
              <InteractionCard key={interaction.id} interaction={interaction} showSessionLink />
            ))}
          </section>
        ))
      )}
      {resolved.length > 0 ? (
        <>
          <h2>Recently resolved</h2>
          <div className="inbox-resolved">
            {resolved.reverse().map((i) => (
              <div key={i.id} className="resolved-row">
                <span className={`badge status-${i.status}`}>{i.status.replace(/_/g, " ")}</span>
                <span className="interaction-title">{i.title}</span>
                {i.answer?.decision ? <span className="decision">{i.answer.decision}</span> : null}
                {i.answered_by ? <span className="by">by {i.answered_by}</span> : null}
              </div>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
