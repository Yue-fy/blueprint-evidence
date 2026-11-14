/** One PDF page as an image for the phone view (loaded only when a PDF is picked: photos never load pdf.js).
 *  pdf_pt evidence is PyMuPDF's UNROTATED page space (top-left origin, docs/CONTRACT.md), but the page is drawn the way
 *  it is meant to be read (its /Rotate applied: 5 of Stoa's 9 example PDFs are rotated 90°, e.g. USACE S-100F, AFCEC
 *  A-101). toPx / toPt map between the two through pdf.js's own viewports (unrotated → PDF user space → shown). */
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

type Pt = [number, number];
export interface Rendered { url: string; scale: number; pages: number; rotation: number; toPx: (pt: Pt) => Pt; toPt: (px: Pt) => Pt }

// one parsed document per file: drawing sets are 16-24 MB (Stoa's AW-78 sets, 121 pages) and the pager turns pages
const docs = new Map<string, Promise<pdfjs.PDFDocumentProxy>>();

export async function renderPage(url: string, page = 0, maxSide = 2400): Promise<Rendered> {
  if (!docs.has(url)) {
    for (const old of docs.values()) void old.then((d) => d.destroy()).catch(() => undefined); // phones: one file in memory
    docs.clear();
    const p = pdfjs.getDocument({ url }).promise;
    p.catch(() => docs.delete(url));
    docs.set(url, p);
  }
  const doc = await docs.get(url)!;
  const p = await doc.getPage(Math.min(doc.numPages, page + 1));
  const base = p.getViewport({ scale: 1 });
  // phones cap canvas memory (iOS ~16 MP): 2,400 px on the long side is plenty to read a sheet when zoomed
  const scale = Math.min(4, maxSide / Math.max(base.width, base.height));
  const vp = p.getViewport({ scale });
  const flat = p.getViewport({ scale: 1, rotation: 0 }); // PyMuPDF's unrotated pt space
  const toPx = (q: Pt): Pt => {
    const u = flat.convertToPdfPoint(q[0], q[1]) as number[];
    return vp.convertToViewportPoint(u[0], u[1]) as Pt;
  };
  const toPt = (q: Pt): Pt => {
    const u = vp.convertToPdfPoint(q[0], q[1]) as number[];
    return flat.convertToViewportPoint(u[0], u[1]) as Pt;
  };
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(vp.width);
  canvas.height = Math.round(vp.height);
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  await p.render({ canvas, canvasContext: ctx, viewport: vp }).promise;
  const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, "image/png"));
  if (!blob) throw new Error("could not draw the PDF page");
  return { url: URL.createObjectURL(blob), scale, pages: doc.numPages, rotation: vp.rotation, toPx, toPt };
}
