// F4 batch Q&A panel: paste many questions (one per line, any language) or drop a .txt / .csv / .tsv / .xlsx / .json,
// all answered on the drawing (and page) on screen through the normal /api/ask, in a live table: question, answer,
// unit, how sure, cost tier, time, Show (opens that answer with its evidence on the drawing). Sortable; CSV / XLSX
// export; one summary line. Kept per drawing in this browser so a reload keeps the answers already in.
import { fmtValue, unitLabel } from "../../card";
import { confidenceWords, costTier, needsMeasuring } from "../../customer";
import type { Answer } from "../../types";
import { MAX_QUESTIONS, SHEET_SEP, droppedLines, extOf, matchDrawing, matchSheet, parseFileText, parseText, fromRows, sheetLabel, toLines, type BatchQuestion, type SheetRef } from "./parse";
import { BatchRunner, effective, firstNumber, matchExpected, median, solverMs, type AskFn, type AskTarget, type CardValue, type Row } from "./runner";
import { reportHtml } from "./report";
import { evidenceThumb } from "./thumbs";
import { readXlsx, xlsxBytes, type Cell } from "./xlsx";

export interface BatchFile { path: string; name?: string; page?: number; pages?: number; solver?: string }

export interface BatchContext {
  /** the drawing on screen (null: none open) */
  file(): BatchFile | null;
  /** show this answer on the drawing like a typed question (evidence, hero card). complete = false: the answer was
   *  restored from storage without its outlines / full trace (ask again instead of showing it as is). Absent: Show
   *  re-asks via the page's question box. */
  show?(file: BatchFile, question: string, answer: Answer, complete: boolean, page?: number): void | Promise<void>;
  /** a link that opens this question on this drawing again (export column) */
  link?(file: BatchFile, question: string): string;
  /** after every change: answered / total / running (e.g. a badge on the toolbar button while the panel is closed) */
  progress?(done: number, total: number, running: boolean): void;
  ask?: AskFn;
  /** the drawings a list may name in its 'drawing' column (the set on screen, this visit's uploads) */
  files?(): BatchFile[];
  /** open another drawing of the list (before Show of a row asked there), or turn to a page the list named */
  open?(path: string, page?: number): Promise<void>;
  /** the pages of a drawing set as the sheet index labels them ([] for one-page files / not a PDF) */
  sheets?(path: string): Promise<SheetRef[]>;
  /** what the answer card shows now (its number may change after the answer came: a room traced and measured on the
   *  drawing, a scale the visitor set), for the question in the box on the drawing on screen; null: no card */
  readCard?(): (CardValue & { question: string; path: string }) | null;
  /** call back when the card changes (returns unsubscribe) */
  watchCard?(cb: () => void): () => void;
}

const baseName = (p: string) => p.split("/").pop() ?? p;

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const STORE = "f4.batch.v1:";
const fmtS = (ms?: number) => ms === undefined ? "" : ms < 1000 ? "<1 s" : ms < 60_000 ? `${Math.round(ms / 1000)} s` : `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
const TIER_RANK = { cheap: 1, mid: 2, high: 3 } as const;
/** A starter list: Stoa's question kinds in their order of importance (areas, lengths, counts) + one in Finnish. */
const EXAMPLES = [
  "What is the total floor area of this plan?",
  "What is the area of the largest room?",
  "How long is the longest corridor?",
  "How many doors are there?",
  "How many windows are there?",
  "What is the scale of this drawing?",
  "Kuinka monta ovea piirustuksessa on?",
];
const COST_HINT = "cheap = rules on the drawing's own lines and text (CPU) · in-between = our local AI model · expensive = a hosted AI model";

/** What the table shows for one answer (same honesty rules as the answer card: an area / length nobody measured is
 *  shown as a guess, not as a measurement). */
export function shownOf(r: Row, file = ""): { text: string; num: number | null; unit: string; guess: boolean; sure: string; cls: string; tier: string; tierCls: string; tierWhy: string } {
  const a = r.answer;
  if (!a) return { text: "", num: null, unit: "", guess: false, sure: "", cls: "", tier: "", tierCls: "", tierWhy: "" };
  if (r.card) {
    // the card measured it after Show (traced the room at the sheet's scale / the visitor's scale): its number and words
    const t = costTier(a);
    const est = /≈/.test(r.card.big);
    return { text: r.card.big.replace(/^≈\s*/, ""), num: firstNumber(r.card.big), unit: r.card.unit, guess: false, sure: r.card.sure || (est ? "Best estimate" : "Measured"),
      cls: est ? "est" : "fair", tier: t.word, tierCls: t.tier, tierWhy: t.why };
  }
  const none = a.value === null || a.value === undefined;
  const guess = !none && needsMeasuring(a, r.q, file);
  const c = confidenceWords(a.confidence ?? 0);
  const t = costTier(a);
  const unit = a.unit === "count" ? "" : unitLabel(a.unit);
  return {
    text: none ? "No answer" : fmtValue(a),
    num: typeof a.value === "number" ? a.value : null,
    unit, guess,
    sure: none ? "No answer" : guess ? "Not measured" : c.word,
    cls: none ? "none" : guess ? "est guess" : c.cls,
    tier: t.word, tierCls: t.tier, tierWhy: t.why,
  };
}

export function exportHeader(hasExpected: boolean): string[] {
  return ["#", ...(hasExpected ? ["your id"] : []), "question", "answer", "unit", "how sure", "confidence %", "cost tier",
    "seconds", "seconds incl. queue", "status", ...(hasExpected ? ["expected", "matches"] : []), "method", "evidence", "assumptions", "missing", "drawing", "page", "open again"];
}

export function exportRows(rows: Row[], file: BatchFile, link?: (q: string, path: string) => string): Cell[][] {
  const hasExp = rows.some((r) => r.expected);
  return rows.map((r, i) => {
    const a = r.answer;
    const path = r.path ?? file.path;
    const own = path === file.path;
    const s = shownOf(r, path);
    const ev = (a?.evidence ?? []).filter((e) => e.bbox || e.polygon || e.polyline || e.ifc_guid);
    const m = matchExpected(r.expected, effective(r));
    const reader = r.card && a ? `${a.value === null ? "no answer" : String(a.value)} ${a.unit === "count" ? "" : unitLabel(a.unit)}`.trim() : "";
    return [i + 1, ...(hasExp ? [r.ref ?? ""] : []), r.q,
      r.card ? s.num ?? s.text : a ? (typeof a.value === "number" ? a.value : a.value === null ? "" : String(a.value)) : "",
      a ? (a.unit === "count" && !r.card ? "count" : s.unit) : "",
      a ? s.sure : "", a ? Math.round((a.confidence ?? 0) * 100) : "", a ? s.tier : "",
      solverMs(r) !== undefined ? Math.round(solverMs(r)! / 100) / 10 : "", r.ms !== undefined ? Math.round(r.ms / 100) / 10 : "",
      r.status === "done" ? (a?.cached ? "done (cached)" : "done") : r.status === "error" ? `failed: ${r.error ?? ""}` : r.status,
      ...(hasExp ? [r.expected ?? "", m === "yes" ? "yes" : m === "no" ? "no" : ""] : []),
      a?.solver ?? "",
      ev.length ? `${ev.length} marked: ${ev.slice(0, 3).map((e) => (e.note ?? "").replace(/\s+/g, " ").slice(0, 80)).join(" | ")}${ev.length > 3 ? " | …" : ""}` : a ? "none marked" : "",
      [...(a?.assumptions ?? []), ...(r.was ? [`changed on the answer card (was ${r.was})`] : []),
        ...(r.card ? [`measured on the answer card after Show${r.card.sentence ? `: ${r.card.sentence}` : ""}; the reader's value was ${reader}`] : [])].join(" | "), (a?.missing ?? []).join(" | "),
      r.missing ? `${r.missing} (not uploaded)` : `${own ? file.name ?? baseName(file.path) : baseName(path)}${r.sheet ? ` · ${r.sheet}` : ""}${r.named ? ` (your list: ${r.named})` : ""}`,
      r.page !== undefined ? r.page + 1 : own && file.page !== undefined ? file.page + 1 : "",
      link ? link(r.q, path) : ""];
  });
}

const csvCell = (v: Cell) => {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n\r;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const tsvCell = (v: Cell) => (v === null || v === undefined ? "" : String(v)).replace(/[\t\r\n]+/g, " ");
export const toTsv = (header: string[], rows: Cell[][]) => [header, ...rows].map((r) => r.map(tsvCell).join("\t")).join("\n") + "\n";
export const toCsv = (header: string[], rows: Cell[][]) => `﻿${[header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n")}\r\n`;

function download(name: string, data: BlobPart, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([data], { type }));
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

type SortKey = "id" | "q" | "answer" | "sure" | "tier" | "ms" | "status" | "match";
/** where a drawing the list names is asked: a file, a page of a set (+ its label), and the list's own name for it */
type Place = { path: string; page?: number; sheet?: string; named?: string };

/** The panel, mounted into `host` (a side panel, or the standalone dev page). Returns a handle to refresh when the
 *  drawing on screen changes. */
export function mountBatch(host: HTMLElement, ctx: BatchContext) {
  injectCss();
  /** the list's own drawing (its rows are kept under this drawing's key); onScreen = the drawing open now (another
   *  drawing of a list that names several, while the visitor looks at one of them) */
  let file = ctx.file();
  let onScreen = file;
  const rowPath = (r: Row) => r.path ?? file?.path ?? "";
  /** the rows' drawings, when the list spans more than one (or names one that is not uploaded) */
  const drawingsOf = () => [...new Set(runner.rows.map(rowPath))];
  const multi = () => drawingsOf().length > 1 || runner.rows.some((r) => r.missing || r.page !== undefined);
  const fileOf = (path: string): BatchFile => path === file?.path ? file : ctx.files?.().find((f) => f.path === path) ?? { path, name: baseName(path) };
  /** the drawings a list may name: the list's own, the one on screen, the set and this visit's uploads */
  const candidates = () => [...new Map([...(file ? [file] : []), ...(onScreen ? [onScreen] : []), ...(ctx.files?.() ?? [])].map((f) => [f.path, f])).values()];
  /** names the sheet index placed: a page of a set ('A-101' → p. 5), or the one drawing open (see resolvePlaces) */
  const placed = new Map<string, Place>();
  /** a drawing named by the list → where it is asked: a file's path (by its name), else a page of a set (by its sheet
   *  number / title, once resolvePlaces read the sheet index), else null (not there) */
  const place = (name: string): Place | null => {
    if (name.includes(SHEET_SEP.trim())) return placed.get(name.trim().toLowerCase()) ?? null; // 'file | sheet': after resolvePlaces
    const p = matchDrawing(name, candidates());
    return p ? { path: p } : placed.get(name.trim().toLowerCase()) ?? null;
  };
  /** 'plan.pdf | A-101' → ['plan.pdf', 'A-101']; a name without a sheet → [name, ''] */
  const splitName = (n: string) => { const k = n.indexOf(SHEET_SEP.trim()); return k < 0 ? [n.trim(), ""] : [n.slice(0, k).trim(), n.slice(k + 1).trim()]; };
  const resolve = (name: string) => place(name)?.path ?? null;
  const placeKey = (p: { path: string; page?: number }) => `${p.path}${p.page !== undefined ? `#${p.page}` : ""}`;
  /** Names no file is called: read the sheet index of the PDFs open / uploaded and place each name on the ONE page whose
   *  sheet number / title it names ('A-101', 'page 3', 'Floor Plan - 1st Floor'). Then, when only ONE drawing is open in
   *  this visit and the whole list calls it by one other name that matched nothing (its sheet number, a title; no other
   *  file extension), the list is about that drawing: its rows are asked there and say so. Everything else stays 'not
   *  uploaded' ('Ask the N not uploaded on <drawing>' is one click). */
  async function resolvePlaces(names: string[]) {
    const all = [...new Set(names.map((n) => n.trim()).filter(Boolean))];
    const todo = all.filter((n) => !place(n));
    if (!todo.length) return;
    const cands = candidates();
    // 'file | sheet' (a file column + a page / sheet column): that file, then the page its sheet names in it
    for (const n of todo.filter((x) => x.includes(SHEET_SEP.trim()))) {
      const [fn, sn] = splitName(n);
      const fp = fn ? matchDrawing(fn, cands) : (cands.length === 1 ? cands[0].path : null);
      if (!fp) continue;
      const idx = ctx.sheets && /\.pdf$/i.test(fp) ? await Promise.race([ctx.sheets(fp).catch(() => [] as SheetRef[]),
        new Promise<SheetRef[]>((res) => setTimeout(() => res([]), 45_000))]) : [];
      const pg = sn ? matchSheet(sn, idx) : null;
      if (pg !== null) placed.set(n.toLowerCase(), idx.length > 1 ? { path: fp, page: pg, sheet: sheetLabel(idx.find((x) => x.page === pg)) } : { path: fp, sheet: sheetLabel(idx.find((x) => x.page === pg)) });
      else if (!sn || idx.length <= 1) placed.set(n.toLowerCase(), { path: fp }); // a one-page file: its only page
    }
    const pdfs = cands.filter((f) => /\.pdf$/i.test(f.path));
    if (ctx.sheets && pdfs.length) {
      const idx = await Promise.all(pdfs.map((f) => Promise.race([ctx.sheets!(f.path).catch(() => [] as SheetRef[]),
        new Promise<SheetRef[]>((res) => setTimeout(() => res([]), 45_000))])));
      for (const n of todo.filter((x) => !x.includes(SHEET_SEP.trim()))) {
        const hits: Place[] = [];
        pdfs.forEach((f, i) => {
          const pg = matchSheet(n, idx[i]);
          if (pg === null) return;
          const lab = sheetLabel(idx[i].find((x) => x.page === pg));
          hits.push(idx[i].length > 1 ? { path: f.path, page: pg, sheet: lab } : { path: f.path, sheet: lab });
        });
        if (hits.length === 1) placed.set(n.toLowerCase(), hits[0]);
      }
    }
    // only when NOTHING in the list matched a file or a sheet: a list that names sheets of the set and one more ('A999')
    // names a sheet that is not in it
    const left = [...new Set(todo.filter((n) => !place(n)).map((n) => n.toLowerCase()))];
    const one = cands.length === 1 ? cands[0] : null;
    if (one && left.length === 1 && all.every((n) => !place(n))) {
      const n = todo.find((x) => x.toLowerCase() === left[0])!;
      const e = extOf(n);
      if (!n.includes(SHEET_SEP.trim()) && (!e || e === extOf(one.path))) placed.set(left[0], { path: one.path, named: n });
    }
  }
  /** rows naming a drawing that was not there before: look again (the visitor may have uploaded it since) */
  function resolveMissing() {
    for (const r of runner.rows) {
      if (!r.missing) continue;
      const p = place(r.missing);
      if (p) { Object.assign(r, { path: p.path, page: p.page, sheet: p.sheet, named: p.named }); r.missing = undefined; }
    }
  }
  let sortKey: SortKey = "id";
  let sortDir = 1;
  let draft: BatchQuestion[] = [];
  /** the questions of an uploaded file, with its expected answers / ids (the text box shows only the questions) */
  let fileDraft: BatchQuestion[] | null = null;
  /** the row whose answer is on the card (highlighted) */
  let shownId = -1;
  /** 'Measure the unmeasured ones' while it walks the rows */
  let measuring: { at: number; of: number; stop: boolean } | null = null;
  /** a picture of where each answer comes from (data URL; null = nothing placed / did not render), per answer object */
  const thumbs = new WeakMap<Answer, string | null>();
  const asked = new WeakSet<Answer>();
  function thumbHtml(r: Row): string {
    const a = r.answer;
    if (!a || !file) return "";
    const t = thumbs.get(a);
    if (t === undefined && !asked.has(a)) {
      asked.add(a);
      void evidenceThumb(a, rowPath(r)).then((url) => { thumbs.set(a, url); schedule(); });
    }
    return t ? `<button type="button" class="f4b-thumb" data-id="${r.id}" title="Where this answer comes from (marked in blue). Click to open it on the drawing"><img alt="where on the drawing" src="${t}"></button>` : "";
  }
  const runner = new BatchRunner({ ask: ctx.ask, concurrency: 2, onChange: () => schedule() });
  host.classList.add("f4b");
  host.innerHTML = `
    <div class="f4b-head">Answered on <b class="f4b-on"></b></div>
    <textarea class="f4b-text" rows="6" spellcheck="false" placeholder="One question per line, in any language. For example:
What is the floor area of the Multi-Purpose Hall?
How many doors are there on this plan?
Kuinka monta ikkunaa on?"></textarea>
    <div class="f4b-row">
      <label class="f4b-file">Or a file <input type="file" accept=".txt,.csv,.tsv,.xlsx,.json,.md,text/plain,text/csv"></label>
      <button type="button" class="f4b-example link" hidden>Try an example list</button>
      <span class="f4b-count"></span>
    </div>
    <div class="f4b-row">
      <button type="button" class="f4b-run primary">Answer all</button>
      <label class="f4b-conc" title="Questions answered side by side (the server has 3 answer workers; 2 leaves one free for your own questions)">at a time
        <select><option>1</option><option selected>2</option><option>3</option></select></label>
      <button type="button" class="f4b-stop" hidden>Stop</button>
      <button type="button" class="f4b-retry" hidden>Ask the failed ones again</button>
      <button type="button" class="f4b-here" hidden title="Your list names a drawing that is not uploaded: ask those questions on the drawing on screen instead (each row says so)">Ask them on this drawing</button>
    </div>
    <div class="f4b-sum" aria-live="polite"></div>
    <div class="f4b-row f4b-tools" hidden>
      <label class="f4b-sort">Sort by <select>
        <option value="id">list order</option><option value="sure">how sure (least first)</option><option value="tier">cost (cheapest first)</option>
        <option value="ms">time (slowest first)</option><option value="status">failed first</option><option value="match">wrong vs expected first</option>
        <option value="answer">answer (largest first)</option><option value="q">question (A–Z)</option></select></label>
      <button type="button" class="f4b-wide link">Wider table</button>
      <button type="button" class="f4b-measure" hidden title="Opens each answer without a measured number on the drawing: the answer card traces the room the question names and measures it at the sheet's scale (you see each outline); the table takes the measured number">Measure the unmeasured ones</button>
    </div>
    <div class="f4b-tablewrap"><table class="f4b-table"><thead></thead><tbody></tbody></table></div>
    <div class="f4b-row f4b-exports" hidden>
      <button type="button" class="f4b-csv">Export CSV</button>
      <button type="button" class="f4b-xlsx">Export Excel</button>
      <button type="button" class="f4b-report" title="One file with every answer next to a picture of where on the drawing it comes from, how sure, the cost and what was assumed: opens in any browser, prints to PDF, can be sent to someone without this app">Report with pictures</button>
      <button type="button" class="f4b-copy" title="Copy the table (tab-separated: pastes into Excel, Sheets or a chat)">Copy table</button>
      <button type="button" class="f4b-clear link">Clear the list</button>
    </div>
    <p class="f4b-note">Each question is answered exactly as if you typed it (same readers, same evidence). The list stays in this browser.
      Several drawings in one list: a <b>drawing</b> column with the file name, or a line starting with <b>[file.pdf]</b>.
      Sheets of one set: a <b>sheet</b> or <b>page</b> column ('A-101', 'Floor Plan - Level 1', '3'), or <b>[file.pdf | A-101]</b>.</p>`;
  const q = <T extends Element>(s: string) => host.querySelector(s) as T;
  const text = q<HTMLTextAreaElement>(".f4b-text");
  const count = q<HTMLElement>(".f4b-count");
  const runBtn = q<HTMLButtonElement>(".f4b-run");
  const stopBtn = q<HTMLButtonElement>(".f4b-stop");
  const retryBtn = q<HTMLButtonElement>(".f4b-retry");
  const hereBtn = q<HTMLButtonElement>(".f4b-here");
  const sum = q<HTMLElement>(".f4b-sum");
  const thead = q<HTMLElement>("thead");
  const tbody = q<HTMLElement>("tbody");
  const exportsRow = q<HTMLElement>(".f4b-exports");
  const conc = q<HTMLSelectElement>(".f4b-conc select");
  const onLine = q<HTMLElement>(".f4b-on");
  const tools = q<HTMLElement>(".f4b-tools");
  const exampleBtn = q<HTMLButtonElement>(".f4b-example");
  /** the count line says which file the list came from (until the visitor edits the text) */
  let fileNote = false;
  const sortSel = q<HTMLSelectElement>(".f4b-sort select");
  const measureBtn = q<HTMLButtonElement>(".f4b-measure");
  const wideBtn = q<HTMLButtonElement>(".f4b-wide");
  // 'Wider table': the side panel grows to the drawing's width (the registry's panel host is 420 px: room for the
  // question and the answer only; wide shows how sure / cost / time / expected as their own sortable columns)
  let wide = false;
  try { wide = localStorage.getItem("f4.batch.wide") === "1"; } catch { /* no storage */ }
  const applyWide = () => {
    host.classList.toggle("f4b-w", wide);
    host.closest(".feat-panels")?.classList.toggle("f4b-wide", wide);
    wideBtn.textContent = wide ? "Narrow table" : "Wider table";
  };
  wideBtn.addEventListener("click", () => {
    wide = !wide;
    try { localStorage.setItem("f4.batch.wide", wide ? "1" : "0"); } catch { /* no storage */ }
    applyWide();
    render();
  });
  sortSel.addEventListener("change", () => {
    sortKey = sortSel.value as SortKey;
    sortDir = sortKey === "ms" || sortKey === "answer" ? -1 : 1;
    render();
  });

  const key = (f: BatchFile | null) => f ? `${STORE}${f.path}|${f.page ?? ""}` : "";
  const target = (f: BatchFile): AskTarget => ({ path: f.path, page: f.page, solver: f.solver });
  function save() {
    if (!file) return;
    try {
      // the answers without their long traces: enough for the table and the export; Show re-asks (cached at the server)
      // outlines dropped too (a 200-question list must fit the ~5 MB storage); boxes and notes stay for the export
      const slim = runner.rows.map((r) => ({ ...r, answer: r.answer ? {
        ...r.answer, trace: (r.answer.trace ?? []).slice(-6),
        evidence: (r.answer.evidence ?? []).slice(0, 12).map((e) => ({ ...e, polygon: undefined, polyline: undefined })),
      } : undefined }));
      localStorage.setItem(key(file), JSON.stringify({ at: Date.now(), draft: text.value, rows: slim, anchor: file }));
      // a list over several drawings: opening any of them (or a reload there) finds this list again
      // (also this drawing's other pages, when the list names pages of it)
      const paged = new Set(runner.rows.filter((r) => r.page !== undefined).map(rowPath));
      for (const p of drawingsOf()) if (p !== file.path || paged.has(p)) localStorage.setItem(`${STORE}@${p}`, key(file));
    } catch { /* full or blocked storage: the list lives in this tab only */ }
  }
  function load() {
    if (!file) return;
    try {
      let j = JSON.parse(localStorage.getItem(key(file)) ?? "null") as { draft?: string; rows?: Row[]; anchor?: BatchFile } | null;
      if (!j?.rows?.length) {
        // this drawing is one of the drawings of a list started on another one
        const k = localStorage.getItem(`${STORE}@${file.path}`);
        const o = k ? JSON.parse(localStorage.getItem(k) ?? "null") as typeof j : null;
        if (o?.rows?.some((r) => r.path === file!.path) && o.anchor?.path) { j = o; file = o.anchor; }
      }
      if (j?.rows?.length) runner.restore(target(file), j.rows);
      else runner.restore(target(file), []);
      text.value = j?.draft ?? "";
    } catch {
      runner.restore(target(file), []);
    }
    onDraft();
  }

  function onDraft() {
    try {
      draft = parseText(text.value);
    } catch { draft = []; }
    const extra = draft.length > MAX_QUESTIONS ? ` (the first ${MAX_QUESTIONS} are used)` : "";
    // an e-mail pasted whole: its greeting / sign-off / 'Our questions:' lines are left out, and the count says so
    let skipped: string[] = [];
    try { skipped = droppedLines(text.value); } catch { /* none */ }
    const left = skipped.length ? ` · left out ${skipped.length} line${skipped.length > 1 ? "s" : ""} that ${skipped.length > 1 ? "are" : "is"} not a question ('${skipped[0].slice(0, 40)}'${skipped.length > 1 ? ", …" : ""})` : "";
    if (!fileNote) count.textContent = draft.length ? `${draft.length} question${draft.length > 1 ? "s" : ""}${extra}${left}` : "";
    // in the list already: answered, failed ('Ask the failed ones again' / 'try again' on its row) or skipped
    const where = (r: Row) => r.missing ? `?${r.missing.toLowerCase()}` : placeKey({ path: rowPath(r), page: r.page });
    const inList = new Set(runner.rows.filter((r) => r.status === "done" || r.status === "error" || r.status === "skipped").map((r) => `${where(r)}|${r.q.toLowerCase()}`));
    const here = onScreen?.path ?? file?.path ?? "";
    const at = (d: BatchQuestion) => { if (!d.drawing) return here; const p = place(d.drawing); return p ? placeKey(p) : `?${d.drawing.toLowerCase()}`; };
    const fresh = draft.slice(0, MAX_QUESTIONS).filter((d) => !inList.has(`${at(d)}|${d.q.toLowerCase()}`)).length;
    const failed = runner.rows.some((r) => r.status === "error");
    const waiting = runner.rows.filter((r) => r.status === "queued").length;
    const pl = (n: number) => `${n} question${n === 1 ? "" : "s"}`;
    runBtn.textContent = fresh && fresh < draft.length ? `Answer ${fresh} more question${fresh === 1 ? "" : "s"}` : fresh ? `Answer ${pl(fresh)}`
      : waiting ? `Continue: ${waiting} waiting` : draft.length ? (failed ? "All asked" : "All answered") : "Answer all";
    runBtn.disabled = !file || (!fresh && !waiting);
    exampleBtn.hidden = !!text.value.trim() || runner.rows.length > 0;
  }

  text.addEventListener("input", () => { fileNote = false; onDraft(); save(); });
  async function loadFile(f: File) {
    try {
      if (f.size > 5 << 20) throw new Error("it is larger than 5 MB");
      const qs = /\.xlsx$/i.test(f.name) ? fromRows(await readXlsx(new Uint8Array(await f.arrayBuffer()))) : parseFileText(f.name, await f.text());
      if (!qs.length) throw new Error("no questions found in it");
      // keep the file's expected answers / ids: they go straight to the list (the text box shows the questions)
      // a list naming its drawings keeps them in the box as '[drawing] question' (the same form can be typed)
      text.value = toLines(qs);
      fileDraft = qs;
      onDraft();
      // the drawings it names, as found among the drawings open / uploaded in this visit (sheet names: after the sheet
      // index of the PDFs is read, a few seconds; the note is updated then)
      const note = () => {
        const ps = qs.filter((x) => x.drawing).map((x) => place(x.drawing!));
        const nd = new Set(ps.filter(Boolean).map((p) => p!.path)).size, nm = new Set(qs.filter((x) => x.drawing && !place(x.drawing)).map((x) => x.drawing!.toLowerCase())).size;
        const sheets = new Set(ps.filter((p) => p?.page !== undefined).map((p) => placeKey(p!))).size;
        const alias = ps.find((p) => p?.named)?.named;
        count.textContent = `${qs.length} question${qs.length > 1 ? "s" : ""} from ${f.name}${ps.length ? ` about ${nd} of your drawing${nd === 1 ? "" : "s"}${sheets ? ` (${sheets} sheet${sheets === 1 ? "" : "s"} of a set)` : ""}${alias ? ` (it calls the drawing on screen '${alias}')` : ""}${nm ? ` + ${nm} not uploaded yet (upload ${nm === 1 ? "it" : "them"} first)` : ""}` : ""}${qs.some((x) => x.expected) ? " (with your expected answers)" : ""}`;
      };
      note();
      fileNote = true;
      save();
      const names = qs.filter((x) => x.drawing).map((x) => x.drawing!);
      if (names.some((n) => !place(n))) void resolvePlaces(names).then(() => { if (fileNote && fileDraft === qs) { note(); onDraft(); } }).catch(() => {});
    } catch (err) {
      count.textContent = `Could not read ${f.name}: ${(err as Error).message ?? err}`;
      fileNote = true;
    }
  }
  q<HTMLInputElement>(".f4b-file input").addEventListener("change", (e) => {
    const inp = e.target as HTMLInputElement;
    const f = inp.files?.[0];
    inp.value = "";
    if (f) void loadFile(f);
  });
  // a question file dropped anywhere on the panel (not on the drawing: that is the app's own upload)
  host.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer?.types ?? [])].includes("Files")) return;
    e.preventDefault();
    e.stopPropagation();
    host.classList.add("f4b-drop");
  });
  host.addEventListener("dragleave", (e) => { if (!host.contains(e.relatedTarget as Node)) host.classList.remove("f4b-drop"); });
  host.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    host.classList.remove("f4b-drop");
    if (!f) return;
    e.preventDefault();
    e.stopPropagation();
    if (/\.(txt|csv|tsv|tab|xlsx|json|md)$/i.test(f.name) || f.type.startsWith("text/")) void loadFile(f);
    else count.textContent = `${f.name} is not a question list (.txt, .csv, .xlsx or .json). To ask about a new drawing, drop it on the drawing area.`;
  });
  exampleBtn.addEventListener("click", () => {
    text.value = EXAMPLES.join("\n");
    fileNote = false;
    onDraft();
    save();
    text.focus();
  });

  /** set while the sheet index of the drawings is read before a run (a list naming sheets of a set) */
  let placing = false;
  runBtn.addEventListener("click", async () => {
    if (!file || placing) return;
    const here = onScreen ?? file;
    const typed = parseText(text.value);
    // a file's rows carry expected answers / ids: kept for every question still in the box (edited lines lose theirs)
    const dk = (x: { q: string; drawing?: string }) => `${(x.drawing ?? "").toLowerCase()}|${x.q.toLowerCase()}`;
    const fromFile = new Map((fileDraft ?? []).map((x) => [dk(x), x]));
    const qs = typed.map((x) => ({ ...fromFile.get(dk(x)), ...x })).slice(0, MAX_QUESTIONS);
    // names no file is called ('A-101', 'page 3', 'Level 00 plan'): read the sheet index first (1-7 s, once per file)
    const names = qs.filter((x) => x.drawing).map((x) => x.drawing!);
    if (names.some((n) => !place(n))) {
      placing = true;
      runBtn.disabled = true;
      runBtn.textContent = "Finding the sheets your list names…";
      try { await resolvePlaces(names); } catch { /* not placed: 'not uploaded' rows */ } finally { placing = false; }
      if (!file) return;
    }
    if (qs.length) {
      const rk = (r: { q: string; path?: string; page?: number }) => `${placeKey({ path: r.path ?? file!.path, page: r.page })}|${r.q.toLowerCase()}`;
      const old = new Map(runner.rows.filter((r) => r.status === "done").map((r) => [rk(r), r]));
      // every row carries its drawing: the one its line names (never a guess between two), else the one on screen
      runner.load(target(file), qs.map((x) => {
        if (!x.drawing) return { ...x, path: here.path };
        const p = place(x.drawing);
        return p ? { ...x, ...p } : { ...x, missing: x.drawing };
      }));
      // a question already answered in this list keeps its answer (asking it again = 'again' on its row)
      for (const r of runner.rows) {
        const o = old.get(rk(r));
        if (o) Object.assign(r, { status: o.status, answer: o.answer, ms: o.ms, tries: o.tries, was: o.was, restored: o.restored });
      }
    }
    runner.setConcurrency(Number(conc.value));
    runner.start();
  });
  stopBtn.addEventListener("click", () => runner.stop());
  retryBtn.addEventListener("click", () => { resolveMissing(); runner.retryFailed(); });
  // rows naming a drawing that is not here: the visitor says the list means the drawing on screen
  hereBtn.addEventListener("click", () => {
    const h = onScreen ?? file;
    if (!h) return;
    for (const r of runner.rows) {
      if (!r.missing) continue;
      Object.assign(r, { path: h.path, named: r.missing, missing: undefined, page: undefined, sheet: undefined, status: "queued", tries: 0, error: undefined, retryable: undefined });
    }
    runner.start();
  });
  conc.addEventListener("change", () => runner.setConcurrency(Number(conc.value)));
  q<HTMLButtonElement>(".f4b-csv").addEventListener("click", () => exportAs("csv"));
  q<HTMLButtonElement>(".f4b-xlsx").addEventListener("click", () => exportAs("xlsx"));
  // the report: the export's cells + a larger picture of each answer's evidence (drawn one at a time; cached by place)
  const reportBtn = q<HTMLButtonElement>(".f4b-report");
  let reporting = false;
  reportBtn.addEventListener("click", async () => {
    if (!file || !runner.rows.length || reporting) return;
    reporting = true;
    const label = reportBtn.textContent;
    try {
      syncCard();
      const f = file;
      const rows = sorted();
      const head = exportHeader(rows.some((r) => r.expected));
      const body = exportRows(rows, f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined);
      const pics: (string | null)[] = [];
      for (let i = 0; i < rows.length; i++) {
        reportBtn.textContent = `Drawing the pictures… ${i + 1} of ${rows.length}`;
        const r = rows[i];
        pics.push(r.status === "done" && r.answer ? await evidenceThumb(r.answer, rowPath(r), 380, 230).catch(() => null) : null);
      }
      const ds = drawingsOf().map((p) => fileOf(p).name ?? baseName(p));
      const html = reportHtml(head, body, pics, {
        title: `Answers about ${ds.slice(0, 3).join(", ")}${ds.length > 3 ? ` and ${ds.length - 3} more` : ""}`,
        summary: sum.textContent ?? "",
        made: `Made ${new Date().toLocaleString()} · ${rows.length} question${rows.length === 1 ? "" : "s"}`,
        note: "Pictures are crops of the drawings the questions were asked on.",
      });
      download(`${baseName2(f)}.html`, html, "text/html;charset=utf-8");
    } finally {
      reporting = false;
      reportBtn.textContent = label;
    }
  });
  const copyBtn = q<HTMLButtonElement>(".f4b-copy");
  copyBtn.addEventListener("click", async () => {
    if (!file || !runner.rows.length) return;
    syncCard();
    const f = file;
    const head = exportHeader(runner.rows.some((r) => r.expected));
    const body = exportRows(sorted(), f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined);
    try {
      await navigator.clipboard.writeText(toTsv(head, body));
      copyBtn.textContent = "Copied";
    } catch {
      copyBtn.textContent = "Copy blocked: use Export";
    }
    setTimeout(() => (copyBtn.textContent = "Copy table"), 2000);
  });
  q<HTMLButtonElement>(".f4b-clear").addEventListener("click", () => {
    runner.restore(file ? target(file) : { path: "" }, []);
    fileDraft = null;
    fileNote = false;
    text.value = "";
    save();
    // a list over several drawings was cleared while another of them is on screen: the next list belongs to that one
    if (onScreen && file && onScreen.path !== file.path) {
      file = onScreen;
      runner.restore(target(file), []);
      save();
    }
  });

  /** 'answers-<drawing>-20261114-2150' */
  function baseName2(f: BatchFile) {
    const d = new Date(), p2 = (x: number) => String(x).padStart(2, "0");
    return `answers-${(f.name ?? f.path.split("/").pop() ?? "drawing").replace(/\.[^.]+$/, "").replace(/[^\w.-]+/g, "_").slice(0, 40)}-${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}`;
  }
  function exportAs(kind: "csv" | "xlsx") {
    if (!file || !runner.rows.length) return;
    syncCard();
    const f = file;
    const head = exportHeader(runner.rows.some((r) => r.expected));
    const body = exportRows(sorted(), f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined);
    const base = baseName2(f);
    if (kind === "csv") download(`${base}.csv`, toCsv(head, body), "text/csv;charset=utf-8");
    else download(`${base}.xlsx`, xlsxBytes(head, body, { sheet: "Answers", widths: head.map((h) => (h === "question" ? 50 : h === "evidence" || h === "open again" ? 40 : h === "assumptions" ? 30 : 12)) }) as BlobPart,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
  }

  function sorted(): Row[] {
    const rows = [...runner.rows];
    const val = (r: Row): number | string => {
      const s = shownOf(r, rowPath(r));
      switch (sortKey) {
        case "q": return r.q.toLowerCase();
        case "answer": return s.num ?? (s.text ? Number.MAX_VALUE : Infinity);
        case "sure": return r.answer ? (s.guess ? -1 : r.answer.confidence ?? 0) : -2;
        case "tier": return r.answer ? TIER_RANK[s.tierCls as keyof typeof TIER_RANK] ?? 0 : 9;
        case "ms": return solverMs(r) ?? (r.started ? Date.now() - r.started : Infinity);
        case "status": return ["error", "running", "queued", "skipped", "done"].indexOf(r.status);
        case "match": return { no: 0, yes: 1, "": 2 }[matchExpected(r.expected, effective(r))];
        default: return r.id;
      }
    };
    return rows.sort((a, b) => {
      const x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : a.id - b.id) * sortDir;
    });
  }

  let pending = 0;
  function schedule() {
    if (pending) return;
    pending = requestAnimationFrame(() => {
      pending = 0;
      render();
      save();
      try {
        ctx.progress?.(runner.rows.filter((r) => r.status === "done" || r.status === "error").length, runner.rows.length, runner.isRunning);
      } catch { /* a badge must not stop the table */ }
    });
  }

  function render() {
    const rows = runner.rows;
    const hasExp = rows.some((r) => r.expected);
    const done = rows.filter((r) => r.status === "done");
    const failed = rows.filter((r) => r.status === "error");
    const busy = rows.filter((r) => r.status === "running").length;
    const waiting = rows.filter((r) => r.status === "queued").length;
    const ds = drawingsOf();
    const isMulti = multi();
    const nSheets = new Set(rows.filter((r) => r.page !== undefined).map((r) => placeKey({ path: rowPath(r), page: r.page }))).size;
    const alias = [...new Set(rows.filter((r) => r.named).map((r) => r.named!))];
    onLine.textContent = !file ? "no drawing: open one first"
      : (isMulti ? `${ds.length} drawing${ds.length > 1 ? "s" : ""}: ${ds.map((p) => fileOf(p).name ?? baseName(p)).join(", ")}${nSheets ? ` (${nSheets} sheet${nSheets > 1 ? "s" : ""} your list names)` : ""}${rows.some((r) => r.missing) ? " (and some not uploaded)" : ""}`
      : `${file.name ?? file.path.split("/").pop()}${file.page !== undefined && (file.pages ?? 0) > 1 ? `, page ${file.page + 1}` : ""}`)
      + (alias.length ? ` · your list calls it ${alias.slice(0, 2).map((a) => `'${a}'`).join(", ")}${alias.length > 2 ? " …" : ""}` : "");
    applyWide();
    stopBtn.hidden = !runner.isRunning;
    runBtn.hidden = runner.isRunning && !draft.length;
    retryBtn.hidden = runner.isRunning || !failed.length;
    const nMissing = rows.filter((r) => r.missing).length;
    hereBtn.hidden = runner.isRunning || !nMissing || !(onScreen ?? file);
    hereBtn.textContent = `Ask the ${nMissing === 1 ? "one" : nMissing} not uploaded on ${(onScreen ?? file)?.name ?? "this drawing"}`;
    exportsRow.hidden = !rows.length;
    host.classList.toggle("f4b-has", rows.length > 0);
    const unmeasured = done.filter((r) => !r.card && shownOf(r, rowPath(r)).guess && /\.(pdf|png|jpe?g|webp|tiff?)$/i.test(rowPath(r)));
    measureBtn.hidden = !ctx.readCard || (!unmeasured.length && !measuring);
    measureBtn.textContent = measuring ? `Stop measuring (${measuring.at} of ${measuring.of})` : `Measure the ${unmeasured.length} unmeasured on the drawing`;
    measureBtn.disabled = runner.isRunning && !measuring;
    tools.hidden = rows.length < 2 && measureBtn.hidden;
    if (sortSel.value !== sortKey) sortSel.value = sortKey;
    sortSel.querySelector<HTMLOptionElement>('option[value="match"]')!.hidden = !hasExp;
    if (!rows.length) { sum.innerHTML = ""; thead.innerHTML = ""; tbody.innerHTML = ""; onDraft(); return; }
    const med = median(done.map((r) => solverMs(r) ?? 0));
    const took = runner.runStart && rows.length > 1 ? (runner.runEnd || Date.now()) - runner.runStart : 0;
    const tiers = { cheap: 0, mid: 0, high: 0 } as Record<string, number>;
    for (const r of done) if (r.answer) tiers[costTier(r.answer).tier]++;
    const guesses = done.filter((r) => shownOf(r, rowPath(r)).guess || (r.answer?.value ?? null) === null).length;
    const matched = hasExp ? rows.filter((r) => matchExpected(r.expected, effective(r)) === "yes").length : 0;
    const checked = hasExp ? rows.filter((r) => matchExpected(r.expected, effective(r))).length : 0;
    sum.innerHTML = `<b>${done.length} of ${rows.length} answered</b>`
      + (busy ? ` · ${busy} answering now` : "") + (waiting ? ` · ${waiting} waiting` : "")
      + (failed.length ? ` · <span class="f4b-bad">${failed.length} failed</span>` : "")
      + (med !== null ? ` · median ${fmtS(med)} per answer` : "")
      + (took && !runner.isRunning && done.length ? ` · the whole list took ${fmtS(took)}` : "")
      + (done.length ? ` · cost: ${tiers.cheap} cheap, ${tiers.mid} in-between, ${tiers.high} expensive` : "")
      + (guesses ? ` · ${guesses} without a measured number` : "")
      + (checked ? ` · <b>${matched} of ${checked} match your expected answers</b>` : "");
    const th = (k: SortKey, label: string, title = "") => `<th data-k="${k}" tabindex="0" aria-sort="${sortKey === k ? (sortDir > 0 ? "ascending" : "descending") : "none"}" class="${sortKey === k ? (sortDir > 0 ? "asc" : "desc") : ""}"${title ? ` title="${esc(title)}"` : ""}>${label}</th>`;
    thead.innerHTML = wide
      ? `<tr>${th("id", "#")}${th("q", "Question")}${th("answer", "Answer")}${th("sure", "How sure")}${th("tier", "Cost", COST_HINT)}${th("ms", "Time")}${hasExp ? th("match", "Expected") : ""}<th></th></tr>`
      : `<tr>${th("id", "#")}${th("q", "Question")}${th("answer", "Answer")}<th></th></tr>`;
    tbody.innerHTML = sorted().map((r) => rowHtml(r, hasExp, isMulti)).join("");
    onDraft();
  }

  function rowHtml(r: Row, hasExp: boolean, isMulti: boolean): string {
    const s = shownOf(r, rowPath(r));
    let ans = "", sure = "", tier = "", time = "", act = "";
    if (r.status === "done" && r.answer) {
      const reader = r.card ? (() => { const x = shownOf({ ...r, card: undefined }, rowPath(r)); return `${x.guess && x.num !== null ? "≈ " : ""}${x.text}${x.unit ? ` ${x.unit}` : ""}`; })() : "";
      ans = r.card
        ? `<span class="f4b-val">${esc(r.card.big)}</span>${r.card.unit ? ` <span class="f4b-unit">${esc(r.card.unit)}</span>` : ""} <small class="f4b-was" title="${esc(r.card.sentence ?? "measured on the drawing")}">measured on the drawing · the reader said ${esc(reader)}</small>`
        : `<span class="f4b-val${s.guess ? " guess" : ""}">${s.guess && s.num !== null ? "≈ " : ""}${esc(s.text)}</span>${s.unit ? ` <span class="f4b-unit">${esc(s.unit)}</span>` : ""}`;
      ans += r.was ? ` <small class="f4b-was" title="Changed on the answer card after Show (your scale, an option you picked or another reader)">was ${esc(r.was)}</small>` : "";
      sure = `<span class="f4b-conf ${s.cls}">${esc(s.sure)}</span>`;
      tier = `<span class="f4b-tier t-${s.tierCls}" title="${esc(s.tierWhy)}">${esc(s.tier)}</span>`;
      const own = solverMs(r);
      const wait = r.ms !== undefined && own !== undefined && r.ms - own > 2000 ? r.ms - own : 0;
      time = `<span title="${esc(r.answer.cached ? "answered before: the server kept it" : `the readers took ${fmtS(own)}${wait ? `; it waited ${fmtS(wait)} behind the list's other questions` : ""}`)}">${fmtS(own)}</span>${r.answer.cached ? ` <small>cached</small>` : ""}`;
      act = `<button type="button" class="f4b-show primary" data-id="${r.id}" title="Open this answer with its evidence on the drawing">Show</button>${wide ? " " : "<br>"}<button type="button" class="f4b-again link" data-id="${r.id}" title="Ask this one again">again</button>`;
    } else if (r.status === "running") {
      ans = `<span class="f4b-wait"><i class="f4b-spin"></i>answering…${r.tries > 1 ? ` (try ${r.tries})` : ""}</span>`;
      time = fmtS(r.started ? Date.now() - r.started : 0);
      if (r.error) ans += `<br><small class="f4b-bad">${esc(r.error.slice(0, 90))} · trying again</small>`;
    } else if (r.status === "queued") {
      ans = `<span class="f4b-muted">waiting</span>`;
      act = `<button type="button" class="f4b-skip link" data-id="${r.id}">skip</button>`;
    } else if (r.status === "skipped") {
      ans = `<span class="f4b-muted">skipped</span>`;
      act = `<button type="button" class="f4b-again link" data-id="${r.id}">ask</button>`;
    } else {
      ans = `<span class="f4b-bad">Failed</span> <small>${esc((r.error ?? "").slice(0, 140))}</small>`;
      time = fmtS(r.ms);
      act = `<button type="button" class="f4b-again link" data-id="${r.id}">try again</button>`;
    }
    const m = matchExpected(r.expected, effective(r));
    const mark = m ? ` <b class="${m === "yes" ? "f4b-ok" : "f4b-bad"}" title="${m === "yes" ? "matches your expected answer (counts exact, sizes within 2 %)" : "differs from your expected answer"}">${m === "yes" ? "✓" : "✗"}</b>` : "";
    const sheet = r.page !== undefined ? `p. ${r.page + 1}${r.sheet && r.sheet !== `p. ${r.page + 1}` ? ` · ${r.sheet}` : ""}` : r.sheet ?? "";
    const dr = isMulti ? `<div class="f4b-dr">${r.missing ? `<span class="f4b-bad">${esc(r.missing)}: ${r.missing.includes(SHEET_SEP.trim()) && place(splitName(r.missing)[0]) ? "no such sheet in it" : "not uploaded"}</span>`
      : drawingsOf().length > 1 || !sheet ? `on ${esc(fileOf(rowPath(r)).name ?? baseName(rowPath(r)))}${sheet ? `, ${esc(sheet)}` : ""}` : `on ${esc(sheet)}`}${r.named && r.page === undefined ? ` <small title="the name your list gives this drawing">(your list: ${esc(r.named)})</small>` : ""}</div>` : "";
    const n = `<td class="f4b-n">${r.ref ? esc(r.ref) : runner.rows.indexOf(r) + 1}</td><td class="f4b-q">${esc(r.q)}${dr}</td>`;
    if (wide) {
      const exp = hasExp ? `<td class="f4b-exp">${esc(r.expected ?? "")}${mark}</td>` : "";
      return `<tr class="st-${r.status}${r.id === shownId ? " shown" : ""}" data-id="${r.id}">${n}<td class="f4b-a">${ans}${thumbHtml(r)}</td><td>${sure}</td><td>${tier}</td><td class="f4b-t">${time}</td>${exp}<td class="f4b-act">${act}</td></tr>`;
    }
    // narrow: how sure · cost · time · expected on one small line under the answer
    const meta = [sure, tier, time && `<span class="f4b-t">${time}</span>`, r.expected ? `<span class="f4b-exp">expected ${esc(r.expected)}${mark}</span>` : ""].filter(Boolean).join(" ");
    return `<tr class="st-${r.status}${r.id === shownId ? " shown" : ""}" data-id="${r.id}">${n}<td class="f4b-a">${ans}${meta ? `<div class="f4b-meta">${meta}</div>` : ""}${thumbHtml(r)}</td><td class="f4b-act">${act}</td></tr>`;
  }

  thead.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    (e.target as HTMLElement).click();
  });
  thead.addEventListener("click", (e) => {
    const k = (e.target as HTMLElement).closest<HTMLElement>("th[data-k]")?.dataset.k as SortKey | undefined;
    if (!k) return;
    if (k === sortKey) sortDir = -sortDir;
    else { sortKey = k; sortDir = k === "ms" || k === "answer" ? -1 : 1; }
    render();
    thead.querySelector<HTMLElement>(`th[data-k="${k}"]`)?.focus();
  });
  tbody.addEventListener("click", async (e) => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-id]");
    if (!b) return;
    const id = Number(b.dataset.id);
    const r = runner.rows.find((x) => x.id === id);
    if (!r) return;
    if (b.classList.contains("f4b-skip")) return runner.skip(id);
    if (b.classList.contains("f4b-again")) { resolveMissing(); return runner.rerun(id); }
    if ((b.classList.contains("f4b-show") || b.classList.contains("f4b-thumb")) && r.answer && file) {
      shownId = id;
      // the wide table covers the drawing: back to the side panel so the marked evidence is visible (not remembered)
      if (wide) { wide = false; applyWide(); }
      render();
      const f = fileOf(rowPath(r));
      // a row asked on another drawing of the list (or another page it names): open it first (the list stays)
      try { await goTo(r); } catch (err) {
        sum.insertAdjacentHTML("beforeend", ` · <span class="f4b-bad">could not open ${esc(f.name ?? baseName(f.path))}: ${esc((err as Error).message ?? err)}</span>`);
        return;
      }
      if (ctx.show) await ctx.show(f, r.q, r.answer, !r.restored, r.page);
      else showViaPage(r.q);
    }
  });

  /** the row's drawing (and the page its list names) on screen, opening / turning to it when needed */
  async function goTo(r: Row) {
    if (!ctx.open) return;
    const p = rowPath(r);
    if (onScreen?.path !== p || (r.page !== undefined && (onScreen?.page ?? 0) !== r.page)) await ctx.open(p, r.page);
  }

  // --- the card's number back into the row (W5's takeoff does the same with the card's DOM: takeoff.ts updateShown) ---
  /** the shown row takes what the card shows for it, when that differs from the reader's number */
  function syncCard(): boolean {
    const c = ctx.readCard?.();
    const r = runner.rows.find((x) => x.id === shownId);
    if (!c || !r || r.status !== "done" || !r.answer) return false;
    if (c.question.trim().toLowerCase() !== r.q.trim().toLowerCase() || c.path !== rowPath(r)) return false;
    if (/…|\.\.\.$/.test(c.big)) return false; // still measuring
    const mine = shownOf({ ...r, card: undefined }, rowPath(r));
    // only a measurement counts: the card measured a guess (its trace), or shows another number in the SAME unit (the
    // visitor's scale); the same quantity written in another unit (mm on the row, m on the card) is not a new reading
    const unitOf = (u: string) => u.trim().split(/\s+/)[0].toLowerCase();
    if (!mine.guess && unitOf(c.unit) !== unitOf(mine.unit)) return false;
    const v = firstNumber(c.big);
    const same = v !== null && mine.num !== null ? Math.abs(v - mine.num) <= 0.005 * Math.max(Math.abs(mine.num), 1e-9) && !(mine.guess && !/≈/.test(c.big))
      : c.big.replace(/^≈\s*/, "") === mine.text;
    const next: CardValue | undefined = same || v === null ? undefined : { big: c.big, unit: c.unit, sentence: c.sentence, sure: c.sure };
    if (JSON.stringify(next) === JSON.stringify(r.card)) return false;
    r.card = next;
    schedule();
    return true;
  }
  let cardTimer = 0;
  const unwatch = ctx.watchCard?.(() => { window.clearTimeout(cardTimer); cardTimer = window.setTimeout(syncCard, 350); }) ?? (() => {});

  /** 'Measure the unmeasured ones': each answer without a measured number is shown in turn; the card traces the room
   *  its question names and measures it (visible on the drawing), the row takes the number. One at a time; Stop. */
  measureBtn.addEventListener("click", async () => {
    if (measuring) { measuring.stop = true; return; }
    const todo = runner.rows.filter((r) => r.status === "done" && !r.card && shownOf(r, rowPath(r)).guess && /\.(pdf|png|jpe?g|webp|tiff?)$/i.test(rowPath(r)));
    if (!todo.length || !ctx.show) return;
    measuring = { at: 0, of: todo.length, stop: false };
    if (wide) { wide = false; applyWide(); }
    let got = 0;
    for (const r of todo) {
      if (measuring.stop) break;
      measuring.at++;
      render();
      shownId = r.id;
      const f = fileOf(rowPath(r));
      try {
        await goTo(r);
        await ctx.show(f, r.q, r.answer!, !r.restored, r.page);
      } catch { continue; }
      // the card traces and measures in a few seconds (a scale estimated from door swings first: longer); settle when
      // its number has stayed the same for 1.2 s, give up after 25 s
      let last = "", since = Date.now();
      const t0 = Date.now();
      while (!measuring.stop && Date.now() - t0 < 25_000) {
        await new Promise((res) => setTimeout(res, 400));
        const c = ctx.readCard?.();
        const now = c ? `${c.big}|${c.unit}` : "";
        if (now !== last) { last = now; since = Date.now(); continue; }
        if (syncCard() || (r.card && Date.now() - since > 1200)) break;
        if (Date.now() - since > 8000) break; // the card is settled and shows the reader's number: nothing to measure
      }
      if (r.card) got++;
    }
    const of = measuring.of;
    measuring = null;
    render();
    sum.insertAdjacentHTML("beforeend", ` · <b>measured ${got} of ${of} on the drawing</b>`);
  });

  // the running rows' clocks
  const tick = window.setInterval(() => { if (runner.rows.some((r) => r.status === "running")) render(); }, 1000);

  load();
  render();
  return {
    runner,
    /** the drawing on screen changed: the list of that drawing (and page) comes back */
    refresh() {
      const f = ctx.file();
      if (key(f) === key(file)) { file = f; onScreen = f; render(); return; }
      // another drawing of this list (Show of a row asked there, or the visitor flips to it): the list stays and runs on
      if (f && file && f.path !== file.path && runner.rows.some((r) => rowPath(r) === f.path)) { onScreen = f; render(); return; }
      // another page of a set whose pages the list names (Show of a row on that sheet): the list stays
      if (f && file && runner.rows.some((r) => r.page !== undefined && rowPath(r) === f.path)) { onScreen = f; render(); return; }
      runner.stop();
      file = f;
      onScreen = f;
      fileDraft = null;
      load();
      render();
    },
    /** an answer shown on the card (after Show, the visitor set the scale / picked an option / chose another reader):
     *  the row on the card takes it, and says what it had before */
    cardAnswer(a: Answer, question: string, path?: string) {
      const r = runner.rows.find((x) => x.id === shownId);
      if (!r || r.status !== "done" || !r.answer || r.answer === a) return;
      if (r.q.trim().toLowerCase() !== question.trim().toLowerCase() || (path && rowPath(r) !== path)) return;
      const shown = (x: Row) => { const v = shownOf(x, rowPath(x)); return `${v.guess && v.num !== null ? "≈ " : ""}${v.text}${v.unit ? ` ${v.unit}` : ""}`; };
      const before = shown(r);
      const same = r.answer.value === a.value && r.answer.unit === a.unit;
      if (same && !r.restored) return;
      r.answer = a;
      r.restored = undefined;
      if (!same) {
        const now = shown(r);
        r.was = r.was === now ? undefined : r.was ?? before;
      }
      schedule();
    },
    destroy() { unwatch(); if (measuring) measuring.stop = true; window.clearInterval(tick); runner.stop(); host.innerHTML = ""; host.classList.remove("f4b"); },
  };
}

/** Without a registry show(): put the question in the page's own question box and ask it (the server has it cached, so
 *  the answer card and its evidence come back at once). */
function showViaPage(question: string) {
  const box = document.getElementById("question") as HTMLTextAreaElement | null;
  const btn = document.getElementById("askBtn") as HTMLButtonElement | null;
  if (!box || !btn) return;
  box.value = question;
  box.dispatchEvent(new Event("input", { bubbles: true }));
  btn.click();
}

let cssDone = false;
function injectCss() {
  if (cssDone || typeof document === "undefined") return;
  cssDone = true;
  const st = document.createElement("style");
  st.dataset.feature = "batch";
  st.textContent = `
.f4b { display: flex; flex-direction: column; gap: 8px; font-size: 13.5px; color: var(--ink, #1d1d1f); }
.f4b-head { font-size: 13.5px; color: var(--muted, #6e6e73); } .f4b-on { color: var(--ink, #1d1d1f); font-weight: 600; }
.feat-panels.f4b-wide:has(> .feat-panel[data-feature="batch"]:not([hidden])) { width: min(980px, calc(100% - 32px)); }
.f4b-meta { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; font-size: 12px; color: var(--muted, #6e6e73); }
.f4b-sort, .f4b-sort select { color: var(--muted, #6e6e73); font-size: 12.5px; } .f4b-sort select { border-radius: 8px; padding: 2px 4px; }
.f4b:not(.f4b-w) .f4b-a { min-width: 104px; } .f4b:not(.f4b-w) .f4b-n { font-size: 11.5px; max-width: 42px; overflow-wrap: anywhere; }
.f4b-act .f4b-show { padding: 3px 12px; }
.f4b-thumb { display: block; margin-top: 6px; padding: 0; border: 1px solid var(--line-strong, #ccc); border-radius: 8px; overflow: hidden; background: #fff; cursor: zoom-in; max-width: 100%; }
.f4b-thumb img { display: block; width: 176px; max-width: 100%; height: auto; }
.f4b.f4b-drop { outline: 2px dashed var(--accent, #0071e3); outline-offset: 4px; border-radius: 12px; }
.f4b-text { width: 100%; box-sizing: border-box; font: inherit; padding: 8px 10px; border-radius: 10px; border: 1px solid var(--line-strong, #ccc); background: var(--panel, #fff); color: inherit; resize: vertical; min-height: 96px; }
.f4b-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.f4b-row button, .f4b-act button, .f4b-conc select { font: inherit; border-radius: 999px; padding: 5px 12px; border: 1px solid var(--line-strong, #ccc); background: var(--panel, #fff); color: inherit; cursor: pointer; }
.f4b button.primary { background: var(--accent, #0071e3); border-color: transparent; color: #fff; font-weight: 600; }
.f4b button:disabled { opacity: .5; cursor: default; }
.f4b button.link { border: 0; background: none; color: var(--accent-ink, #0062c4); padding: 2px 4px; text-decoration: underline; }
.f4b-file { color: var(--muted, #6e6e73); } .f4b-file input { font-size: 12.5px; max-width: 220px; }
.f4b-count, .f4b-conc, .f4b-note, .f4b-muted { color: var(--muted, #6e6e73); } .f4b-note { font-size: 12px; margin: 0; }
.f4b-sum { line-height: 1.5; }
.f4b-tablewrap { overflow: auto; max-height: 58vh; min-height: min(260px, 42vh); flex: 1 0 auto; border: 1px solid var(--line, #eee); border-radius: 10px; }
.f4b-tablewrap:has(tbody:empty) { min-height: 0; border: 0; }
/* smaller while a list is shown; never tied to :focus (a box that shrinks on blur moves the button away between mousedown and mouseup: QA 21:19, the click was lost) */
.f4b.f4b-has .f4b-text { min-height: 58px; height: 80px; flex-shrink: 0; }
.f4b-table { border-collapse: collapse; width: 100%; }
.f4b-table th, .f4b-table td { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid var(--line, #eee); }
.f4b-table th { position: sticky; top: 0; background: var(--panel, #fff); cursor: pointer; user-select: none; white-space: nowrap; font-weight: 600; z-index: 1; }
.f4b-table th.asc::after { content: " ▲"; font-size: 10px; } .f4b-table th.desc::after { content: " ▼"; font-size: 10px; }
.f4b-table tr.shown td { background: var(--accent-soft, rgba(0,113,227,.14)); }
.f4b-n { color: var(--muted, #6e6e73); } .f4b-w .f4b-q { min-width: 220px; }
.f4b:not(.f4b-w) .f4b-table th, .f4b:not(.f4b-w) .f4b-table td { padding: 6px 5px; } .f4b:not(.f4b-w) .f4b-act { width: 1%; } .f4b-t { white-space: nowrap; } .f4b-act { white-space: nowrap; }
.f4b-val { font-weight: 650; font-variant-numeric: tabular-nums; } .f4b-val.guess { color: var(--muted, #6e6e73); font-weight: 500; }
.f4b-unit { color: var(--muted, #6e6e73); }
.f4b-dr { margin-top: 2px; font-size: 11.5px; color: var(--muted, #6e6e73); overflow-wrap: anywhere; } .f4b-was { color: var(--muted, #6e6e73); font-weight: 400; }
.f4b-conf, .f4b-tier { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; background: var(--fill, rgba(118,118,128,.12)); }
.f4b-conf.sure { background: var(--ok-soft); color: var(--ok); } .f4b-conf.fair { background: var(--warn-soft); color: var(--warn); }
.f4b-tier.t-cheap { color: #2f8f5b; } .f4b-tier.t-mid { color: #a8730c; } .f4b-tier.t-high { color: #b0413e; }
.f4b-bad { color: var(--bad, #d70015); } .f4b-ok { color: var(--ok, #248a3d); }
.f4b-spin { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 50%; border: 2px solid var(--accent, #0071e3); border-right-color: transparent; animation: f4bspin .9s linear infinite; vertical-align: -1px; }
@keyframes f4bspin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .f4b-spin { animation: none; } }
`;
  document.head.append(st);
}
