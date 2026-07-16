import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api/client";
import type { ProdeoEvent } from "../api/types";
import { projectName, shortTime, timeAgo } from "../format";
import { useLiveEvents } from "../live";
import { InteractionCard } from "./InteractionCard";

function payloadText(event: ProdeoEvent): string {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  return typeof payload["text"] === "string" ? payload["text"] : "";
}

function EventRow({ event }: { event: ProdeoEvent }) {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  const ts = <span className="ts">{shortTime(event.timestamp)}</span>;

  switch (event.type) {
    case "agent.output_appended": {
      const role = typeof payload["role"] === "string" ? payload["role"] : "assistant";
      return (
        <div className={`event output role-${role}`}>
          {ts}
          <span className="role">{role}</span>
          <pre>{payloadText(event)}</pre>
        </div>
      );
    }
    case "tool.started":
    case "tool.finished":
    case "tool.failed": {
      const verb = event.type.split(".")[1];
      return (
        <div className={`event tool tool-${verb}`}>
          {ts}
          <span className="tool-name">
            {String(payload["tool"] ?? "tool")} {verb}
          </span>
          <span className="tool-detail">{String(payload["detail"] ?? "")}</span>
        </div>
      );
    }
    case "session.state_changed":
      return (
        <div className="event state">
          {ts}
          <span>
            {String(payload["from"])} → {String(payload["to"])}
            {payload["reason"] ? ` (${String(payload["reason"])})` : ""}
          </span>
        </div>
      );
    case "agent.turn_started":
    case "agent.turn_completed":
      return (
        <div className="event turn">
          {ts}
          <span>{event.type === "agent.turn_started" ? "— turn started —" : "— turn completed —"}</span>
        </div>
      );
    case "interaction.requested": {
      const interaction = (payload["interaction"] ?? {}) as Record<string, unknown>;
      return (
        <div className="event interaction-event">
          {ts}
          <span className="interaction-mark">⚑</span>
          <span>
            {String(interaction["kind"] ?? "interaction")}: {String(interaction["title"] ?? "")}
          </span>
        </div>
      );
    }
    case "interaction.answered": {
      const answer = (payload["answer"] ?? {}) as Record<string, unknown>;
      const outcome = answer["decision"] ?? answer["text"] ?? "";
      return (
        <div className="event interaction-event">
          {ts}
          <span>answered: {String(outcome)} (by {String(payload["answered_by"] ?? "?")})</span>
        </div>
      );
    }
    case "interaction.timed_out":
      return (
        <div className="event interaction-event">
          {ts}
          <span>interaction timed out</span>
        </div>
      );
    case "interaction.cancelled":
      return (
        <div className="event interaction-event">
          {ts}
          <span>interaction cancelled ({String(payload["reason"] ?? "")})</span>
        </div>
      );
    default:
      return (
        <div className="event other">
          {ts}
          <span>{event.type}</span>
        </div>
      );
  }
}

const TIMELINE_TYPES = "session.*,agent.*,tool.*,interaction.*";

export function SessionView({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const [live, setLive] = useState<ProdeoEvent[]>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  const session = useQuery({ queryKey: ["session", id], queryFn: () => api.session(id) });
  const events = useQuery({
    queryKey: ["events", id],
    queryFn: () => api.sessionEvents(id),
  });
  const pending = useQuery({
    queryKey: ["interactions", id],
    queryFn: () => api.interactions({ session: id, status: "pending" }),
    refetchInterval: 15_000,
  });

  useLiveEvents(TIMELINE_TYPES, (batch) => {
    const mine = batch.filter((e) => e.session_id === id);
    if (mine.length > 0) {
      setLive((prev) => [...prev, ...mine]);
      if (mine.some((e) => e.type.startsWith("session."))) {
        void queryClient.invalidateQueries({ queryKey: ["session", id] });
      }
      if (mine.some((e) => e.type.startsWith("interaction."))) {
        void queryClient.invalidateQueries({ queryKey: ["interactions"] });
      }
    }
  });

  const timeline = useMemo(() => {
    const seen = new Set<string>();
    const merged: ProdeoEvent[] = [];
    for (const e of [...(events.data?.events ?? []), ...live]) {
      if (e.id && !seen.has(e.id)) {
        seen.add(e.id);
        merged.push(e);
      }
    }
    return merged.sort((a, b) => (a.id ?? "").localeCompare(b.id ?? ""));
  }, [events.data, live]);

  useEffect(() => {
    if (pinnedRef.current) bottomRef.current?.scrollIntoView({ behavior: "instant" });
  }, [timeline.length]);

  const s = session.data;
  return (
    <div className="session-view">
      <a className="back" href="#/">
        ← fleet
      </a>
      {s ? (
        <header className="session-header">
          <h1>{s.title || s.native_id}</h1>
          <div className="session-meta">
            <span className="agent-chip">{s.adapter}</span>
            <span className={`badge state-${s.state}`}>{(s.state ?? "").replace(/_/g, " ")}</span>
            <span className="project">{projectName(s.project ?? "")}</span>
            {s.model ? <span className="model">{s.model}</span> : null}
            <span className="ago">{timeAgo(s.last_activity_at)}</span>
          </div>
        </header>
      ) : null}
      {(pending.data?.interactions ?? []).map((interaction) => (
        <InteractionCard key={interaction.id} interaction={interaction} />
      ))}
      <div
        className="timeline"
        onScroll={(e) => {
          const el = e.currentTarget;
          pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {events.isLoading ? <div className="notice">Loading events…</div> : null}
        {timeline.map((e) => (
          <EventRow key={e.id} event={e} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
