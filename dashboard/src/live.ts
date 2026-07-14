// A thin WebSocket layer: one connection, ULID cursor, reconnection with
// backoff, and per-animation-frame batching of incoming events.
import { useEffect, useRef } from "react";

import { wsUrl } from "./api/client";
import type { ProdeoEvent } from "./api/types";

export type EventHandler = (events: ProdeoEvent[]) => void;

export function useLiveEvents(types: string, onEvents: EventHandler): void {
  const handlerRef = useRef<EventHandler>(onEvents);
  handlerRef.current = onEvents;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let backoff = 500;
    let cursor: string | undefined;
    let pending: ProdeoEvent[] = [];
    let frame = 0;

    const flush = () => {
      frame = 0;
      if (pending.length > 0) {
        const batch = pending;
        pending = [];
        handlerRef.current(batch);
      }
    };

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(wsUrl(types, cursor));
      ws.onopen = () => {
        backoff = 500;
      };
      ws.onmessage = (msg: MessageEvent<string>) => {
        const event = JSON.parse(msg.data) as ProdeoEvent;
        cursor = event.id ?? cursor;
        pending.push(event);
        if (frame === 0) frame = requestAnimationFrame(flush);
      };
      ws.onclose = () => {
        if (closed) return;
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 15_000);
      };
    };

    connect();
    return () => {
      closed = true;
      if (frame !== 0) cancelAnimationFrame(frame);
      ws?.close();
    };
  }, [types]);
}
