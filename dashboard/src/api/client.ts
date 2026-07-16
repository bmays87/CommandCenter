import type { AnswerRequest, Interaction, LaunchRequest, ProdeoEvent, Session } from "./types";

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

/** 409: someone else resolved the interaction first. */
export class ConflictError extends Error {
  constructor(detail: string) {
    super(detail || "conflict");
  }
}

async function parse<T>(resp: Response): Promise<T> {
  if (resp.status === 401) throw new UnauthorizedError();
  if (resp.status === 409) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: string };
    throw new ConflictError(body.detail ?? "");
  }
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as T;
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { headers: authHeaders() }));
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body ?? {}),
    }),
  );
}

export interface SessionListResponse {
  sessions: Session[];
}

export interface EventListResponse {
  events: ProdeoEvent[];
  cursor: string | null;
}

export interface InteractionListResponse {
  interactions: Interaction[];
  pending: number;
}

export const api = {
  health: () => get<{ status: string; version: string; node: string }>("/api/health"),
  sessions: () => get<SessionListResponse>("/api/sessions"),
  session: (id: string) => get<Session>(`/api/sessions/${id}`),
  sessionEvents: (id: string, limit = 500) =>
    get<EventListResponse>(`/api/sessions/${id}/events?limit=${limit}`),
  events: (params: {
    type?: string;
    session?: string;
    before?: string;
    order?: "asc" | "desc";
    limit?: number;
  }) => {
    const search = new URLSearchParams();
    if (params.type) search.set("type", params.type);
    if (params.session) search.set("session", params.session);
    if (params.before) search.set("before", params.before);
    if (params.order) search.set("order", params.order);
    if (params.limit) search.set("limit", String(params.limit));
    return get<EventListResponse>(`/api/events?${search.toString()}`);
  },
  interactions: (params?: { status?: string; session?: string }) => {
    const search = new URLSearchParams();
    if (params?.status) search.set("status", params.status);
    if (params?.session) search.set("session", params.session);
    const qs = search.toString();
    return get<InteractionListResponse>(`/api/interactions${qs ? `?${qs}` : ""}`);
  },
  answerInteraction: (id: string, body: AnswerRequest) =>
    post<Interaction>(`/api/interactions/${id}/answer`, body),
  launchSession: (body: LaunchRequest) => post<Session>("/api/sessions", body),
  terminateSession: (id: string) => post<Session>(`/api/sessions/${id}/terminate`),
  promptSession: (id: string, prompt: string) =>
    post<Session>(`/api/sessions/${id}/prompt`, { prompt }),
};

export function wsUrl(types: string, after?: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ types });
  const token = getToken();
  if (token) params.set("token", token);
  if (after) params.set("after", after);
  return `${proto}://${location.host}/api/ws/events?${params.toString()}`;
}
