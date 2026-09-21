// -----------------------------------------------------------------------------
// Module: src/api.ts
// Role: Typed same-origin client for the Navigator Web Host and Product proxies.
// -----------------------------------------------------------------------------

/** A JSON object received from a Product API after boundary validation. */
export type JsonRecord = Record<string, unknown>;

/** The browser-visible session projection returned by Navigator Web Host. */
export interface SessionPayload {
  authenticated: boolean;
  state: "AUTHENTICATED" | "ANONYMOUS";
  sessionId: string | null;
  expiresAt: string | null;
  refreshExpiresAt: string | null;
  refreshable: boolean;
  csrfToken: string | null;
  refreshed: boolean;
}

export interface GpuDeviceStatus {
  name: string;
  totalMib: number;
  usedMib: number;
  utilizationPct: number;
}

export interface GpuStatus {
  available: boolean;
  gpus?: GpuDeviceStatus[];
}

export interface DiskStatus {
  available?: boolean;
  totalGib?: number;
  usedGib?: number;
  freeGib?: number;
  usedPct?: number;
}

export interface ServiceHealthStatus {
  name: string;
  url: string;
  status: "UP" | "DOWN";
  latencyMs: number;
}

export interface BlockerInfo {
  code: string;
  message: string;
}

export interface PluginInfo {
  name: string;
  kind?: string;
  state: string;
}

export interface ActiveRoutePayload {
  gatewayEndpointId: string;
  modelId: string;
  baseUrl: string;
  apiKeyHint?: string;
}

export interface PreviewRow {
  index: number;
  mapped: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface DatasetPreview {
  versionId: string;
  totalRows: number;
  rows: PreviewRow[];
}

export interface DeploymentEvent {
  sequence: number;
  phase: string;
  message: string;
  occurredAt: string;
  failureCode?: string | null;
}

export interface DeploymentEventsResponse {
  deploymentId: string;
  events: DeploymentEvent[];
}

/** Safe host status; it intentionally contains no credential material. */
export interface SystemStatus {
  service: string;
  status: string;
  version: string;
  authenticated: boolean;
  proxyPrefixes: string[];
  credentials: {
    active: number;
    revoked: number;
  };
  gpu?: GpuStatus;
  disk?: DiskStatus;
  services?: ServiceHealthStatus[];
  blockers?: BlockerInfo[];
  plugins?: PluginInfo[];
  observedAt: string;
}

/** Write-only credential metadata returned by the Web Host. */
export interface CredentialMetadata {
  id: string;
  name: string;
  provider: string;
  kind: string;
  state: string;
  credentialRef: string;
  createdAt: string;
  updatedAt: string;
}

/** Gateway API key metadata returned by Exchange. */
export interface ApiKeyMetadata {
  id: string;
  name: string;
  credentialRef: string;
  state: "ACTIVE" | "REVOKED";
  modelScope: string[];
  createdAt: string;
  expiresAt?: string | null;
  revokedAt?: string | null;
  resourceVersion?: number;
}

export interface CreateApiKeyInput {
  name: string;
  expiresAt?: string | null;
  modelScope?: string[];
}

/** Model import command accepted by the Reactor Product API. */
export interface CreateModelImportInput {
  name: string;
  servingBindingId: string;
  source: {
    kind: "HUGGING_FACE" | "LOCAL_PATH";
    repository?: string;
    revision?: string;
    path?: string;
  };
  credentialRef?: string;
  trustRemoteCode: false;
}

/** Dataset creation command accepted by Catalyst. */
export interface CreateDatasetInput {
  name: string;
  description: string;
}

/** A stable set of same-origin proxy prefixes exposed by Navigator. */
export const NAVIGATOR_PROXY_PATHS = {
  catalyst: "/api/v1/catalyst",
  exchange: "/api/v1/exchange",
  navigator: "/api/v1/navigator",
  reactor: "/api/v1/reactor",
  yield: "/api/v1/yield",
} as const;

const AUTH_SESSION_PATH = "/api/v1/auth/session";
const AUTH_REFRESH_PATH = "/api/v1/auth/session/refresh";
const AUTH_PAIR_PATH = "/api/v1/auth/pair";
const AUTH_LOGOUT_PATH = "/api/v1/auth/session";

/** An RFC 9457 or Web Host error raised before a page can render a result. */
export class NavigatorHttpError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly retryable: boolean;

  constructor(
    status: number,
    code: string,
    detail: string,
    retryable: boolean,
  ) {
    super(detail);
    this.name = "NavigatorHttpError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.retryable = retryable;
  }
}

/** Raised when a response does not satisfy the small client-side wire contract. */
export class NavigatorContractError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "NavigatorContractError";
  }
}

type ResponseParser<T> = (value: unknown) => T;
type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/**
 * Own browser session material in memory and route every Product request through
 * Navigator's configured same-origin prefixes. No API origin is accepted from
 * page code and no session token is written to browser storage.
 */
export class NavigatorApi {
  private csrfToken: string | null = null;
  private refreshInFlight: Promise<SessionPayload> | null = null;
  private sessionExpiredHandler: (() => void) | null = null;

  constructor(private readonly fetcher: Fetcher = globalThis.fetch.bind(globalThis)) {}

  /** Register the App-level response for an exhausted Web Host session. */
  setSessionExpiredHandler(handler: (() => void) | null): void {
    this.sessionExpiredHandler = handler;
  }

  /** Read current session state and rotate it when only refresh state remains. */
  async restoreSession(): Promise<SessionPayload> {
    const session = await this.getSession();
    if (!session.authenticated && session.refreshable) {
      try {
        return await this.refreshSession();
      } catch (error) {
        if (error instanceof NavigatorHttpError && error.status === 401) {
          return session;
        }
        throw error;
      }
    }
    return session;
  }

  /** Pair the browser with the one-time code printed by the Web Host launcher. */
  async pair(pairingCode: string): Promise<SessionPayload> {
    const value = pairingCode.trim();
    if (!value) {
      throw new NavigatorContractError("Enter the one-time Navigator pairing code.");
    }
    return this.requestJson(
      AUTH_PAIR_PATH,
      jsonRequest("POST", { pairingCode: value }),
      parseSession,
      false,
    );
  }

  /** Return session state without converting anonymous access into an error. */
  async getSession(): Promise<SessionPayload> {
    return this.requestJson(AUTH_SESSION_PATH, { method: "GET" }, parseSession, false);
  }

  /** Rotate the refresh cookie and its CSRF token, coalescing concurrent calls. */
  async refreshSession(): Promise<SessionPayload> {
    if (this.refreshInFlight) {
      return this.refreshInFlight;
    }

    this.refreshInFlight = this.requestJson(
      AUTH_REFRESH_PATH,
      { method: "POST" },
      parseSession,
      false,
    );
    try {
      return await this.refreshInFlight;
    } finally {
      this.refreshInFlight = null;
    }
  }

  /** Revoke the browser session and clear the in-memory CSRF token. */
  async logout(): Promise<void> {
    await this.requestJson<void>(
      AUTH_LOGOUT_PATH,
      { method: "DELETE" },
      () => undefined,
      false,
    );
    this.csrfToken = null;
  }

  /** Read non-secret Web Host readiness and credential lifecycle counts. */
  async getSystemStatus(): Promise<SystemStatus> {
    return this.requestJson(
      "/api/v1/system/status",
      { method: "GET" },
      parseSystemStatus,
    );
  }

  /** Read write-only credential metadata. */
  async getCredentials(): Promise<CredentialMetadata[]> {
    return this.requestJson(
      "/api/v1/credentials",
      { method: "GET" },
      parseCredentialArray,
    );
  }

  /** Create a credential without ever echoing its secret in the UI response. */
  async createCredential(input: {
    name: string;
    provider: string;
    kind: string;
    secret: string;
  }): Promise<CredentialMetadata> {
    return this.requestJson(
      "/api/v1/credentials",
      jsonRequest("POST", input, true),
      parseCredential,
    );
  }

  /** Revoke one Web Host credential by metadata identifier. */
  async revokeCredential(id: string): Promise<CredentialMetadata> {
    return this.requestJson(
      `/api/v1/credentials/${encodeURIComponent(id)}`,
      { method: "DELETE" },
      parseCredential,
    );
  }

  /** List Reactor-owned model imports through the Navigator proxy. */
  async getModelImports(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/model-imports`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** List serving bindings available to the current Reactor installation. */
  async getServingBindings(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/serving-bindings`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Start a model import with a stable mutation key. */
  async createModelImport(input: CreateModelImportInput): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/model-imports`,
      jsonRequest("POST", input, true),
      parseResource,
    );
  }

  /** List Catalyst-owned dataset containers through the Navigator proxy. */
  async getDatasets(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.catalyst}/datasets`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Create a dataset container; file preparation remains Catalyst-owned. */
  async createDataset(input: CreateDatasetInput): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.catalyst}/datasets`,
      jsonRequest("POST", input, true),
      parseResource,
    );
  }

  /** List Yield-owned training drafts through the Navigator proxy. */
  async getTrainingDrafts(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.yield}/training-drafts`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Start one prepared training draft. */
  async startTrainingDraft(id: string): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.yield}/training-drafts/${encodeURIComponent(id)}/actions/start`,
      jsonRequest("POST", {}, true),
      parseResource,
    );
  }

  /** Read a Yield training run by its owning Product identifier. */
  async getTrainingRun(id: string): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.yield}/training-runs/${encodeURIComponent(id)}`,
      { method: "GET" },
      parseResource,
    );
  }

  /** Read public attempt diagnostics for one training run. */
  async getTrainingRunAttempts(id: string): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.yield}/training-runs/${encodeURIComponent(id)}/attempts`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Request cancellation without pretending that cancellation is synchronous. */
  async cancelTrainingRun(id: string): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.yield}/training-runs/${encodeURIComponent(id)}/actions/cancel`,
      jsonRequest("POST", {}, true),
      parseResource,
    );
  }

  /** List Reactor deployment intent and observed lifecycle projections. */
  async getDeployments(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/deployments`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Stop one deployment through Reactor's explicit lifecycle action. */
  async stopDeployment(id: string): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/deployments/${encodeURIComponent(id)}/actions/stop`,
      jsonRequest("POST", {}, true),
      parseResource,
    );
  }

  /** Read Gateway routes configured on Exchange. */
  async getGatewayRoutes(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/gateway-routes`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Read Gateway endpoints configured on Exchange. */
  async getGatewayEndpoints(): Promise<JsonRecord[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/gateway-endpoints`,
      { method: "GET" },
      parseResourceArray,
    );
  }

  /** Confirm and publish a draft gateway route. */
  async confirmGatewayRoute(routeId: string, resourceVersion: number): Promise<JsonRecord> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/gateway-route-drafts/${encodeURIComponent(routeId)}/actions/confirm`,
      jsonRequest("POST", { resourceVersion }, true),
      parseResource,
    );
  }

  /** List Exchange gateway API keys. */
  async listApiKeys(): Promise<ApiKeyMetadata[]> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/api-keys`,
      { method: "GET" },
      parseApiKeyArray,
    );
  }

  /** Create an Exchange gateway API key; secret is returned exactly once. */
  async createApiKey(
    routeId: string,
    input: CreateApiKeyInput | JsonRecord,
  ): Promise<{ key: ApiKeyMetadata; secret: string }> {
    void routeId;
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/api-keys`,
      jsonRequest("POST", input, true),
      parseCreatedApiKey,
    );
  }

  /** Revoke an Exchange gateway API key. */
  async revokeApiKey(id: string): Promise<ApiKeyMetadata> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.exchange}/api/v1/api-keys/${encodeURIComponent(id)}/actions/revoke`,
      jsonRequest("POST", {}, true),
      parseApiKey,
    );
  }

  /** Read dataset version sample preview through Catalyst proxy. */
  async getDatasetVersionPreview(
    versionId: string,
    limit: number = 10,
    offset: number = 0,
  ): Promise<DatasetPreview> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.catalyst}/dataset-versions/${encodeURIComponent(versionId)}/preview?limit=${limit}&offset=${offset}`,
      { method: "GET" },
      parseDatasetPreview,
    );
  }

  /** Read deployment loading phase events through Reactor proxy. */
  async getDeploymentEvents(deploymentId: string): Promise<DeploymentEventsResponse> {
    return this.requestJson(
      `${NAVIGATOR_PROXY_PATHS.reactor}/deployments/${encodeURIComponent(deploymentId)}/events`,
      { method: "GET" },
      parseDeploymentEventsResponse,
    );
  }

  /** Read active gateway route from Navigator Web Host session. */
  async getActiveRoute(): Promise<ActiveRoutePayload> {
    return this.requestJson(
      "/api/v1/navigator/active-route",
      { method: "GET" },
      parseActiveRoute,
    );
  }

  /** Store active gateway route into Navigator Web Host session. */
  async setActiveRoute(payload: ActiveRoutePayload): Promise<ActiveRoutePayload> {
    return this.requestJson(
      "/api/v1/navigator/active-route",
      jsonRequest("POST", payload, true),
      parseActiveRoute,
    );
  }

  private async requestJson<T>(
    path: string,
    init: RequestInit,
    parser: ResponseParser<T>,
    retryAuth = true,
  ): Promise<T> {
    const response = await this.send(path, init);
    if (response.status === 401 && retryAuth && !path.startsWith("/api/v1/auth/")) {
      try {
        const refreshed = await this.refreshSession();
        if (refreshed.authenticated) {
          return this.requestJson(path, init, parser, false);
        }
      } catch {
        // The original response contains the useful Product/Web Host problem.
      }
      this.sessionExpiredHandler?.();
      this.csrfToken = null;
    }
    return readResponse(response, path, parser, this.acceptSession.bind(this));
  }

  private async send(path: string, init: RequestInit): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const method = (init.method || "GET").toUpperCase();
    const isMutation = method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE";
    if (isMutation && this.csrfToken && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", this.csrfToken);
    }
    if (init.body && typeof init.body === "string" && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return this.fetcher(path, {
      ...init,
      headers,
      credentials: "same-origin",
    });
  }

  private acceptSession(payload: SessionPayload): void {
    if (payload.csrfToken) {
      this.csrfToken = payload.csrfToken;
      return;
    }
    if (!payload.refreshable) {
      this.csrfToken = null;
    } else if (!this.csrfToken) {
      this.csrfToken = readCookie("cyrene_csrf");
    }
  }
}

/** Create a JSON request and an idempotency key for a Product mutation. */
function jsonRequest(method: string, body: unknown, idempotent = false): RequestInit {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (idempotent) {
    headers["Idempotency-Key"] = makeIdempotencyKey();
  }
  return { method, headers, body: JSON.stringify(body) };
}

function makeIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `navigator-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function readResponse<T>(
  response: Response,
  path: string,
  parser: ResponseParser<T>,
  acceptSession: (payload: SessionPayload) => void,
): Promise<T> {
  const text = await response.text();
  const body: unknown = text ? parseJson(text, path) : undefined;
  if (!response.ok) {
    throw toHttpError(response.status, body);
  }
  const parsed = parser(body);
  if (isSessionPayload(parsed)) {
    acceptSession(parsed);
  }
  return parsed;
}

function parseJson(text: string, path: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new NavigatorContractError(`Navigator returned non-JSON data for ${path}.`);
  }
}

function toHttpError(status: number, body: unknown): NavigatorHttpError {
  const record = isRecord(body) ? body : {};
  return new NavigatorHttpError(
    status,
    readString(record, "code", `HTTP_${status}`),
    readString(record, "detail", `Navigator request failed with HTTP ${status}.`),
    readBoolean(record, "retryable", status >= 500),
  );
}

function parseSession(value: unknown): SessionPayload {
  const record = requireRecord(value, "auth session");
  const state = readString(record, "state", "");
  if (state !== "AUTHENTICATED" && state !== "ANONYMOUS") {
    throw new NavigatorContractError("Navigator returned an unknown session state.");
  }
  return {
    authenticated: requireBoolean(record, "authenticated", "auth session"),
    state,
    sessionId: readNullableString(record, "sessionId"),
    expiresAt: readNullableString(record, "expiresAt"),
    refreshExpiresAt: readNullableString(record, "refreshExpiresAt"),
    refreshable: requireBoolean(record, "refreshable", "auth session"),
    csrfToken: readNullableString(record, "csrfToken"),
    refreshed: requireBoolean(record, "refreshed", "auth session"),
  };
}

function parseSystemStatus(value: unknown): SystemStatus {
  const record = requireRecord(value, "system status");
  const credentials = requireRecord(record.credentials, "system status credentials");
  const prefixes = record.proxyPrefixes;
  if (!Array.isArray(prefixes) || !prefixes.every((prefix) => typeof prefix === "string")) {
    throw new NavigatorContractError("Navigator returned invalid proxy prefix metadata.");
  }

  let gpu: GpuStatus | undefined;
  if (record.gpu && typeof record.gpu === "object") {
    const gpuRec = record.gpu as Record<string, unknown>;
    const gpus = Array.isArray(gpuRec.gpus)
      ? gpuRec.gpus.map((g) => {
          const gr = requireRecord(g, "gpu device");
          return {
            name: requireString(gr, "name", "gpu name"),
            totalMib: requireNumber(gr, "totalMib", "gpu totalMib"),
            usedMib: requireNumber(gr, "usedMib", "gpu usedMib"),
            utilizationPct: requireNumber(gr, "utilizationPct", "gpu utilizationPct"),
          };
        })
      : undefined;
    gpu = {
      available: Boolean(gpuRec.available),
      gpus,
    };
  }

  let disk: DiskStatus | undefined;
  if (record.disk && typeof record.disk === "object") {
    const diskRec = record.disk as Record<string, unknown>;
    disk = {
      available: diskRec.available !== undefined ? Boolean(diskRec.available) : undefined,
      totalGib: typeof diskRec.totalGib === "number" ? diskRec.totalGib : undefined,
      usedGib: typeof diskRec.usedGib === "number" ? diskRec.usedGib : undefined,
      freeGib: typeof diskRec.freeGib === "number" ? diskRec.freeGib : undefined,
      usedPct: typeof diskRec.usedPct === "number" ? diskRec.usedPct : undefined,
    };
  }

  let services: ServiceHealthStatus[] | undefined;
  if (Array.isArray(record.services)) {
    services = record.services.map((s) => {
      const sr = requireRecord(s, "service health");
      return {
        name: requireString(sr, "name", "service name"),
        url: requireString(sr, "url", "service url"),
        status: sr.status === "UP" ? ("UP" as const) : ("DOWN" as const),
        latencyMs: requireNumber(sr, "latencyMs", "service latencyMs"),
      };
    });
  }

  let blockers: BlockerInfo[] | undefined;
  if (Array.isArray(record.blockers)) {
    blockers = record.blockers.map((b) => {
      const br = requireRecord(b, "blocker");
      return {
        code: requireString(br, "code", "blocker code"),
        message: requireString(br, "message", "blocker message"),
      };
    });
  }

  let plugins: PluginInfo[] | undefined;
  if (Array.isArray(record.plugins)) {
    plugins = record.plugins.map((p) => {
      const pr = requireRecord(p, "plugin");
      return {
        name: requireString(pr, "name", "plugin name"),
        kind: typeof pr.kind === "string" ? pr.kind : undefined,
        state: requireString(pr, "state", "plugin state"),
      };
    });
  }

  return {
    service: requireString(record, "service", "system status"),
    status: requireString(record, "status", "system status"),
    version: requireString(record, "version", "system status"),
    authenticated: requireBoolean(record, "authenticated", "system status"),
    proxyPrefixes: prefixes,
    credentials: {
      active: requireNumber(credentials, "active", "credential counts"),
      revoked: requireNumber(credentials, "revoked", "credential counts"),
    },
    gpu,
    disk,
    services,
    blockers,
    plugins,
    observedAt: requireString(record, "observedAt", "system status"),
  };
}

function parseDatasetPreview(value: unknown): DatasetPreview {
  const record = requireRecord(value, "dataset preview");
  const versionId = String(record.versionId ?? record.version_id ?? "");
  const totalRows = typeof record.totalRows === "number" ? record.totalRows : typeof record.total_rows === "number" ? record.total_rows : 0;
  const rows = requireArray(record.rows, "preview rows").map((row, index) => {
    const r = requireRecord(row, "preview row");
    return {
      index: typeof r.index === "number" ? r.index : index,
      mapped: isRecord(r.mapped) ? r.mapped : {},
      raw: isRecord(r.raw) ? r.raw : {},
    };
  });
  return { versionId, totalRows, rows };
}

function parseDeploymentEventsResponse(value: unknown): DeploymentEventsResponse {
  const record = requireRecord(value, "deployment events response");
  const deploymentId = String(record.deploymentId ?? record.deployment_id ?? "");
  const events = requireArray(record.events, "deployment events").map((evt) => {
    const e = requireRecord(evt, "deployment event");
    return {
      sequence: typeof e.sequence === "number" ? e.sequence : 0,
      phase: requireString(e, "phase", "event phase"),
      message: requireString(e, "message", "event message"),
      occurredAt: String(e.occurredAt ?? e.occurred_at ?? ""),
      failureCode: typeof (e.failureCode ?? e.failure_code) === "string" ? String(e.failureCode ?? e.failure_code) : null,
    };
  });
  return { deploymentId, events };
}

function parseActiveRoute(value: unknown): ActiveRoutePayload {
  const record = requireRecord(value, "active route");
  return {
    gatewayEndpointId: String(record.gatewayEndpointId ?? record.gateway_endpoint_id ?? ""),
    modelId: String(record.modelId ?? record.model_id ?? ""),
    baseUrl: String(record.baseUrl ?? record.base_url ?? ""),
    apiKeyHint: typeof (record.apiKeyHint ?? record.api_key_hint) === "string" ? String(record.apiKeyHint ?? record.api_key_hint) : undefined,
  };
}

function parseApiKey(value: unknown): ApiKeyMetadata {
  const record = requireRecord(value, "api key");
  const rawState = requireString(record, "state", "api key state");
  const state: "ACTIVE" | "REVOKED" = rawState === "REVOKED" ? "REVOKED" : "ACTIVE";
  const modelScopeRaw = record.modelScope ?? record.model_scope;
  const modelScope = Array.isArray(modelScopeRaw)
    ? modelScopeRaw.map((item) => String(item))
    : [];

  return {
    id: requireString(record, "id", "api key id"),
    name: requireString(record, "name", "api key name"),
    credentialRef: String(record.credentialRef ?? record.credential_ref ?? ""),
    state,
    modelScope,
    createdAt: String(record.createdAt ?? record.created_at ?? ""),
    expiresAt: readNullableString(record, "expiresAt") ?? readNullableString(record, "expires_at"),
    revokedAt: readNullableString(record, "revokedAt") ?? readNullableString(record, "revoked_at"),
    resourceVersion: typeof record.resourceVersion === "number" ? record.resourceVersion : undefined,
  };
}

function parseApiKeyArray(value: unknown): ApiKeyMetadata[] {
  return requireArray(value, "api keys").map((item) => parseApiKey(item));
}

function parseCreatedApiKey(value: unknown): { key: ApiKeyMetadata; secret: string } {
  const record = requireRecord(value, "created api key");
  const key = parseApiKey(record);
  const secret = requireString(record, "secret", "api key secret");
  return { key, secret };
}

function parseCredentialArray(value: unknown): CredentialMetadata[] {
  return requireArray(value, "credentials").map((item) => parseCredential(item));
}

function parseCredential(value: unknown): CredentialMetadata {
  const record = requireRecord(value, "credential metadata");
  return {
    id: requireString(record, "id", "credential metadata"),
    name: requireString(record, "name", "credential metadata"),
    provider: requireString(record, "provider", "credential metadata"),
    kind: requireString(record, "kind", "credential metadata"),
    state: requireString(record, "state", "credential metadata"),
    credentialRef: requireString(record, "credentialRef", "credential metadata"),
    createdAt: requireString(record, "createdAt", "credential metadata"),
    updatedAt: requireString(record, "updatedAt", "credential metadata"),
  };
}

function parseResourceArray(value: unknown): JsonRecord[] {
  return requireArray(value, "Product resource list").map((item) =>
    requireRecord(item, "Product resource"),
  );
}

function parseResource(value: unknown): JsonRecord {
  return requireRecord(value, "Product resource");
}

function requireRecord(value: unknown, label: string): JsonRecord {
  if (!isRecord(value)) {
    throw new NavigatorContractError(`Navigator returned an invalid ${label}.`);
  }
  return value;
}

function requireArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new NavigatorContractError(`Navigator returned an invalid ${label}.`);
  }
  return value;
}

function requireString(record: JsonRecord, key: string, label: string): string {
  const value = record[key];
  if (typeof value !== "string") {
    throw new NavigatorContractError(`Navigator returned an invalid ${key} in ${label}.`);
  }
  return value;
}

function requireNumber(record: JsonRecord, key: string, label: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new NavigatorContractError(`Navigator returned an invalid ${key} in ${label}.`);
  }
  return value;
}

function requireBoolean(record: JsonRecord, key: string, label: string): boolean {
  const value = record[key];
  if (typeof value !== "boolean") {
    throw new NavigatorContractError(`Navigator returned an invalid ${key} in ${label}.`);
  }
  return value;
}

function readString(record: JsonRecord, key: string, fallback: string): string {
  return typeof record[key] === "string" ? record[key] : fallback;
}

function readBoolean(record: JsonRecord, key: string, fallback: boolean): boolean {
  return typeof record[key] === "boolean" ? record[key] : fallback;
}

function readNullableString(record: JsonRecord, key: string): string | null {
  return typeof record[key] === "string" ? record[key] : null;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSessionPayload(value: unknown): value is SessionPayload {
  return isRecord(value) && (value.state === "AUTHENTICATED" || value.state === "ANONYMOUS");
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const prefix = `${name}=`;
  const cookie = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null;
}
