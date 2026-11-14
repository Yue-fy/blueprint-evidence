import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import { stoaApi } from "./mock/stoaApi";
// W6 17:27: the phone page's door-swing scale (GET /api/m/doorscale only; everything else falls through to stoaApi)
import { phoneApi } from "./src/mobile/phoneapi";

// W6's phone page (web/mobile.html, captain 16:00) is built next to the main page whenever it exists
const pages: Record<string, string> = { main: path.resolve(__dirname, "index.html") };
if (fs.existsSync(path.resolve(__dirname, "mobile.html"))) pages.mobile = path.resolve(__dirname, "mobile.html");

// `/api/*` → W4's API on :8100 when it is up, else the local mock (see mock/stoaApi.ts).
export default defineConfig({
  plugins: [phoneApi(), stoaApi()],
  // '.ts.net': the captain may open the demo from a phone through `tailscale serve` (Vite blocks unknown hosts, W6 16:28)
  server: { host: "127.0.0.1", port: 5173, allowedHosts: [".ts.net"] },
  preview: { host: "127.0.0.1", port: 5173, allowedHosts: [".ts.net"] },
  // the landing page links to the phone page only when this build has it
  define: { __HAS_MOBILE__: JSON.stringify(!!pages.mobile) },
  optimizeDeps: { exclude: ["web-ifc"] },
  build: { target: "es2022", chunkSizeWarningLimit: 4000, rollupOptions: { input: pages } },
});
