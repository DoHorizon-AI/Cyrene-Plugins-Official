// -----------------------------------------------------------------------------
// Module: src/router.test.ts
// Role: Route resolution tests for the Navigator console navigation rail.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：测试 Navigator 控制台导航栏的路由解析。

import { pathForRoute, routeForPath } from "./router";
import { describe, expect, it } from "vitest";

describe("Navigator routes", () => {
  it("resolves all page paths", () => {
    expect(routeForPath("/")).toBe("overview");
    expect(routeForPath("/models")).toBe("models");
    expect(routeForPath("/datasets/")).toBe("datasets");
    expect(routeForPath("/training")).toBe("training");
    expect(routeForPath("/runs?runId=run-1")).toBe("runs");
    expect(routeForPath("/deployments")).toBe("deployments");
    expect(routeForPath("/gateway")).toBe("gateway");
    expect(routeForPath("/chat")).toBe("chat");
    expect(routeForPath("/settings")).toBe("settings");
  });

  it("keeps unknown paths on the safe overview surface", () => {
    expect(routeForPath("/not-a-page")).toBe("overview");
    expect(routeForPath("/runs/run-1")).toBe("runs");
  });

  it("returns canonical paths for route ids", () => {
    expect(pathForRoute("overview")).toBe("/");
    expect(pathForRoute("models")).toBe("/models");
    expect(pathForRoute("deployments")).toBe("/deployments");
    expect(pathForRoute("gateway")).toBe("/gateway");
    expect(pathForRoute("chat")).toBe("/chat");
    expect(pathForRoute("settings")).toBe("/settings");
  });
});
