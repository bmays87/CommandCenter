import type {
  AnswerRequest,
  AppStatus,
  AssetResult,
  AssetStatus,
  ContextUsage,
  DirectoryListing,
  EnvironmentReport,
  HealthResponse,
  RestartResponse,
  Catalog,
  ExtensionConfig,
  ExtensionDetail,
  ExtensionSettings,
  ExtensionSummary,
  InstallResult,
  Interaction,
  LaunchRequest,
  ProdeoEvent,
  Session,
} from "./types";

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

async function put<T>(path: string, body?: unknown): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body ?? {}),
    }),
  );
}

async function del<T>(path: string): Promise<T> {
  return parse<T>(
    await fetch(path, { method: "DELETE", headers: authHeaders() }),
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

export interface ExtensionListResponse {
  extensions: ExtensionSummary[];
}

export interface AppListResponse {
  apps: AppStatus[];
}

export interface AssetListResponse {
  assets: AssetStatus[];
}

export const api = {
  health: () => get<HealthResponse>("/api/health"),
  restartServer: () => post<RestartResponse>("/api/system/restart"),
  browse: (path: string) =>
    get<DirectoryListing>(`/api/system/browse?path=${encodeURIComponent(path)}`),
  openEditor: (path: string) =>
    post<{ opened: boolean; path: string }>("/api/system/open-editor", { path }),
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
    return get<InteractionListResponse>(
      `/api/interactions${qs ? `?${qs}` : ""}`,
    );
  },
  answerInteraction: (id: string, body: AnswerRequest) =>
    post<Interaction>(`/api/interactions/${id}/answer`, body),
  launchSession: (body: LaunchRequest) => post<Session>("/api/sessions", body),
  terminateSession: (id: string) =>
    post<Session>(`/api/sessions/${id}/terminate`),
  interruptSession: (id: string) => post<Session>(`/api/sessions/${id}/interrupt`),
  archiveSession: (id: string) => post<Session>(`/api/sessions/${id}/archive`),
  sessionContext: (id: string) => get<ContextUsage>(`/api/sessions/${id}/context`),
  promptSession: (id: string, prompt: string) =>
    post<Session>(`/api/sessions/${id}/prompt`, { prompt }),
  setSessionModel: (id: string, model: string) =>
    post<Session>(`/api/sessions/${id}/model`, { model }),
  setSessionPermissionMode: (id: string, mode: string) =>
    post<Session>(`/api/sessions/${id}/permission-mode`, { mode }),
  extensions: () => get<ExtensionListResponse>("/api/extensions"),
  extension: (name: string) => get<ExtensionDetail>(`/api/extensions/${name}`),
  extensionCatalog: () => get<Catalog>("/api/extensions/catalog"),
  extensionConfig: (name: string) =>
    get<ExtensionConfig>(`/api/extensions/${name}/config`),
  saveExtensionConfig: (name: string, values: Record<string, unknown>) =>
    put<ExtensionConfig>(`/api/extensions/${name}/config`, { values }),
  installExtension: (name: string) =>
    post<InstallResult>(`/api/extensions/${name}/install`),
  uninstallExtension: (name: string) =>
    del<InstallResult>(`/api/extensions/${name}/install`),
  setExtensionEnabled: (name: string, enabled: boolean) =>
    put<ExtensionSummary>(`/api/extensions/${name}/enabled`, { enabled }),
  extensionSettings: () => get<ExtensionSettings>("/api/extension-settings"),
  saveExtensionSettings: (settings: ExtensionSettings) =>
    put<ExtensionSettings>("/api/extension-settings", settings),
  extensionAssets: (name: string) =>
    get<AssetListResponse>(`/api/extensions/${name}/assets`),
  downloadAsset: (name: string, assetId: string) =>
    post<AssetResult>(`/api/extensions/${name}/assets/${assetId}`),
  environment: () => get<EnvironmentReport>("/api/system/environment"),
  apps: () => get<AppListResponse>("/api/apps"),
  startApp: (name: string) => post<AppStatus>(`/api/apps/${name}/start`),
  stopApp: (name: string) => post<AppStatus>(`/api/apps/${name}/stop`),
  restartApp: (name: string) => post<AppStatus>(`/api/apps/${name}/restart`),
  setAppAutostart: (name: string, autostart: boolean) =>
    put<AppStatus>(`/api/apps/${name}/autostart`, { autostart }),
};

export function wsUrl(types: string, after?: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ types });
  const token = getToken();
  if (token) params.set("token", token);
  if (after) params.set("after", after);
  return `${proto}://${location.host}/api/ws/events?${params.toString()}`;
}
