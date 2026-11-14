/**
 * The phone page's one server-side helper, as a Vite plugin that any config can load (W5's web/vite.config.ts adds it
 * with one line so the captain's demo serves it too; src/mobile/vite.config.ts adds it unless the base already has it).
 */
import { execFile } from "node:child_process";
import fs from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import os from "node:os";
import path from "node:path";
import type { Plugin } from "vite";

const web = path.resolve(__dirname, "../..");

/**
 * GET /api/m/doorscale?path=<upload/… or data/…> → a photo's best-estimate scale from its door swings
 * (src/mobile/doorscale.py, ~2 s, cached per file).
 * GET /api/m/pdfscale?path=<….pdf>&page=N → the scale PRINTED on a vector PDF page (src/mobile/pdfscale.py, ~0.1 s):
 * tap-the-room on a PDF uses it before W5's door-swing estimate (/api/doorscale).
 * Same file layout as W5's middleware (STOA_UPLOADS, STOA_DATA). Runs before W5's /api middleware (enforce: "pre");
 * every other /api request goes on to it unchanged.
 */
export function phoneApi(): Plugin {
  const UPLOADS = process.env.STOA_UPLOADS ?? path.join(os.tmpdir(), "stoa-uploads");
  const DATA = process.env.STOA_DATA ?? "junction-dataset";
  // the same order as W5's middleware (S0 D42: a server started without JUNCTION_VENV fell back to the system python3,
  // which has no stoa): $JUNCTION_VENV, the team venv on this machine, then python3
  const PY = [process.env.JUNCTION_VENV, ".venv"].map((v) => (v ? `${v}/bin/python` : "")).find((py) => py && fs.existsSync(py)) ?? "python3";
  const SCRIPTS = { doorscale: path.join(__dirname, "doorscale.py"), pdfscale: path.join(__dirname, "pdfscale.py") };
  const cache = new Map<string, Promise<unknown>>();
  const absOf = (v: string, ext: RegExp): string | null => {
    const m = v.match(/^upload\/([0-9a-f]{12})\/(?:(cubicasa5k)\/)?([^/\\]+)$/);
    const abs = m ? (m[2] ? path.join(UPLOADS, m[1], "data", m[2], m[3]) : path.join(UPLOADS, m[1], m[3]))
      : /^data\/[^\\]+$/.test(v) && !v.split("/").includes("..") ? path.join(DATA, v) : null;
    return abs && ext.test(abs) && fs.existsSync(abs) ? abs : null;
  };
  const mw = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    const kind = req.url?.match(/^\/api\/m\/(doorscale|pdfscale)\b/)?.[1] as keyof typeof SCRIPTS | undefined;
    if (!kind) return next();
    const q = new URL(req.url!, "http://x").searchParams;
    const abs = absOf(q.get("path") ?? "", kind === "pdfscale" ? /\.pdf$/i : /\.(png|jpe?g)$/i);
    const page = String(Math.max(0, Math.min(999, parseInt(q.get("page") ?? "0", 10) || 0)));
    res.setHeader("Content-Type", "application/json");
    if (!abs) return void res.end(JSON.stringify({ ok: false, why: kind === "pdfscale" ? "not a PDF we have" : "not a photo or image we have" }));
    const key = `${kind}|${abs}|${page}`;
    if (!cache.has(key)) {
      const args = kind === "pdfscale" ? [SCRIPTS.pdfscale, abs, page] : [SCRIPTS.doorscale, abs];
      cache.set(key, new Promise((resolve) => execFile(PY, args, { cwd: path.resolve(web, ".."), timeout: 60_000 }, (err, out) => {
        try {
          resolve(JSON.parse(String(out).trim().split("\n").pop()!));
        } catch {
          cache.delete(key);
          resolve({ ok: false, why: String(err?.message ?? "failed").slice(0, 200) });
        }
      })));
    }
    void cache.get(key)!.then((j) => res.end(JSON.stringify(j)));
  };
  return { name: "w6-phone-api", enforce: "pre", configureServer: (s) => void s.middlewares.use(mw), configurePreviewServer: (s) => void s.middlewares.use(mw) };
}
