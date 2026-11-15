// F4 batch Q&A: a PDF given as a QUESTION LIST (a list exported from Word / a mail / a test sheet), read with pdf.js (the
// viewer's own copy and worker; browser only). A drawing dropped here by mistake has no question lines and is refused
// with a pointer to the drawing area. The line logic (pdfQuestions) is pure and tested in batch.test.ts.
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { pdfQuestions, type TextItem } from "./parse";

if (!pdfjs.GlobalWorkerOptions.workerSrc) pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

/** The text items of up to `maxPages` pages, in reading order per page (pdf.js text content: string, baseline, x, size). */
export async function pdfItems(data: Uint8Array, maxPages = 12): Promise<{ items: TextItem[][]; pages: number }> {
  const doc = await pdfjs.getDocument({ data }).promise;
  try {
    const out: TextItem[][] = [];
    for (let i = 1; i <= Math.min(doc.numPages, maxPages); i++) {
      const page = await doc.getPage(i);
      const tc = await page.getTextContent();
      out.push((tc.items as { str?: string; transform?: number[]; width?: number; height?: number }[])
        .filter((it) => typeof it.str === "string" && it.transform)
        .map((it) => ({ s: it.str!, x: it.transform![4], y: it.transform![5], w: it.width ?? 0, h: Math.abs(it.height || it.transform![3] || 10) })));
    }
    return { items: out, pages: doc.numPages };
  } finally {
    void doc.destroy();
  }
}

/** A PDF's questions, or why it is not a question list. */
export async function readPdfList(data: Uint8Array) {
  const { items, pages } = await pdfItems(data);
  return { ...pdfQuestions(items), pages };
}
