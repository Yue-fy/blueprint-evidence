// F4 batch Q&A: a small picture of WHERE each answer comes from (the drawing cropped around its first marked box or
// outline, the outline drawn in), so a list of 20 answers can be checked at a glance before opening any of them.
// PDFs through pdf.js (the viewer's own copy and worker), images through an <img>; one at a time, cached per answer.
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { fileUrl } from "../../api";
import type { Answer, Evidence } from "../../types";

if (!pdfjs.GlobalWorkerOptions.workerSrc) pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

type Pt = [number, number];
const docs = new Map<string, Promise<pdfjs.PDFDocumentProxy>>();
const imgs = new Map<string, Promise<HTMLImageElement>>();
let queue: Promise<unknown> = Promise.resolve();

const pts = (p: unknown): Pt[] => Array.isArray(p) ? (p as unknown[]).filter((q): q is number[] => Array.isArray(q) && q.length >= 2).map((q) => [Number(q[0]), Number(q[1])] as Pt) : [];

/** The evidence to picture: the first one with a place on a PDF page or an image, preferring the drawing on screen. */
export function pickEvidence(a: Answer, file: string): Evidence | null {
  const placed = (a.evidence ?? []).filter((e) => (e.bbox_space === "pdf_pt" || e.bbox_space === "image_px")
    && ((Array.isArray(e.bbox) && e.bbox.length >= 4) || pts(e.polygon).length >= 3 || pts(e.polyline).length >= 2));
  return placed.find((e) => e.file === file) ?? placed[0] ?? null;
}

/** The outline (polygon / polyline) or the box's corners, in the evidence's own space. */
function shapeOf(e: Evidence): { pts: Pt[]; closed: boolean } {
  const poly = pts(e.polygon);
  if (poly.length >= 3) return { pts: poly, closed: true };
  const line = pts(e.polyline);
  if (line.length >= 2) return { pts: line, closed: false };
  const b = (e.bbox ?? []) as number[];
  return { pts: [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]], closed: true };
}

/** A crop around the points with some context, widened to the picture's aspect (w:h). */
function cropAround(p: Pt[], w: number, h: number, minSide: number): [number, number, number, number] {
  let [x0, y0, x1, y1] = [Math.min(...p.map((q) => q[0])), Math.min(...p.map((q) => q[1])), Math.max(...p.map((q) => q[0])), Math.max(...p.map((q) => q[1]))];
  const pad = Math.max(0.3 * Math.max(x1 - x0, y1 - y0), minSide / 2);
  x0 -= pad; y0 -= pad; x1 += pad; y1 += pad;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  let cw = x1 - x0, ch = y1 - y0;
  if (cw / ch > w / h) ch = cw * h / w; else cw = ch * w / h;
  return [cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2];
}

function outline(g: CanvasRenderingContext2D, p: Pt[], closed: boolean) {
  if (p.length < 2) return;
  g.beginPath();
  p.forEach((q, i) => (i ? g.lineTo(q[0], q[1]) : g.moveTo(q[0], q[1])));
  if (closed) g.closePath();
  g.lineJoin = "round";
  g.strokeStyle = "rgba(0, 113, 227, 0.95)";
  g.lineWidth = Math.max(2, Math.round(g.canvas.width / 88));
  if (closed) { g.fillStyle = "rgba(0, 113, 227, 0.14)"; g.fill(); }
  g.stroke();
}

async function pdfThumb(url: string, e: Evidence, w: number, h: number): Promise<HTMLCanvasElement> {
  let d = docs.get(url);
  if (!d) {
    docs.set(url, (d = pdfjs.getDocument({ url }).promise));
    d.catch(() => docs.delete(url));
  }
  const page = await (await d).getPage((e.page ?? 0) + 1);
  // evidence is in PDF points, y down from the top of the unrotated page (PyMuPDF); pdf.js user space is y up, and the
  // scale-1 viewport applies the page's /Rotate, so the picture is turned like the drawing on screen
  const v = page.view;
  const vp1 = page.getViewport({ scale: 1 });
  const toVp = (q: Pt): Pt => vp1.convertToViewportPoint(v[0] + q[0], v[3] - q[1]) as Pt;
  const shape = shapeOf(e);
  const p = shape.pts.map(toVp);
  const [x0, y0, x1] = cropAround(p, w, h, 40);
  const s = w / (x1 - x0);
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff";
  g.fillRect(0, 0, w, h);
  await page.render({ canvas: c, canvasContext: g, viewport: page.getViewport({ scale: s }), transform: [1, 0, 0, 1, -x0 * s, -y0 * s] }).promise;
  page.cleanup();
  outline(g, p.map((q) => [(q[0] - x0) * s, (q[1] - y0) * s] as Pt), shape.closed);
  return c;
}

async function imageThumb(url: string, e: Evidence, w: number, h: number): Promise<HTMLCanvasElement> {
  let ip = imgs.get(url);
  if (!ip) {
    imgs.set(url, (ip = new Promise((res, rej) => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = () => rej(new Error("image did not load"));
      im.src = url;
    })));
    ip.catch(() => imgs.delete(url));
  }
  const im = await ip;
  const shape = shapeOf(e);
  const [x0, y0, x1, y1] = cropAround(shape.pts, w, h, 60);
  const s = w / (x1 - x0);
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const g = c.getContext("2d")!;
  g.fillStyle = "#fff";
  g.fillRect(0, 0, w, h);
  g.drawImage(im, x0, y0, x1 - x0, y1 - y0, 0, 0, w, h);
  outline(g, shape.pts.map((q) => [(q[0] - x0) * s, (q[1] - y0) * s] as Pt), shape.closed);
  return c;
}

/** A data URL (PNG) of the drawing around this answer's evidence, or null (nothing placed, or it did not render).
 *  Rendered one at a time (a large CAD sheet takes ~0.1-0.5 s per crop). */
export function evidenceThumb(a: Answer, file: string, cssW = 176, cssH = 104): Promise<string | null> {
  // sharp on a retina laptop: drawn at the screen's pixel ratio (max 2), shown at cssW x cssH
  const k = Math.min(2, Math.max(1, Math.round(globalThis.devicePixelRatio || 1)));
  const w = cssW * k, h = cssH * k;
  const e = pickEvidence(a, file);
  if (!e) return Promise.resolve(null);
  const path = e.file || file;
  // the same place on the same drawing is drawn once (asked again, a reload of the list, two questions on one row)
  const key = `${path}|${e.bbox_space}|${e.page ?? 0}|${JSON.stringify(shapeOf(e).pts)}|${w}x${h}`;
  const hit = made.get(key);
  if (hit) return hit;
  const job = queue.catch(() => undefined).then(async () => {
    const url = fileUrl(path);
    const c = e.bbox_space === "pdf_pt" && /\.pdf$/i.test(path) ? await pdfThumb(url, e, w, h)
      : e.bbox_space === "image_px" && /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(path) ? await imageThumb(url, e, w, h) : null;
    return c ? c.toDataURL("image/png") : null;
  });
  queue = job;
  const out = job.catch(() => null);
  if (made.size >= 400) made.delete(made.keys().next().value!);
  made.set(key, out);
  return out;
}
const made = new Map<string, Promise<string | null>>();
