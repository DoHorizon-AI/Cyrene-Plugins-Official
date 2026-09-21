// -----------------------------------------------------------------------------
// Module: src/api.test.ts
// Role: Session rotation and same-origin Product proxy client tests.
// -----------------------------------------------------------------------------

import { NavigatorApi } from "./api";
import { describe, expect, it } from "vitest";

function response(body: unknown, status = 200): Response {
  return new Response(body === undefined ? undefined : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("NavigatorApi", () => {
  it("refreshes once after an expired access session and retries the Product read", async () => {
    const calls: Array<{ path: string; init: RequestInit }> = [];
    let modelRead = 0;
    const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const path = String(input);
      calls.push({ path, init: init ?? {} });
      if (path === "/api/v1/auth/session") {
        return response({
          authenticated: true,
          state: "AUTHENTICATED",
          sessionId: "session-1",
          expiresAt: "2030-01-01T00:00:00Z",
          refreshExpiresAt: "2030-01-02T00:00:00Z",
          refreshable: true,
          csrfToken: "csrf-old",
          refreshed: false,
        });
      }
      if (path === "/api/v1/reactor/model-imports" && modelRead === 0) {
        modelRead += 1;
        return response({ code: "NAVIGATOR_AUTH_REQUIRED", detail: "expired", retryable: false }, 401);
      }
      if (path === "/api/v1/auth/session/refresh") {
        return response({
          authenticated: true,
          state: "AUTHENTICATED",
          sessionId: "session-1",
          expiresAt: "2030-01-01T01:00:00Z",
          refreshExpiresAt: "2030-01-02T00:00:00Z",
          refreshable: true,
          csrfToken: "csrf-new",
          refreshed: true,
        });
      }
      if (path === "/api/v1/reactor/model-imports") {
        return response([]);
      }
      throw new Error(`Unexpected request: ${path}`);
    };

    const api = new NavigatorApi(fetcher);
    await api.getSession();
    expect(await api.getModelImports()).toEqual([]);
    expect(calls.map((call) => call.path)).toEqual([
      "/api/v1/auth/session",
      "/api/v1/reactor/model-imports",
      "/api/v1/auth/session/refresh",
      "/api/v1/reactor/model-imports",
    ]);
    const refreshCall = calls[2];
    const retryCall = calls[3];
    expect(new Headers(refreshCall?.init.headers).get("X-CSRF-Token")).toBe("csrf-old");
    expect(new Headers(retryCall?.init.headers).get("X-CSRF-Token")).toBeNull();
    expect(retryCall?.init.credentials).toBe("same-origin");
  });

  it("pairs through the Web Host auth path and does not use a Product origin", async () => {
    let request: { path: string; init: RequestInit } | undefined;
    const api = new NavigatorApi(async (input, init) => {
      request = { path: String(input), init: init ?? {} };
      return response({
        authenticated: true,
        state: "AUTHENTICATED",
        sessionId: "session-1",
        expiresAt: "2030-01-01T00:00:00Z",
        refreshExpiresAt: "2030-01-02T00:00:00Z",
        refreshable: true,
        csrfToken: "csrf",
        refreshed: false,
      });
    });

    await api.pair("one-time-code");
    expect(request?.path).toBe("/api/v1/auth/pair");
    expect(request?.init.credentials).toBe("same-origin");
    expect(JSON.parse(String(request?.init.body)) as unknown).toEqual({ pairingCode: "one-time-code" });
  });

  it("adds the rotating CSRF token and idempotency key to mutations", async () => {
    const calls: Array<{ path: string; init: RequestInit }> = [];
    const api = new NavigatorApi(async (input, init) => {
      calls.push({ path: String(input), init: init ?? {} });
      if (String(input) === "/api/v1/auth/session") {
        return response({
          authenticated: true,
          state: "AUTHENTICATED",
          sessionId: "session-1",
          expiresAt: "2030-01-01T00:00:00Z",
          refreshExpiresAt: "2030-01-02T00:00:00Z",
          refreshable: true,
          csrfToken: "csrf-token",
          refreshed: false,
        });
      }
      return response({ id: "dataset-1", name: "dataset" });
    });

    await api.getSession();
    await api.createDataset({ name: "dataset", description: "" });
    const mutation = calls[1];
    const headers = new Headers(mutation?.init.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(mutation?.path).toBe("/api/v1/catalyst/datasets");
  });

  it("parses system status including GPU, disk, and service reachability", async () => {
    const api = new NavigatorApi(async () => {
      return response({
        service: "cyrene-navigator-web-host",
        status: "ok",
        version: "1.0.0",
        authenticated: true,
        proxyPrefixes: ["/api/v1/yield", "/api/v1/exchange"],
        credentials: { active: 2, revoked: 1 },
        gpu: {
          available: true,
          gpus: [{ name: "RTX 5070", totalMib: 12227, usedMib: 4200, utilizationPct: 15 }],
        },
        disk: {
          available: true,
          totalGib: 1000,
          usedGib: 250,
          freeGib: 750,
          usedPct: 25,
        },
        services: [
          { name: "yield", url: "http://127.0.0.1:8001/health", status: "UP", latencyMs: 2.5 },
        ],
        observedAt: "2026-09-21T00:00:00Z",
      });
    });

    const status = await api.getSystemStatus();
    expect(status.status).toBe("ok");
    expect(status.gpu?.available).toBe(true);
    expect(status.gpu?.gpus?.[0]?.name).toBe("RTX 5070");
    expect(status.disk?.totalGib).toBe(1000);
    expect(status.services?.[0]?.status).toBe("UP");
  });

  it("handles Gateway API routes, endpoints, and API key management", async () => {
    const calls: Array<{ path: string; method?: string }> = [];
    const api = new NavigatorApi(async (input, init) => {
      const path = String(input);
      calls.push({ path, method: init?.method });
      if (path === "/api/v1/auth/session") {
        return response({
          authenticated: true,
          state: "AUTHENTICATED",
          sessionId: "session-1",
          expiresAt: "2030-01-01T00:00:00Z",
          refreshExpiresAt: "2030-01-02T00:00:00Z",
          refreshable: true,
          csrfToken: "csrf-token",
          refreshed: false,
        });
      }
      if (path === "/api/v1/exchange/api/v1/gateway-routes") {
        return response([
          { id: "route-1", modelPattern: "llama-3-8b", targetBindingId: "local-gpu", state: "ACTIVE" },
        ]);
      }
      if (path === "/api/v1/exchange/api/v1/api-keys") {
        if (init?.method === "POST") {
          return response({
            id: "key-1",
            name: "test-key",
            credentialRef: "api-key://uuid-1",
            state: "ACTIVE",
            modelScope: ["llama-3-8b"],
            createdAt: "2026-09-21T00:00:00Z",
            secret: "cyk_live_test_secret_12345",
          });
        }
        return response([
          {
            id: "key-1",
            name: "test-key",
            credentialRef: "api-key://uuid-1",
            state: "ACTIVE",
            modelScope: ["llama-3-8b"],
            createdAt: "2026-09-21T00:00:00Z",
          },
        ]);
      }
      if (path === "/api/v1/exchange/api/v1/api-keys/key-1/actions/revoke") {
        return response({
          id: "key-1",
          name: "test-key",
          credentialRef: "api-key://uuid-1",
          state: "REVOKED",
          modelScope: ["llama-3-8b"],
          createdAt: "2026-09-21T00:00:00Z",
        });
      }
      throw new Error(`Unexpected path: ${path}`);
    });

    await api.getSession();
    const routes = await api.getGatewayRoutes();
    expect(routes).toHaveLength(1);
    expect(routes[0]?.["modelPattern"]).toBe("llama-3-8b");

    const created = await api.createApiKey("route-1", { name: "test-key" });
    expect(created.secret).toBe("cyk_live_test_secret_12345");
    expect(created.key.name).toBe("test-key");

    const keys = await api.listApiKeys();
    expect(keys).toHaveLength(1);
    expect(keys[0]?.state).toBe("ACTIVE");

    const revoked = await api.revokeApiKey("key-1");
    expect(revoked.state).toBe("REVOKED");
  });
});

