// F4 convention switch: the drawing's pixels around the traced rooms, for walls.ts inkWalls (browser only). A picture
// is drawn from its file, a PDF page rendered by pdf.js (the viewer's own copy and worker), both at ~40 px per metre and
// cropped to the rooms + 3 m, so even an A0 sheet stays a few million pixels. Polygons come in the takeoff's space
// (image_px of the uploaded picture, or pdf_pt from the top of the unrotated page, like PyMuPDF).
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { fileUrl } from "../../api";
import type { Pt } from "./calc";
import { inkWalls, type InkWalls } from "./walls";

if (!pdfjs.GlobalWorkerOptions.workerSrc) pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

const TARGET = 40; // px per metre
// phones (iOS caps a canvas near 16 M px and its memory far lower): a smaller raster; the walls still span >= 4 px
const SMALL = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
const MAX_PX = SMALL ? 4_000_000 : 12_000_000;

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((res, rej) => {
    const im = new Image();
    im.onload = () => res(im);
    im.onerror = () => rej(new Error("the picture did not load"));
    im.src = url;
  });
}

/** Ink walls of the open drawing: polys in the takeoff's space, mPerUnit metres per unit of that space. */
export async function measureInk(path: string, page: number, space: string | null, polys: Pt[][], mPerUnit: number): Promise<InkWalls> {
  const url = fileUrl(path);
  const xs = polys.flatMap((p) => p.map((q) => q[0])), ys = polys.flatMap((p) => p.map((q) => q[1]));
  const pad = 3 / mPerUnit;
  let [x0, y0, x1, y1] = [Math.min(...xs) - pad, Math.min(...ys) - pad, Math.max(...xs) + pad, Math.max(...ys) + pad];
  let f = TARGET * mPerUnit; // raster px per drawing unit
  const area = (x1 - x0) * (y1 - y0) * f * f;
  if (area > MAX_PX) f *= Math.sqrt(MAX_PX / area);
  const c = document.createElement("canvas");
  let toPx: (q: Pt) => Pt;
  if (space === "pdf_pt" || /\.pdf$/i.test(path)) {
    const doc = await pdfjs.getDocument({ url }).promise;
    try {
      const pg = await doc.getPage(page + 1);
      const v = pg.view;
      const vp = pg.getViewport({ scale: f });
      const toVp = (q: Pt): Pt => vp.convertToViewportPoint(v[0] + q[0], v[3] - q[1]) as Pt;
      const corners = [toVp([x0, y0]), toVp([x1, y1])];
      const vx0 = Math.max(0, Math.min(corners[0][0], corners[1][0])), vy0 = Math.max(0, Math.min(corners[0][1], corners[1][1]));
      const vx1 = Math.min(vp.width, Math.max(corners[0][0], corners[1][0])), vy1 = Math.min(vp.height, Math.max(corners[0][1], corners[1][1]));
      c.width = Math.max(1, Math.round(vx1 - vx0));
      c.height = Math.max(1, Math.round(vy1 - vy0));
      const g = c.getContext("2d", { willReadFrequently: true })!;
      g.fillStyle = "#fff";
      g.fillRect(0, 0, c.width, c.height);
      await pg.render({ canvas: c, canvasContext: g, viewport: vp, transform: [1, 0, 0, 1, -vx0, -vy0] }).promise;
      pg.cleanup();
      toPx = (q) => {
        const p = toVp(q);
        return [p[0] - vx0, p[1] - vy0];
      };
    } finally {
      void doc.destroy();
    }
  } else {
    const im = await loadImage(url);
    x0 = Math.max(0, x0);
    y0 = Math.max(0, y0);
    x1 = Math.min(im.naturalWidth, x1);
    y1 = Math.min(im.naturalHeight, y1);
    f = Math.min(f, 1); // never enlarge a picture
    c.width = Math.max(1, Math.round((x1 - x0) * f));
    c.height = Math.max(1, Math.round((y1 - y0) * f));
    const g = c.getContext("2d", { willReadFrequently: true })!;
    g.fillStyle = "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.drawImage(im, x0, y0, x1 - x0, y1 - y0, 0, 0, c.width, c.height);
    toPx = (q) => [(q[0] - x0) * f, (q[1] - y0) * f];
  }
  const W = c.width, H = c.height;
  const data = c.getContext("2d", { willReadFrequently: true })!.getImageData(0, 0, W, H).data;
  const dark = new Uint8Array(W * H);
  for (let i = 0, j = 0; i < W * H; i++, j += 4) dark[i] = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2] < 128 ? 1 : 0; // ink: darker than mid-grey (antialiased edges under half covered are paper)
  const pxPolys = polys.map((p) => p.map(toPx));
  const res = inkWalls((x, y) => dark[y * W + x] === 1, W, H, pxPolys, f / mPerUnit);
  // rays back into the takeoff's space (for Show where): invert toPx numerically on the two corners
  const a0 = toPx([0, 0]), ax = toPx([1, 0]), ay = toPx([0, 1]);
  const det = (ax[0] - a0[0]) * (ay[1] - a0[1]) - (ax[1] - a0[1]) * (ay[0] - a0[0]);
  const back = (p: Pt): Pt => {
    const dx = p[0] - a0[0], dy = p[1] - a0[1];
    return [(dx * (ay[1] - a0[1]) - dy * (ay[0] - a0[0])) / det, ((ax[0] - a0[0]) * dy - (ax[1] - a0[1]) * dx) / det];
  };
  res.rays = res.rays.map((r) => ({ ...r, a: back(r.a), b: back(r.b) }));
  return res;
}
