// -----------------------------------------------------------------------------
// Module: vite.config.ts
// Role: Vite build and local same-origin Navigator Web Host proxy.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：配置 Vite 构建，以及 Navigator Web Host 的本地同源代理。

import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Keep browser code on Navigator's relative `/api` boundary in development.
 * The target is only a local development convenience; production remains
 * same-origin with the Navigator Web Host.
 * 开发环境中的浏览器代码始终通过 Navigator 的相对 `/api` 边界访问后端。
 * 该目标仅用于本地开发；生产环境仍与 Navigator Web Host 保持同源。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const navigatorHost = env.NAVIGATOR_WEB_HOST_URL || "http://127.0.0.1:8100";

  return {
    plugins: [react()],
    appType: "spa",
    server: {
      proxy: {
        "/api": {
          target: navigatorHost,
          changeOrigin: false,
          secure: false,
        },
      },
    },
    test: {
      environment: "node",
      include: ["src/**/*.test.ts"],
    },
  };
});
