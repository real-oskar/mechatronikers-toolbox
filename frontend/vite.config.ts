/// <reference types="vitest/config" />
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { devConfig } from "./dev/devConfig.ts";
import { hubApi } from "./dev/hubApi.ts";
import { mockApi } from "./dev/mockApi.ts";

// Dev server against a running session: MT_GATEWAY=http://127.0.0.1:<port> MT_TOKEN=<token>
// (the review link's ?t=), and the host collector for the run table (MT_DEV_MODE=host); the hub's
// overview runs on fixture rows (MT_DEV_MODE=hub).
const gateway = process.env.MT_GATEWAY;
const token = process.env.MT_TOKEN;
const sandboxProxy = gateway
  ? { target: gateway, headers: token ? { Authorization: `Bearer ${token}` } : {} }
  : undefined;

// MT_DEV_DATA=1: real design data from frontend/.dev-data instead of a running session (dev only).
const devData = process.env.MT_DEV_DATA ? [mockApi(resolve(import.meta.dirname, ".dev-data"))] : [];
// MT_DEV_MODE=hub: the hub's overview with fixture rows (`GET /api/runs`, until the hub exists).
const hub = process.env.MT_DEV_MODE === "hub" ? [hubApi()] : [];

export default defineConfig({
  plugins: [react(), devConfig(process.env.MT_DEV_MODE), ...devData, ...hub],
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1000, // the three.js chunk (~940 kB) is loaded lazily with the 3D view
    // One entry (index.html) for every mode; `/config.json` picks the screen at runtime (D74).
  },
  server: {
    proxy: {
      ...(sandboxProxy ? { "/api": sandboxProxy, "/events": sandboxProxy, "/circuit/api": sandboxProxy } : {}),
      "/sessions": "http://127.0.0.1:8765",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
});
