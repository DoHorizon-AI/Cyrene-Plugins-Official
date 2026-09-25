// -----------------------------------------------------------------------------
// Module: src/router.ts
// Role: Minimal history router for the seven Navigator console surfaces.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：为 Navigator 的七个控制台页面提供轻量级 history 路由。

/**
 * The pages intentionally exposed by the Navigator WebUI navigation rail.
 * Navigator WebUI 导航栏有意公开的页面集合。
 */
export type RouteId =
  | "overview"
  | "models"
  | "datasets"
  | "training"
  | "runs"
  | "deployments"
  | "gateway"
  | "settings"
  | "chat";

/**
 * Navigation metadata used by both the shell and the route resolver.
 * 供外壳与路由解析器共同使用的导航元数据。
 */
export interface RouteDefinition {
  id: RouteId;
  label: string;
  path: string;
  description: string;
}

export const ROUTES: readonly RouteDefinition[] = [
  {
    id: "overview",
    label: "Overview",
    path: "/",
    description: "Workspace pulse and service reachability",
  },
  {
    id: "models",
    label: "Models",
    path: "/models",
    description: "Imports, validation, and artifacts",
  },
  {
    id: "datasets",
    label: "Datasets",
    path: "/datasets",
    description: "Containers and preparation handoffs",
  },
  {
    id: "training",
    label: "Training",
    path: "/training",
    description: "Drafts, preflight, and launch intent",
  },
  {
    id: "runs",
    label: "Runs",
    path: "/runs",
    description: "Run detail and attempt diagnostics",
  },
  {
    id: "deployments",
    label: "Deployments",
    path: "/deployments",
    description: "Serving lifecycle and readiness",
  },
  {
    id: "gateway",
    label: "Gateway",
    path: "/gateway",
    description: "Routes, API keys, and client configuration",
  },
  {
    id: "chat",
    label: "Chat",
    path: "/chat",
    description: "Exchange model testing with active route",
  },
  {
    id: "settings",
    label: "Settings",
    path: "/settings",
    description: "Workspace session and credentials",
  },
];

/**
 * Resolve a browser path without introducing a second routing dependency.
 * 在不引入第二套路由依赖的前提下解析浏览器路径。
 */
export function routeForPath(pathname: string): RouteId {
  const normalized = normalizePath(pathname);
  const match = ROUTES.find((route) => route.path === normalized);
  if (match) {
    return match.id;
  }
  if (normalized.startsWith("/runs/")) {
    return "runs";
  }
  return "overview";
}

/**
 * Return the canonical path for a route identifier.
 * 返回路由标识对应的规范路径。
 */
export function pathForRoute(route: RouteId): string {
  return ROUTES.find((definition) => definition.id === route)?.path ?? "/";
}

/**
 * Push a route without reloading the WebUI bundle.
 * 切换到指定路由，但不重新加载 WebUI bundle。
 */
export function pushRoute(route: RouteId): void {
  window.history.pushState({}, "", pathForRoute(route));
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function normalizePath(pathname: string): string {
  const pathOnly = pathname.split(/[?#]/, 1)[0] ?? "/";
  const withoutTrailingSlash = pathOnly.replace(/\/+$/, "");
  return withoutTrailingSlash || "/";
}
