/** 'Export takeoff' (captain 18:50): every answer of this visit as one CSV row an estimator can paste into a sheet:
 *  the number as the card shows it (after any measuring on the drawing), its unit, the sheet and page, how sure we are,
 *  the method, the evidence and where the data went, plus a link that opens the same answer again. Kept in memory only
 *  (this browser tab); the same question on the same sheet replaces its row. */
import { confidenceWords } from "./customer";
import { dataWent } from "./governance";
import type { Answer } from "./types";

export type TakeoffRow = {
  at: string; path: string; file: string; page: number | null; question: string;
  shown: string; unit: string; sentence: string; reader: string; confidence: number; sure: string;
  method: string; evidence: string; data: string; link: string;
};

const rows: TakeoffRow[] = [];
const key = (r: { path: string; page: number | null; question: string }) => `${r.path}|${r.page ?? ""}|${r.question.trim().toLowerCase()}`;

export function recordAnswer(a: Answer, ctx: { path: string; page: number | null; question: string; link: string }) {
  if (!ctx.question || a.value === undefined) return;
  const ev = (a.evidence ?? []).filter((e) => e.bbox || e.polygon || e.polyline || e.ifc_guid);
  const r: TakeoffRow = {
    at: new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
    path: ctx.path, file: ctx.path.split("/").pop() ?? ctx.path, page: ctx.page, question: ctx.question,
    shown: a.value === null ? "" : String(a.value), unit: String(a.unit ?? ""), sentence: "",
    reader: a.value === null ? "no answer" : `${a.value}${a.unit ? ` ${a.unit}` : ""}`,
    confidence: Math.round((a.confidence ?? 0) * 100), sure: a.value === null ? "No answer" : confidenceWords(a.confidence ?? 0).word,
    method: String(a.solver ?? ""),
    evidence: ev.length ? `${ev.length} marked: ${ev.slice(0, 3).map((e) => (e.note ?? "").replace(/\s+/g, " ").slice(0, 80)).join(" | ")}${ev.length > 3 ? " | …" : ""}` : "none marked",
    data: dataWent(a).map((p) => p.label).join(" + "),
    link: ctx.link,
  };
  const i = rows.findIndex((x) => key(x) === key(r));
  if (i >= 0) rows.splice(i, 1);
  rows.push(r);
}

/** The card may change the number after the answer came (a room traced on the drawing, a scale the visitor set): the row
 *  takes what the card shows when the next question is asked or the CSV is made. */
export function updateShown(hero: HTMLElement | null, question: string, path: string) {
  const r = rows[rows.length - 1];
  if (!r || !hero || r.question !== question || r.path !== path) return;
  const big = hero.querySelector(".hero-value .big")?.textContent?.replace(/^≈\s*/, "").trim();
  const unit = hero.querySelector(".hero-value .unit")?.textContent?.trim();
  const sentence = hero.querySelector(".hero-sentence")?.textContent?.trim();
  const sure = hero.querySelector(".hero-badges .conf")?.textContent?.trim();
  if (big) r.shown = big;
  if (unit !== undefined && big) r.unit = unit;
  if (sentence) r.sentence = sentence;
  if (sure) r.sure = sure;
}

/** F1 23:17: 'Clear selection' / 'Clear all' in the Measure tool leave no trace: the rows its 'Save to takeoff' made on
 *  that file (their questions), and with scope 'all' every row a measurement made there (method 'measure/…'). Returns
 *  how many rows went. */
export function dropRows(path: string, questions: string[], all = false): number {
  const qs = new Set(questions.map((q) => q.trim().toLowerCase()));
  const before = rows.length;
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i];
    if (r.path !== path) continue;
    if (qs.has(r.question.trim().toLowerCase()) || (all && /^measure\//i.test(r.method))) rows.splice(i, 1);
  }
  return before - rows.length;
}

export const takeoffCount = () => rows.length;
/** This visit's answers, read-only copies (F2 21:03: the takeoff panel's Export adds them as section 'visit'). */
export const takeoffRows = (): TakeoffRow[] => rows.map((r) => ({ ...r }));

const cell = (v: unknown) => {
  const s = String(v ?? "");
  return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

export function takeoffCsv(): string {
  const head = ["#", "time", "question", "answer", "unit", "what it is", "how sure", "confidence %", "drawing", "page", "method", "evidence", "data went to", "reader's value", "open again"];
  const body = rows.map((r, i) => [i + 1, r.at, r.question, r.shown, r.unit, r.sentence, r.sure, r.confidence, r.file, r.page === null ? "" : r.page + 1,
    r.method, r.evidence, r.data, r.reader, r.link].map(cell).join(","));
  return `﻿${[head.join(","), ...body].join("\r\n")}\r\n`; // BOM: Excel opens UTF-8 (m², ≈) correctly
}

export function downloadTakeoff() {
  const blob = new Blob([takeoffCsv()], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const d = new Date(), p2 = (x: number) => String(x).padStart(2, "0");
  a.download = `takeoff-${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}.csv`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
