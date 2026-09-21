// -----------------------------------------------------------------------------
// Module: vite.config.ts
// Role: Vite build and local same-origin Navigator Web Host proxy.
// -----------------------------------------------------------------------------

import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Keep browser code on Navigator's relative `/api` boundary in development.
 * The target is only a local development convenience; production remains
 * same-origin with the Navigator Web Host.
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
