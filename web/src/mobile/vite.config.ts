/**
 * W6's own Vite config for the phone page (web/mobile.html): W5's web/vite.config.ts (the same /api middleware and
 * settings) plus (1) mobile.html as a second build entry and (2) the tailnet host name, so a phone can open the page
 * through `tailscale serve` (Vite blocks unknown Host headers). web/vite.config.ts is not changed.
 *   npx vite --config src/mobile/vite.config.ts --port 8131            (dev, hot reload)
 *   npx vite build --config src/mobile/vite.config.ts --outDir <dir>   (stable build: index.html + mobile.html)
 */
import { execFile } from "node:child_process";
import fs from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import os from "node:os";
import path from "node:path";
import { defineConfig, mergeConfig, type Plugin } from "vite";
import base from "../../vite.config";

const web = path.resolve(__dirname, "../..");
const hosts = [".ts.net", "localhost", "127.0.0.1"];
// node_modules may be a symlink (to W5's checkout): let the dev server serve files from its real folder too (fonts)
const nm = fs.existsSync(path.join(web, "node_modules")) ? fs.realpathSync(path.join(web, "node_modules")) : path.join(web, "node_modules");

/**
 * GET /api/m/doorscale?path=<upload/… or data/…> → a photo's best-estimate scale from its door swings
 * (src/mobile/doorscale.py, ~2 s, cached per file). Same file layout as W5's middleware (STOA_UPLOADS, STOA_DATA).
 * Runs before W5's /api middleware (enforce: "pre"); every other /api request goes on to it unchanged.
 */
function phoneApi(): Plugin {
  const UPLOADS = process.env.STOA_UPLOADS ?? path.join(os.tmpdir(), "stoa-uploads");
  const DATA = process.env.STOA_DATA ?? "junction-dataset";
  const PY = process.env.JUNCTION_VENV ? `${process.env.JUNCTION_VENV}/bin/python` : "python3";
  const SCRIPT = path.join(__dirname, "doorscale.py");
  const cache = new Map<string, Promise<unknown>>();
  const absOf = (v: string): string | null => {
    const m = v.match(/^upload\/([0-9a-f]{12})\/(?:(cubicasa5k)\/)?([^/\\]+)$/);
    const abs = m ? (m[2] ? path.join(UPLOADS, m[1], "data", m[2], m[3]) : path.join(UPLOADS, m[1], m[3]))
      : /^data\/[^\\]+$/.test(v) && !v.split("/").includes("..") ? path.join(DATA, v) : null;
    return abs && /\.(png|jpe?g)$/i.test(abs) && fs.existsSync(abs) ? abs : null;
  };
  const mw = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (!req.url?.startsWith("/api/m/doorscale")) return next();
    const abs = absOf(new URL(req.url, "http://x").searchParams.get("path") ?? "");
    res.setHeader("Content-Type", "application/json");
    if (!abs) return void res.end(JSON.stringify({ ok: false, why: "not a photo or image we have" }));
    if (!cache.has(abs)) {
      cache.set(abs, new Promise((resolve) => execFile(PY, [SCRIPT, abs], { cwd: path.resolve(web, ".."), timeout: 60_000 }, (err, out) => {
        try {
          resolve(JSON.parse(String(out).trim().split("\n").pop()!));
        } catch {
          cache.delete(abs);
          resolve({ ok: false, why: String(err?.message ?? "failed").slice(0, 200) });
        }
      })));
    }
    void cache.get(abs)!.then((j) => res.end(JSON.stringify(j)));
  };
  return { name: "w6-phone-api", enforce: "pre", configureServer: (s) => void s.middlewares.use(mw), configurePreviewServer: (s) => void s.middlewares.use(mw) };
}

export default mergeConfig(base, defineConfig({
  plugins: [phoneApi()],
  root: web,
  // node_modules is often a symlink to W5's checkout: keep this server's pre-bundled deps out of W5's cache
  cacheDir: path.join(os.tmpdir(), "vite-cache-w6"),
  server: { allowedHosts: hosts, fs: { allow: [web, nm] } },
  preview: { allowedHosts: hosts },
  build: { rollupOptions: { input: { main: path.join(web, "index.html"), mobile: path.join(web, "mobile.html") } } },
}));
