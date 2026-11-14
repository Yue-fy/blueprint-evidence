import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import { stoaApi } from "./mock/stoaApi";

// W6's phone page (web/mobile.html, captain 16:00) is built next to the main page whenever it exists
const pages: Record<string, string> = { main: path.resolve(__dirname, "index.html") };
if (fs.existsSync(path.resolve(__dirname, "mobile.html"))) pages.mobile = path.resolve(__dirname, "mobile.html");

// `/api/*` → W4's API on :8100 when it is up, else the local mock (see mock/stoaApi.ts).
export default defineConfig({
  plugins: [stoaApi()],
  server: { host: "127.0.0.1", port: 5173 },
  preview: { host: "127.0.0.1", port: 5173 },
  optimizeDeps: { exclude: ["web-ifc"] },
  build: { target: "es2022", chunkSizeWarningLimit: 4000, rollupOptions: { input: pages } },
});
