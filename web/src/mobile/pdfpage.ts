/** One PDF page as an image for the phone view (loaded only when a PDF is picked: photos never load pdf.js).
 *  pdf_pt evidence (PyMuPDF unrotated page space, top-left origin) maps to the image as px = pt × scale. */
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

export async function renderPage(url: string, page = 0, maxSide = 2400): Promise<{ url: string; scale: number; pages: number }> {
  const doc = await pdfjs.getDocument({ url }).promise;
  const p = await doc.getPage(Math.min(doc.numPages, page + 1));
  const base = p.getViewport({ scale: 1 });
  // phones cap canvas memory (iOS ~16 MP): 2,400 px on the long side is plenty to read a sheet when zoomed
  const scale = Math.min(4, maxSide / Math.max(base.width, base.height));
  const vp = p.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(vp.width);
  canvas.height = Math.round(vp.height);
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  await p.render({ canvas, canvasContext: ctx, viewport: vp }).promise;
  const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, "image/png"));
  if (!blob) throw new Error("could not draw the PDF page");
  return { url: URL.createObjectURL(blob), scale, pages: doc.numPages };
}
