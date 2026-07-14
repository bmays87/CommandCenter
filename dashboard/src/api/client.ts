import type { ProdeoEvent, Session } from "./types";

const TOKEN_KEY = "prodeo_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
  }
}

async function get<T>(path: string): Promise<T> {
  const token = getToken();
  const resp = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (resp.status === 401) throw new UnauthorizedError();
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return (await resp.json()) as T;
}

export interface SessionListResponse {
  sessions: Session[];
}

export interface EventListResponse {
  events: ProdeoEvent[];
  cursor: string | null;
}

export const api = {
  health: () => get<{ status: string; version: string; node: string }>("/api/health"),
  sessions: () => get<SessionListResponse>("/api/sessions"),
  session: (id: string) => get<Session>(`/api/sessions/${id}`),
  sessionEvents: (id: string, limit = 500) =>
    get<EventListResponse>(`/api/sessions/${id}/events?limit=${limit}`),
};

export function wsUrl(types: string, after?: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ types });
  const token = getToken();
  if (token) params.set("token", token);
  if (after) params.set("after", after);
  return `${proto}://${location.host}/api/ws/events?${params.toString()}`;
}
