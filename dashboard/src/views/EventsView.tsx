import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { ProdeoEvent } from "../api/types";
import { shortTime } from "../format";
import { useLiveEvents } from "../live";

const PAGE = 100;

function EventRow({ event }: { event: ProdeoEvent }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="explorer-row">
      <button className="explorer-line" onClick={() => setOpen(!open)}>
        <span className="ts">{shortTime(event.timestamp)}</span>
        <span className="explorer-type">{event.type}</span>
        <span className="explorer-source">{event.source}</span>
        {event.session_id ? (
          <a
            className="explorer-session"
            href={`#/session/${event.session_id}`}
            onClick={(e) => e.stopPropagation()}
          >
            {event.session_id.slice(-6)}
          </a>
        ) : null}
      </button>
      {open ? <pre className="explorer-json">{JSON.stringify(event, null, 2)}</pre> : null}
    </div>
  );
}

/** Filterable raw event history, newest first, with an optional live tail. */
export function EventsView() {
  const [pattern, setPattern] = useState("*");
  const [session, setSession] = useState("");
  const [tail, setTail] = useState(true);
  const [events, setEvents] = useState<ProdeoEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [exhausted, setExhausted] = useState(false);
  const [error, setError] = useState("");
  const filterRef = useRef({ pattern, session });
  filterRef.current = { pattern, session };
  const cursorRef = useRef<string | null>(null);
  cursorRef.current = cursor;

  const load = useCallback(async (reset: boolean) => {
    const { pattern, session } = filterRef.current;
    try {
      const resp = await api.events({
        type: pattern || "*",
        session: session || undefined,
        order: "desc",
        limit: PAGE,
        before: reset ? undefined : (cursorRef.current ?? undefined),
      });
      setError("");
      setEvents((prev) => (reset ? resp.events : [...prev, ...resp.events]));
      setCursor(resp.cursor);
      setExhausted(resp.events.length < PAGE);
    } catch (err: unknown) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void load(true);
  }, [load, pattern, session]);

  useLiveEvents(tail ? pattern || "*" : "", (batch) => {
    const { session } = filterRef.current;
    const mine = session ? batch.filter((e) => e.session_id === session) : batch;
    if (mine.length > 0) {
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.id));
        const fresh = mine.filter((e) => e.id && !seen.has(e.id));
        return fresh.length > 0 ? [...fresh.reverse(), ...prev] : prev;
      });
    }
  });

  return (
    <div className="explorer">
      <div className="explorer-filters">
        <input
          className="interaction-input"
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
          placeholder="type pattern: *, session.*, tool.failed"
        />
        <input
          className="interaction-input"
          value={session}
          onChange={(e) => setSession(e.target.value)}
          placeholder="session id (optional)"
        />
        <label className="explorer-tail">
          <input type="checkbox" checked={tail} onChange={(e) => setTail(e.target.checked)} />
          live tail
        </label>
      </div>
      {error ? <div className="notice error">{error}</div> : null}
      <div className="explorer-list">
        {events.map((e) => (
          <EventRow key={e.id} event={e} />
        ))}
        {events.length === 0 && !error ? <div className="notice">No events match.</div> : null}
      </div>
      {!exhausted ? (
        <button className="btn option explorer-older" onClick={() => void load(false)}>
          Load older
        </button>
      ) : null}
    </div>
  );
}
