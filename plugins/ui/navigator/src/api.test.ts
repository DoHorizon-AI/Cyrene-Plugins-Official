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
});
