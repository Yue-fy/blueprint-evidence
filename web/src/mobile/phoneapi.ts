/**
 * The phone page's one server-side helper, as a Vite plugin that any config can load (W5's web/vite.config.ts adds it
 * with one line so the captain's demo serves it too; src/mobile/vite.config.ts adds it unless the base already has it).
 */
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import http, { type IncomingMessage, type ServerResponse } from "node:http";
import os from "node:os";
import path from "node:path";
import type { Plugin } from "vite";

const web = path.resolve(__dirname, "../..");

/**
 * GET /api/m/doorscale?path=<upload/… or data/…> → a photo's best-estimate scale from its door swings
 * (src/mobile/doorscale.py, ~2 s, cached per file).
 * GET /api/m/pdfscale?path=<….pdf>&page=N → the scale PRINTED on a vector PDF page (src/mobile/pdfscale.py, ~0.1 s):
 * tap-the-room on a PDF uses it before W5's door-swing estimate (/api/doorscale).
 * GET /api/m/sheetquad?path=<upload/… or data/…> → the paper in a photo of a printed sheet and the homography to its
 * flat plane (src/mobile/sheetquad.py, ~0.3 s): tap-the-room measures a tilted photo there.
 * GET /api/m/barscale?path=<upload/… or data/…> → the graphic scale bar printed on a photo: px per m and its two ends
 * (src/mobile/barscale.py; the photo's text layer, local OCR, 20-270 s cold, cached): tap-the-room uses it before the
 * door swings.
 * 'Try a case' (the sheets of W5's catalogue, web/fixtures/projects.json): GET /api/m/cases → {sheets: {path: true|false}}
 * (which of them this server has); GET /api/m/case/thumb?path= → a small JPEG of the sheet (src/mobile/thumb.py, cached);
 * POST /api/m/case?path=[&private=1[&eu=1]] → the sheet opened: a dataset or existing upload path comes back as is, an
 * example file (Stoa's sheets, outside git) is handed to W5's own POST /api/upload on THIS server (same store, normalising,
 * warm-up and answer cache as a visitor's upload; nothing goes over the phone's network) and its FileInfo comes back.
 * Same file layout as W5's middleware (STOA_UPLOADS, STOA_DATA). Runs before W5's /api middleware (enforce: "pre");
 * every other /api request goes on to it unchanged.
 */
export function phoneApi(): Plugin {
  const UPLOADS = process.env.STOA_UPLOADS ?? path.join(os.tmpdir(), "stoa-uploads");
  const DATA = process.env.STOA_DATA ?? "junction-dataset";
  // the same order as W5's middleware (S0 D42: a server started without JUNCTION_VENV fell back to the system python3,
  // which has no stoa): $JUNCTION_VENV, the team venv on this machine, then python3
  const PY = [process.env.JUNCTION_VENV, ".venv"].map((v) => (v ? `${v}/bin/python` : "")).find((py) => py && fs.existsSync(py)) ?? "python3";
  const SCRIPTS = { doorscale: path.join(__dirname, "doorscale.py"), pdfscale: path.join(__dirname, "pdfscale.py"), sheetquad: path.join(__dirname, "sheetquad.py"), barscale: path.join(__dirname, "barscale.py") };
  const cache = new Map<string, Promise<unknown>>();
  const absOf = (v: string, ext: RegExp): string | null => {
    const m = v.match(/^upload\/([0-9a-f]{12})\/(?:(cubicasa5k)\/)?([^/\\]+)$/);
    const abs = m ? (m[2] ? path.join(UPLOADS, m[1], "data", m[2], m[3]) : path.join(UPLOADS, m[1], m[3]))
      : /^data\/[^\\]+$/.test(v) && !v.split("/").includes("..") ? path.join(DATA, v) : null;
    return abs && ext.test(abs) && fs.existsSync(abs) ? abs : null;
  };
  // 'Try a case': the sheets of W5's catalogue (web/fixtures/projects.json, the desktop's projects too: one list). A sheet
  // path is data/… (the API reads it), upload/<id>/<name> (already in STOA_UPLOADS), or any other name: that file is
  // looked up by its base name in the example folders below (Stoa's sheets, the original PDFs, X1's scan, DfE's other
  // levels; outside git; the same folders as W5's /api/case, so both open the same upload: ids are content + name)
  const EXAMPLE_DIRS = [process.env.STOA_EXAMPLES_DIR ?? "", path.join(web, "../samples/examples")].filter(Boolean);
  const SHEET = /\.(pdf|png|jpe?g)$/i;
  /** every 2D sheet path the catalogue lists (only these can be opened or thumbnailed here) */
  const sheets = (): Set<string> => {
    try {
      const ps = JSON.parse(fs.readFileSync(path.join(web, "fixtures/projects.json"), "utf8")) as { sheets?: { path?: string }[] }[];
      return new Set(ps.flatMap((p) => (p.sheets ?? []).map((x) => x.path ?? "")).filter((x) => SHEET.test(x)));
    } catch {
      return new Set();
    }
  };
  /** where a catalogue sheet is: the dataset, an existing upload, or an example file to upload */
  const sheetAbs = (v: string): { abs: string; how: "dataset" | "upload" | "example" } | null => {
    if (v.startsWith("data/")) {
      const abs = absOf(v, SHEET);
      return abs ? { abs, how: "dataset" } : null;
    }
    const up = v.startsWith("upload/") ? absOf(v, SHEET) : null;
    if (up) return { abs: up, how: "upload" };
    const name = path.basename(v);
    for (const d of EXAMPLE_DIRS) if (fs.existsSync(path.join(d, name))) return { abs: path.join(d, name), how: "example" };
    return null;
  };
  /** POST the sheet to this same server's /api/upload (W5's middleware): same store, same answers. No style (as W5's
   *  /api/case): a style guess would put the sheet in another folder (upload/<id>/<style>/…) than the desktop's copy and
   *  could route a CAD sheet export as a CubiCasa image; without one the id is content + name, the same on both pages. */
  const uploadHere = (req: IncomingMessage, name: string, body: Buffer, extra: string) => new Promise<{ status: number; body: string }>((resolve, reject) => {
    const la = req.socket.localAddress ?? "127.0.0.1";
    const host = la.startsWith("::ffff:") ? la.slice(7) : la === "::" || la === "0.0.0.0" || !la ? "127.0.0.1" : la;
    const r = http.request({ host, port: req.socket.localPort, method: "POST", path: `/api/upload?name=${encodeURIComponent(name)}${extra}`,
      headers: { Host: "localhost", "Content-Type": "application/octet-stream", "Content-Length": body.length }, timeout: 180_000 }, (res) => {
      const parts: Buffer[] = [];
      res.on("data", (d: Buffer) => parts.push(d));
      res.on("end", () => resolve({ status: res.statusCode ?? 500, body: Buffer.concat(parts).toString("utf8") }));
    });
    r.on("timeout", () => r.destroy(new Error("the upload step took too long")));
    r.on("error", reject);
    r.end(body);
  });
  const THUMBS = path.join(os.tmpdir(), "w6-case-thumbs");
  const thumbs = new Map<string, Promise<string | null>>();
  const thumbOf = (abs: string): Promise<string | null> => {
    const out = path.join(THUMBS, `${createHash("sha1").update(abs).digest("hex").slice(0, 16)}.jpg`);
    if (!thumbs.has(abs)) thumbs.set(abs, fs.existsSync(out) ? Promise.resolve(out) : new Promise((resolve) => {
      fs.mkdirSync(THUMBS, { recursive: true });
      execFile(PY, [path.join(__dirname, "thumb.py"), abs, out], { timeout: 60_000 }, (err) => {
        if (err || !fs.existsSync(out)) thumbs.delete(abs);
        resolve(err || !fs.existsSync(out) ? null : out);
      });
    }));
    return thumbs.get(abs)!;
  };
  const caseMw = (req: IncomingMessage, res: ServerResponse): boolean => {
    const m = req.url?.match(/^\/api\/m\/(cases|case\/thumb|case)(?:\?|$)/);
    if (!m) return false;
    const q = new URL(req.url!, "http://x").searchParams;
    const json = (code: number, j: unknown) => {
      res.statusCode = code;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(j));
    };
    const listed = sheets();
    // GET /api/m/cases: which catalogue sheets this server can open
    if (m[1] === "cases") return json(200, { sheets: Object.fromEntries([...listed].map((v) => [v, !!sheetAbs(v)])) }), true;
    const v = q.get("path") ?? "";
    const at = listed.has(v) ? sheetAbs(v) : null;
    if (!at) return json(404, { detail: listed.has(v) ? "this sheet is not on this server" : "not a sheet of the catalogue" }), true;
    if (m[1] === "case/thumb") {
      void thumbOf(at.abs).then((t) => {
        if (!t) return json(404, { detail: "no thumbnail" });
        res.setHeader("Content-Type", "image/jpeg");
        res.setHeader("Cache-Control", "public, max-age=3600");
        fs.createReadStream(t).pipe(res);
      });
      return true;
    }
    // POST /api/m/case?path=: open it; an example file goes through W5's upload on this server
    if (at.how !== "example") return json(200, { path: v, how: at.how }), true;
    const extra = q.get("private") === "1" ? `&private=1${q.get("eu") === "1" ? "&eu=1" : ""}` : "";
    void uploadHere(req, path.basename(at.abs), fs.readFileSync(at.abs), extra)
      .then((r) => {
        res.statusCode = r.status;
        res.setHeader("Content-Type", "application/json");
        res.end(r.body);
      })
      .catch((e: Error) => json(502, { detail: `the sheet did not open: ${e.message}` }));
    return true;
  };
  const mw = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (caseMw(req, res)) return;
    const kind = req.url?.match(/^\/api\/m\/(doorscale|pdfscale|sheetquad|barscale)\b/)?.[1] as keyof typeof SCRIPTS | undefined;
    if (!kind) return next();
    const q = new URL(req.url!, "http://x").searchParams;
    const abs = absOf(q.get("path") ?? "", kind === "pdfscale" ? /\.pdf$/i : /\.(png|jpe?g)$/i);
    const page = String(Math.max(0, Math.min(999, parseInt(q.get("page") ?? "0", 10) || 0)));
    res.setHeader("Content-Type", "application/json");
    if (!abs) return void res.end(JSON.stringify({ ok: false, why: kind === "pdfscale" ? "not a PDF we have" : "not a photo or image we have" }));
    const key = `${kind}|${abs}|${page}`;
    if (!cache.has(key)) {
      const args = kind === "pdfscale" ? [SCRIPTS.pdfscale, abs, page] : [SCRIPTS[kind], abs];
      // barscale waits for the upload's warm-up (<= 90 s), then reads the photo's text layer: 20-270 s cold on a 4,000 px photo
      cache.set(key, new Promise((resolve) => execFile(PY, args, { cwd: path.resolve(web, ".."), timeout: kind === "barscale" ? 330_000 : 60_000 }, (err, out) => {
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
