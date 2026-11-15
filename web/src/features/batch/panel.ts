// F4 batch Q&A panel: paste many questions (one per line, any language) or drop a .txt / .csv / .tsv / .xlsx / .json,
// all answered on the drawing (and page) on screen through the normal /api/ask, in a live table: question, answer,
// unit, how sure, cost tier, time, Show (opens that answer with its evidence on the drawing). Sortable; CSV / XLSX
// export; one summary line. Kept per drawing in this browser so a reload keeps the answers already in.
import { fmtValue, unitLabel } from "../../card";
import { confidenceWords, costTier, isGuess, needsMeasuring } from "../../customer";
import type { Answer } from "../../types";
import { MAX_QUESTIONS, SHEET_SEP, decodeText, droppedLines, extOf, fromDocument, matchDrawing, matchSheet, parseFileText, parseText, fromRows, sheetLabel, toLines, type BatchQuestion, type SheetRef } from "./parse";
import { BatchRunner, effective, etaMs, etaWords, firstNumber, kindOf, matchByKind, matchExpected, median, solverMs, type AskFn, type AskTarget, type CardValue, type Row } from "./runner";
import { AUTO_KINDS, autoWhy, checkKind, printedAnswer, crossCheck, type CrossCheck, type Glance } from "./crosscheck";
import { reportHtml } from "./report";
import { evidenceThumb } from "./thumbs";
import { KEEP_LISTS, STORE, evictLists } from "./store";
import { readDocument, readXlsx, xlsxBytes, type Cell } from "./xlsx";

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
  /** the name the app shows for this drawing (its sheet tab: 'Level 00'), null when it shows none (UX C-7) */
  sheetTitle?(path: string): string | null;
  /** the catalogue case's name for a sample drawing ('Apartment floor'), null for the visitor's own files */
  caseTitle?(path: string): string | null;
  /** what the answer card shows now (its number may change after the answer came: a room traced and measured on the
   *  drawing, a scale the visitor set), for the question in the box on the drawing on screen; null: no card */
  readCard?(): (CardValue & { question: string; path: string }) | null;
  /** call back when the card changes (returns unsubscribe) */
  watchCard?(cb: () => void): () => void;
  /** this sheet's finished takeoff, summarised (W5's GET /api/glance: never starts one); null / ok:false = none yet */
  glance?(path: string, page: number): Promise<Glance | null>;
}

const baseName = (p: string) => p.split("/").pop() ?? p;

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
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
const BUSY_HINT = "The door / window finder was busy or slow when this question was asked (other questions were using it), so the answer was made without it and is unsure. The list asks it once more after its other questions; at most twice, and only when the server did not just repeat its earlier reply.";
const AGAIN_HINT = "The first question on a new picture is answered while its own lines are still being measured (a traced copy of the drawing, up to a few minutes). The list asks such a question once more after its other questions, so the answer can read the measured lines; at most twice, and only when the server did not just repeat its earlier reply.";
/** a question about doors / windows (en fi de sv no nl fr es it): on a picture the door / window finder answers it */
const SYMBOL_Q = /\b(?:doors?|doorways?|windows?|openings?)\b|\bov(?:i|ea|et|ia|en)\b|ovien|ikkun|türen|\btür\b|fenster|dörr|fönster|\bdør|vindu|deur|raam|ramen|porte|fenêtre|puerta|ventana|\bport[ae]\b|finestr/i;
const KINDS_HINT = "Your expected answers by what the question asks for (Stoa's order: areas, lengths, counts, then names / scales / other text). Counts must be exact; areas and lengths within 2 % (in the same units: SF and ft are turned into m² and m first); text by its words. The 'kind' column of the exports says it per row.";
const COST_HINT = "Stoa's three cost tiers: cheap = rules on the drawing's own lines and text (CPU, no AI) · in-between = our local AI model on our own GPU · expensive = a hosted AI model (the $ is its list price; an answer the server repeated from its cache cost nothing new)";

/** What the table shows for one answer (same honesty rules as the answer card: an area / length nobody measured is
 *  shown as a guess, not as a measurement). */
/** The answer card's cost words (W5 customer.ts tierHtml, not exported: kept the same here, as W6 did on the phone;
 *  captain 21:39 + UX C-3: concrete and in neutral ink, never a red 'Expensive'): '≈ $0.004 · 1 Gemini call' /
 *  'our GPU · 2 calls' / 'cheap, no AI'. usd = this answer's own hosted spend (0 unless the tier is high). */
export function costWords(a: Answer): { words: string; usd: number; who: string; dots: number } {
  const t = costTier(a);
  const c = (a.cost ?? {}) as Record<string, number>;
  const n = (k: string) => Math.max(0, (Number(c[k] ?? 0) || 0) - (Number(c[`prep_${k}`] ?? 0) || 0));
  const calls = n("api_calls");
  const usd = t.tier === "high" ? n("gemini_usd") || n("usd") || (n("api_tokens_in") * 2 + n("api_tokens_out") * 12) / 1e6 : 0; // Gemini 3.1 Pro list price per M tokens
  const who = (a.trace ?? []).some((l) => /gemini/i.test(String(l))) || n("gemini_usd") > 0 ? "Gemini" : "hosted model";
  const words = t.tier === "high" ? `${usd > 0 ? `≈ ${usdText(usd)} · ` : ""}${calls || 1} ${who} call${calls > 1 ? "s" : ""}`
    : t.tier === "mid" ? `our GPU${n("llm_calls") ? ` · ${n("llm_calls")} call${n("llm_calls") > 1 ? "s" : ""}` : ""}` : "cheap, no AI";
  return { words, usd, who, dots: t.tier === "cheap" ? 1 : t.tier === "mid" ? 2 : 3 };
}
export const usdText = (usd: number) => `$${usd < 0.01 ? usd.toFixed(3) : usd.toFixed(2)}`;

export function shownOf(r: Row, file = ""): { text: string; num: number | null; unit: string; guess: boolean; approx?: boolean; sure: string; cls: string; tier: string; tierCls: string; tierWhy: string; cost: string; dots: number } {
  const a = r.answer;
  if (!a) return { text: "", num: null, unit: "", guess: false, sure: "", cls: "", tier: "", tierCls: "", tierWhy: "", cost: "", dots: 0 };
  const cw = costWords(a);
  if (r.card) {
    // the card measured it after Show (traced the room at the sheet's scale / the visitor's scale): its number and words
    const t = costTier(a);
    const est = /≈/.test(r.card.big);
    return { text: r.card.big.replace(/^≈\s*/, ""), num: firstNumber(r.card.big), unit: r.card.unit, guess: false, sure: r.card.sure || (est ? "Best estimate" : "Measured"),
      cls: est ? "est" : "fair", tier: t.word, tierCls: t.tier, tierWhy: t.why, cost: cw.words, dots: cw.dots };
  }
  const none = a.value === null || a.value === undefined;
  const guess = !none && needsMeasuring(a, r.q, file);
  const c = confidenceWords(a.confidence ?? 0);
  const t = costTier(a);
  const unit = a.unit === "count" ? "" : unitLabel(a.unit);
  return {
    text: none ? "No answer" : fmtValue(a),
    num: typeof a.value === "number" ? a.value : null,
    // '≈' like the answer card: not measured, or a guess (confidence under 0.4: '≈ 4,093.35 m² · Rough guess')
    unit, guess, approx: guess || (!none && isGuess(a)),
    sure: none ? "No answer" : guess ? "Not measured" : c.word,
    cls: none ? "none" : guess ? "est guess" : c.cls,
    tier: t.word, tierCls: t.tier, tierWhy: t.why, cost: cw.words, dots: cw.dots,
  };
}

export function exportHeader(hasExpected: boolean): string[] {
  return ["#", ...(hasExpected ? ["your id"] : []), "question", "answer", "unit", "kind", "how sure", "confidence %", "cost tier",
    "seconds", "seconds incl. queue", "status", ...(hasExpected ? ["expected", "matches"] : []), "method", "evidence", "assumptions", "missing", "drawing", "page", "open again"];
}

export function exportRows(rows: Row[], file: BatchFile, link?: (q: string, path: string) => string, xcOf?: (r: Row) => CrossCheck | null): Cell[][] {
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
      kindOf(r),
      a ? s.sure : "", a ? Math.round((a.confidence ?? 0) * 100) : "", a ? `${s.tierCls === "cheap" ? "cheap (no AI" : `${s.tier.toLowerCase()} (${s.cost}`}${a.cached ? ", cached: no new calls" : ""})` : "",
      solverMs(r) !== undefined ? Math.round(solverMs(r)! / 100) / 10 : "", r.ms !== undefined ? Math.round(r.ms / 100) / 10 : "",
      r.status === "done" ? (a?.cached ? "done (cached)" : r.againAt !== undefined ? (r.why === "busy" ? "done (the door / window finder was busy: asking again)" : "done (first reading: asking again)") : "done") : r.status === "error" ? `failed: ${r.error ?? ""}` : r.status,
      ...(hasExp ? [r.expected ?? "", m === "yes" ? "yes" : m === "no" ? "no" : ""] : []),
      a?.solver ?? "",
      ev.length ? `${ev.length} marked: ${ev.slice(0, 3).map((e) => (e.note ?? "").replace(/\s+/g, " ").slice(0, 80)).join(" | ")}${ev.length > 3 ? " | …" : ""}` : a ? "none marked" : "",
      [...(a?.assumptions ?? []), ...(r.was ? [`changed on the answer card (was ${r.was})`] : []),
        ...(r.first ? [r.why === "busy" ? `asked again once the door / window finder was free (the answer without it was ${r.first})` : `asked again once the drawing's lines were measured (the first reading was ${r.first})`] : []),
        ...((x) => x && !/^this sheet's takeoff/.test(r.card?.sentence ?? "") ? [`this sheet's takeoff: ${x.text}${x.agree === true ? " (agrees)" : x.agree === false ? ` (differs: ${x.diff ?? ""})` : ""}`] : [])(xcOf?.(r) ?? null),
        ...(r.card ? [/^this sheet's takeoff/.test(r.card.sentence ?? "") ? `taken from ${r.card.sentence}${r.xcAuto === "far" ? " (by itself: our first reading of this whole-sheet number was over 2× off the takeoff and not very sure)" : r.xcAuto ? " (by itself: our first reading of this whole-sheet number was only a guess)" : ""}; our first reading was ${reader}`
          : `measured on the answer card after Show${r.card.sentence ? `: ${r.card.sentence}` : ""}; our first reading was ${reader}`] : [])].join(" | "), (a?.missing ?? []).join(" | "),
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
  // UX M19: whole-sheet questions checked against this sheet's own takeoff ('This floor at a glance' says the same);
  // read once per drawing / page, again after a minute while there is no finished takeoff yet
  const glances = new Map<string, { at: number; g: Glance | null; busy: boolean }>();
  const pageOfRow = (r: Row) => r.page ?? (rowPath(r) === file?.path ? file?.page ?? 0 : 0);
  function glanceOf(path: string, page: number): Glance | null {
    if (!ctx.glance || !path) return null;
    const k = `${path}|${page}`;
    const e = glances.get(k);
    if (e && (e.g?.ok || e.busy || Date.now() - e.at < 60_000)) return e.g;
    const ent = { at: Date.now(), g: e?.g ?? null, busy: true };
    glances.set(k, ent);
    ctx.glance(path, page).then((g) => { ent.g = g && g.ok ? g : null; }, () => { ent.g = null; })
      .finally(() => { ent.busy = false; ent.at = Date.now(); if (ent.g) render(); });
    return ent.g;
  }
  const takeoffCard = (x: CrossCheck): CardValue => ({ big: x.shown ?? x.their.toLocaleString("en-US", { maximumFractionDigits: 2 }), unit: x.unit, sentence: `this sheet's takeoff: ${x.text}`, sure: "From the takeoff" });
  function xcOf(r: Row): CrossCheck | null {
    if (r.status !== "done" || !r.answer || r.missing) return null;
    const kind = checkKind(r.q);
    if (!kind) return null;
    const g = glanceOf(rowPath(r), pageOfRow(r));
    return g ? crossCheck(kind, effective(r), g) : null;
  }
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
  /** the row shows this sheet's takeoff's number: the reader's evidence picture would show what the row does NOT say */
  const fromTakeoff = (r: Row) => /^this sheet's takeoff/.test(r.card?.sentence ?? "");
  function thumbHtml(r: Row): string {
    const a = r.answer;
    if (!a || !file || fromTakeoff(r)) return "";
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
      <label class="f4b-file" title="A list of questions: .txt (one per line), .csv / .tsv, Excel (.xlsx), Word (.docx: its lines, or a table with a 'Question' column), .odt, a PDF of questions (lines ending in '?'; a number after the '?' is the expected answer) or .json; columns 'question', 'expected', 'drawing', 'sheet' are read when present. You can also drop the file on this panel."><input type="file" accept=".txt,.csv,.tsv,.xlsx,.json,.md,.docx,.odt,.pdf,text/plain,text/csv"><span class="f4b-filebtn">Choose a list file…</span> <span class="f4b-filehint">.txt, .csv, Excel, Word or PDF, or drop it here</span></label>
      <button type="button" class="f4b-example link" hidden>Try an example list</button>
      <span class="f4b-count"></span>
    </div>
    <div class="f4b-row">
      <button type="button" class="f4b-run primary">Answer all</button>
      <label class="f4b-conc" title="How many questions are answered side by side (the server has 3 answer workers; 2 leaves one free for your own questions)">answer
        <select aria-label="questions answered at once"><option>1</option><option selected>2</option><option>3</option></select> at once</label>
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
    <p class="f4b-note">Each question is answered exactly as if you typed it (same methods, same evidence). The list stays in this browser.
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
  let afterReset = false;
  let saves = 0;
  function save() {
    if (!file) return;
    if (afterReset) {
      if (!runner.rows.length && !text.value.trim()) return;
      afterReset = false;
    }
    try {
      // the answers without their long traces: enough for the table and the export; Show re-asks (cached at the server)
      // outlines dropped too (a 200-question list must fit the ~5 MB storage); boxes and notes stay for the export
      const slim = runner.rows.map((r) => ({ ...r, answer: r.answer ? {
        ...r.answer, trace: (r.answer.trace ?? []).slice(-6),
        evidence: (r.answer.evidence ?? []).slice(0, 12).map((e) => ({ ...e, polygon: undefined, polyline: undefined })),
      } : undefined }));
      const body = JSON.stringify({ at: Date.now(), draft: text.value, rows: slim, anchor: file });
      try {
        localStorage.setItem(key(file), body);
      } catch {
        // full storage (a judge's browser after hours of uploads): drop the oldest lists, try once more
        evictLists(4, key(file));
        localStorage.setItem(key(file), body);
      }
      if (++saves % 25 === 1) evictLists(KEEP_LISTS, key(file));
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
    if (!fileNote) count.textContent = draft.length ? `${draft.length} question${draft.length > 1 ? "s" : ""}${extra}${left}` : runner.rows.length ? "" : "Type or paste your questions above, one per line";
    // in the list already: answered, failed ('Ask the failed ones again' / 'try again' on its row) or skipped
    const where = (r: Row) => r.missing ? `?${r.missing.toLowerCase()}` : placeKey({ path: rowPath(r), page: r.page });
    const inList = new Set(runner.rows.filter((r) => r.status !== "queued" || runner.isRunning).map((r) => `${where(r)}|${r.q.toLowerCase()}`));
    const here = onScreen?.path ?? file?.path ?? "";
    const at = (d: BatchQuestion) => { if (!d.drawing) return here; const p = place(d.drawing); return p ? placeKey(p) : `?${d.drawing.toLowerCase()}`; };
    const fresh = draft.slice(0, MAX_QUESTIONS).filter((d) => !inList.has(`${at(d)}|${d.q.toLowerCase()}`)).length;
    const failed = runner.rows.some((r) => r.status === "error");
    const waiting = runner.rows.filter((r) => r.status === "queued").length;
    const pl = (n: number) => `${n} question${n === 1 ? "" : "s"}`;
    runBtn.textContent = fresh && fresh < draft.length ? `Answer ${fresh} more question${fresh === 1 ? "" : "s"}` : fresh ? `Answer ${pl(fresh)}`
      : waiting ? `Continue: ${waiting} waiting` : draft.length ? (failed ? "All asked" : "All answered") : "Answer all";
    runBtn.disabled = !file || (!fresh && !waiting);
    // while the list runs, the button only appears for questions typed since (UX M19: it read 'Answer 1 more question'
    // for the one still running)
    if (runner.isRunning) runBtn.hidden = !fresh;
    // a disabled button says why (UX 23:30)
    runBtn.title = !file ? "Open or upload a drawing first: the questions are answered on it"
      : runBtn.disabled ? (draft.length ? "Every question in the box is in the table already: add new ones, or use 'Ask again' on a row" : "Type or paste questions in the box above, one per line (or choose a list file)")
      : "Answer every new question in the box, " + (Number(conc.value) > 1 ? `${conc.value} at once` : "one at a time");
    if (!file && !runner.rows.length) count.textContent = "Open or upload a drawing first";
    exampleBtn.hidden = !!text.value.trim() || runner.rows.length > 0;
  }

  text.addEventListener("input", () => { fileNote = false; onDraft(); save(); });
  async function loadFile(f: File) {
    try {
      const pdf = /\.pdf$/i.test(f.name);
      if (f.size > (pdf ? 40 : 5) << 20) throw new Error(`it is larger than ${pdf ? 40 : 5} MB`);
      const qs = /\.xlsx$/i.test(f.name) ? fromRows(await readXlsx(new Uint8Array(await f.arrayBuffer())))
        : /\.(docx|odt)$/i.test(f.name) ? fromDocument(await readDocument(f.name, new Uint8Array(await f.arrayBuffer())))
        // a PDF of questions (a list printed / exported to PDF); a drawing given here by mistake is refused with a pointer
        : pdf ? await (async () => {
          const r = await (await import("./pdflist")).readPdfList(new Uint8Array(await f.arrayBuffer()));
          if (r.why) throw new Error(`${r.why}. To ask about a drawing, drop it on the drawing area`);
          return r.qs;
        })()
        : parseFileText(f.name, decodeText(new Uint8Array(await f.arrayBuffer()))); // Excel's Windows CSV / UTF-16 too
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
        count.textContent = `${qs.length} question${qs.length > 1 ? "s" : ""} from ${f.name}${ps.length ? `${nd === 0 ? "" : nd === 1 ? " about one drawing" : ` about ${nd} of your drawings`}${sheets ? ` (${sheets} sheet${sheets === 1 ? "" : "s"} of a set)` : ""}${alias ? ` (it calls the drawing on screen '${alias}')` : ""}${nm ? `${nd ? " +" : " about"} ${nm} drawing${nm === 1 ? "" : "s"} not uploaded yet (upload ${nm === 1 ? "it" : "them"} first)` : ""}` : ""}${qs.some((x) => x.expected) ? " (with your expected answers)" : ""}`;
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
    if (/\.(txt|csv|tsv|tab|xlsx|json|md|docx|odt|pdf)$/i.test(f.name) || f.type.startsWith("text/")) void loadFile(f);
    else count.textContent = `${f.name} is not a question list (.txt, .csv, Excel, Word, PDF or .json). To ask about a new drawing, drop it on the drawing area.`;
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
      const body = exportRows(rows, f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined, xcOf);
      const pics: (string | null)[] = [];
      for (let i = 0; i < rows.length; i++) {
        reportBtn.textContent = `Drawing the pictures… ${i + 1} of ${rows.length}`;
        const r = rows[i];
        pics.push(r.status === "done" && r.answer && !fromTakeoff(r) ? await evidenceThumb(r.answer, rowPath(r), 380, 230).catch(() => null) : null);
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
    const body = exportRows(sorted(), f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined, xcOf);
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
    const body = exportRows(sorted(), f, ctx.link ? (qq, p) => ctx.link!(fileOf(p), qq) : undefined, xcOf);
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
    // UX M19: a reader's GUESS at a whole-sheet area / largest room / room count gives way to this sheet's takeoff by
    // itself (it measures room by room; 'keep the reader's' undoes it); a measured or sure answer only gets the line + 'use it'
    let auto = false;
    for (const r of done) {
      if (r.card || r.xcKeep || !r.answer) continue;
      const x = xcOf(r);
      const why = x && autoWhy(x, { guess: shownOf(r, rowPath(r)).guess, confidence: r.answer.confidence, value: r.answer.value, printed: printedAnswer(r.answer) });
      if (!x || !why) continue;
      r.card = takeoffCard(x);
      r.xcAuto = why;
      auto = true;
    }
    if (auto) save();
    const nAuto = done.filter((r) => r.xcAuto && r.card).length;
    const failed = rows.filter((r) => r.status === "error");
    const busy = rows.filter((r) => r.status === "running").length;
    const waiting = rows.filter((r) => r.status === "queued").length;
    const later = runner.pendingAgain.length;
    const ds = drawingsOf();
    const isMulti = multi();
    const nSheets = new Set(rows.filter((r) => r.page !== undefined).map((r) => placeKey({ path: rowPath(r), page: r.page }))).size;
    const alias = [...new Set(rows.filter((r) => r.named).map((r) => r.named!))];
    // UX C-7: the sheet's name as the app shows it ('Level 00', 'A101 · Floor Plan - 1st Floor'), the file name beside it
    // UX P2b-4: a catalogue case by its name ('Apartment floor'), not its dataset file id (site1000_bldg2082_floor9385.pdf)
    const fname = file ? (!isMulti && ctx.caseTitle?.(file.path)) || (file.name ?? file.path.split("/").pop() ?? "") : "";
    const title = file && !isMulti ? sheetTitleOf(file) : "";
    onLine.innerHTML = !file ? "no drawing: open one first"
      : esc(isMulti ? `${ds.length} drawing${ds.length > 1 ? "s" : ""}: ${ds.map((p) => fileOf(p).name ?? baseName(p)).join(", ")}${nSheets ? ` (${nSheets} sheet${nSheets > 1 ? "s" : ""} your list names)` : ""}${rows.some((r) => r.missing) ? " (and some not uploaded)" : ""}`
        : title && title.toLowerCase() !== fname.toLowerCase() ? title : `${fname}${file.page !== undefined && (file.pages ?? 0) > 1 ? `, page ${file.page + 1}` : ""}`)
      + (title && !isMulti && title.toLowerCase() !== fname.toLowerCase() ? ` <span class="f4b-onfile">· ${esc(fname)}${file.page !== undefined ? `, page ${file.page + 1}` : ""}</span>` : "")
      + esc(alias.length ? ` · your list calls it ${alias.slice(0, 2).map((a) => `'${a}'`).join(", ")}${alias.length > 2 ? " …" : ""}` : "");
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
    let usd = 0, who = "";
    for (const r of done) if (r.answer) {
      const tt = costTier(r.answer).tier, cw = costWords(r.answer);
      tiers[tt]++;
      if (!r.answer.cached) usd += cw.usd;
      if (tt === "high") who = who && who !== cw.who ? "hosted model" : cw.who;
    }
    const costLine = [tiers.cheap ? `${tiers.cheap} cheap (no AI)` : "", tiers.mid ? `${tiers.mid} on our GPU` : "",
      tiers.high ? `${tiers.high} with ${who === "Gemini" ? "Gemini" : "a hosted model"}${usd > 0 ? ` (≈ ${usdText(usd)} in all)` : ""}` : ""].filter(Boolean).join(", ");
    const guesses = done.filter((r) => shownOf(r, rowPath(r)).guess || (r.answer?.value ?? null) === null).length;
    const differ = done.filter((r) => xcOf(r)?.agree === false).length;
    const laterBusy = done.filter((r) => r.againAt !== undefined && r.why === "busy").length;
    const matched = hasExp ? rows.filter((r) => matchExpected(r.expected, effective(r)) === "yes").length : 0;
    const checked = hasExp ? rows.filter((r) => matchExpected(r.expected, effective(r))).length : 0;
    sum.innerHTML = `<b>${done.length} of ${rows.length} answered</b>`
      + (busy ? ` · ${busy} answering now` : "") + (waiting ? ` · ${waiting} waiting` : "")
      + ((t) => (t !== null && runner.isRunning && t >= 45_000 ? ` · <span title="At the pace of the answers so far (the median wait per question, ${esc(conc.value)} at once); a first reading asked again later is not counted">${etaWords(t)}</span>` : ""))(etaMs(rows, Number(conc.value)))
      + (later - laterBusy > 0 ? ` · <span title="${esc(AGAIN_HINT)}">${later - laterBusy} first reading${later - laterBusy > 1 ? "s" : ""} to ask again once the drawing is measured</span>` : "")
      + (laterBusy ? ` · <span title="${esc(BUSY_HINT)}">${laterBusy} to ask again once the door / window finder is free</span>` : "")
      + (failed.length ? ` · <span class="f4b-bad">${failed.length} failed</span>` : "")
      + (med !== null ? ` · median ${fmtS(med)} per answer` : "")
      + (took && !runner.isRunning && done.length ? ` · the whole list took ${fmtS(took)}` : "")
      + (done.length ? ` · <span title="${esc(COST_HINT)}">cost: ${costLine}</span>` : "")
      + (guesses ? ` · ${guesses} without a measured number` : "")
      + (nAuto ? ` · <span title="Whole-sheet questions (a floor's area, its largest room, how many rooms) our first reading could only guess, or answered more than 2× off the takeoff without being very sure: the row shows this sheet's takeoff instead, which measures room by room. 'use our first reading' on a row undoes it.">${nAuto} taken from this sheet's takeoff (our first reading was a guess or over 2× off)</span>` : "")
      + (differ ? ` · <span class="f4b-bad" title="Whole-sheet questions checked against this sheet's takeoff (the same numbers as 'This floor at a glance'): counts exact, areas within 2 %">${differ} differ from this sheet's takeoff</span>` : "")
      + (checked ? ` · <b>${matched} of ${checked} match your expected answers</b>` : "")
      + ((k) => k ? ` <span class="f4b-kinds" title="${esc(KINDS_HINT)}">(${esc(k)})</span>` : "")(hasExp ? matchByKind(rows) : "");
    const th = (k: SortKey, label: string, title = "") => `<th data-k="${k}" tabindex="0" aria-sort="${sortKey === k ? (sortDir > 0 ? "ascending" : "descending") : "none"}" class="${sortKey === k ? (sortDir > 0 ? "asc" : "desc") : ""}"${title ? ` title="${esc(title)}"` : ""}>${label}</th>`;
    thead.innerHTML = wide
      ? `<tr>${th("id", "#")}${th("q", "Question")}${th("answer", "Answer")}${th("sure", "How sure")}${th("tier", "Cost", COST_HINT)}${th("ms", "Time")}${hasExp ? th("match", "Expected") : ""}<th></th></tr>`
      : `<tr>${th("id", "#")}${th("q", "Question")}${th("answer", "Answer")}<th></th></tr>`;
    tbody.innerHTML = sorted().map((r) => rowHtml(r, hasExp, isMulti)).join("");
    onDraft();
  }

  const titleMemo = new Map<string, string>();
  function sheetTitleOf(f: BatchFile): string {
    const tab = ctx.sheetTitle?.(f.path);
    if (tab) return tab;
    if (f.page === undefined || !ctx.sheets || !/\.pdf$/i.test(f.path)) return "";
    const key = `${f.path}|${f.page}`;
    if (!titleMemo.has(key)) {
      titleMemo.set(key, "");
      void ctx.sheets(f.path).then((idx) => {
        const lab = idx.length > 1 ? sheetLabel(idx.find((x) => x.page === f.page)) : "";
        if (lab) { titleMemo.set(key, lab); render(); }
      }).catch(() => {});
    }
    return titleMemo.get(key) ?? "";
  }

  function rowHtml(r: Row, hasExp: boolean, isMulti: boolean): string {
    const s = shownOf(r, rowPath(r));
    let ans = "", sure = "", tier = "", time = "", act = "";
    if (r.status === "done" && r.answer) {
      const reader = r.card ? (() => { const x = shownOf({ ...r, card: undefined }, rowPath(r)); return `${x.approx && x.num !== null ? "≈ " : ""}${x.text}${x.unit ? ` ${x.unit}` : ""}`; })() : "";
      const fromXc = /^this sheet's takeoff/.test(r.card?.sentence ?? "");
      ans = r.card
        ? `<span class="f4b-val">${esc(r.card.big)}</span>${r.card.unit ? ` <span class="f4b-unit">${esc(r.card.unit)}</span>` : ""} <small class="f4b-was" title="${esc(r.card.sentence ?? "measured on the drawing")}">${fromXc
          ? r.xcAuto === "far" ? `measured room by room in this sheet's takeoff · our first reading (${esc(reader)}) was over 2× off it`
            : r.xcAuto ? `measured room by room in this sheet's takeoff · our first reading (${esc(reader)}) was a guess`
            : `from this sheet's takeoff · our first reading was ${esc(reader)}`
          : `measured on the drawing · our first reading was ${esc(reader)}`}</small>`
        : `<span class="f4b-val${s.approx ? " guess" : ""}">${s.approx && s.num !== null ? "≈ " : ""}${esc(s.text)}</span>${s.unit ? ` <span class="f4b-unit">${esc(s.unit)}</span>` : ""}`;
      ans += r.was ? ` <small class="f4b-was" title="Changed on the answer card after Show (your scale, an option you picked or another way of reading it)">was ${esc(r.was)}</small>` : "";
      const busyRow = r.why === "busy";
      ans += r.againAt !== undefined ? `<br><small class="f4b-later" title="${esc(busyRow ? BUSY_HINT : AGAIN_HINT)}">${busyRow ? "the door / window finder was busy" : "first reading"} · asked again in ${fmtS(Math.max(0, r.againAt - Date.now()))}${runner.rows.some((x) => x.status === "queued") ? " (after the list)" : ""}</small>`
        : r.first ? ` <small class="f4b-was" title="${busyRow ? "The first answer was made while the door / window finder was busy (without it); asked again once it was free" : "The first reading came before the drawing's own lines were measured (a traced copy of the picture); asked again once they were"}">${busyRow ? "without the finder" : "first reading"} ${esc(r.first)}</small>` : "";
      const x = /^this sheet's takeoff/.test(r.card?.sentence ?? "") ? null : xcOf(r);
      if (x && x.agree === true) ans += `<br><small class="f4b-xc f4b-ok" title="${esc(`This sheet's takeoff: ${x.text}`)}">✓ agrees with this sheet's takeoff</small>`;
      else if (x) ans += `<br><small class="f4b-xc${x.agree === false ? " f4b-bad" : ""}" title="${esc(AUTO_KINDS.has(x.kind) ? "The takeoff traces every room of this sheet (the same numbers as 'This floor at a glance'); counts exact, areas within 2 %" : "The takeoff's door / window count (the same as 'This floor at a glance') is no surer than this answer: they find the symbols differently. Show marks this answer's ones on the drawing")}">this sheet's takeoff: ${esc(x.text)}${x.diff ? ` (yours ${esc(x.diff)})` : ""}</small>`
        + (r.card || !AUTO_KINDS.has(x.kind) ? "" : ` <button type="button" class="f4b-usexc link" data-id="${r.id}" title="Make the takeoff's number this row's answer (in the table, the exports and the expected-answer check)">use it</button>`);
      sure = `<span class="f4b-conf ${s.cls}">${esc(s.sure)}</span>`;
      tier = `<span class="f4b-tier tier t-${s.tierCls}" title="Cost tier ${esc(s.tier.toLowerCase())}: ${esc(s.tierWhy)}${r.answer.cached ? ". This time it came from our answer cache: no new model calls" : ""}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= s.dots ? "on" : ""}"></i>`).join("")}</span>${esc(s.cost)}</span>`;
      const own = solverMs(r);
      const wait = r.ms !== undefined && own !== undefined && r.ms - own > 2000 ? r.ms - own : 0;
      time = `<span title="${esc(r.answer.cached ? "answered before: the server kept it" : `answering took ${fmtS(own)}${wait ? `; it waited ${fmtS(wait)} behind the list's other questions` : ""}`)}">${fmtS(own)}</span>${r.answer.cached ? ` <small>cached</small>` : ""}`;
      act = `<button type="button" class="f4b-show primary" data-id="${r.id}" title="${fromXc ? "Open our first reading with its evidence on the drawing, and 'This floor at a glance' (the takeoff's numbers this row shows)" : "Open this answer with its evidence on the drawing"}">Show</button>${wide ? " " : "<br>"}<button type="button" class="f4b-again link" data-id="${r.id}" title="Ask this question again">Ask again</button>`
        + (fromXc ? `<br><button type="button" class="f4b-unxc link f4b-minor" data-id="${r.id}" title="Put our first reading back in this row (the table, the exports and the expected-answer check)">use our first reading</button>` : "");
    } else if (r.status === "running" && r.answer && r.reasks) {
      ans = `<span class="f4b-val${s.approx ? " guess" : ""}">${s.approx && s.num !== null ? "≈ " : ""}${esc(s.text)}</span>${s.unit ? ` <span class="f4b-unit">${esc(s.unit)}</span>` : ""}`
        + (r.why === "busy" ? `<br><span class="f4b-wait" title="${esc(BUSY_HINT)}"><i class="f4b-spin"></i>asking again with the door / window finder…</span>`
          : `<br><span class="f4b-wait" title="${esc(AGAIN_HINT)}"><i class="f4b-spin"></i>asking again: the drawing's lines should be measured now…</span>`);
      time = fmtS(r.started ? Date.now() - r.started : 0);
    } else if (r.status === "running") {
      ans = `<span class="f4b-wait"><i class="f4b-spin"></i>answering…${r.tries > 1 ? ` (try ${r.tries})` : ""}</span>`
        // UX C-6: doors / windows on a picture wait for the door / window finder (~1 min) while the median says seconds
        + (r.started && Date.now() - r.started > 10_000 && SYMBOL_Q.test(r.q) ? `<br><small class="f4b-later">doors and windows take about a minute: the door / window finder reads the whole picture</small>` : "");
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
    if (b.classList.contains("f4b-usexc")) {
      const x = xcOf(r);
      if (!x) return;
      r.card = takeoffCard(x);
      r.xcKeep = undefined;
      save();
      return render();
    }
    if (b.classList.contains("f4b-unxc")) {
      r.card = undefined;
      r.xcAuto = undefined;
      r.xcKeep = true;
      save();
      return render();
    }
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
      // a row that shows this sheet's takeoff's number: its reading is 'This floor at a glance' (W5's card, read only here)
      if (fromTakeoff(r)) flashGlance();
    }
  });

  /** bring 'This floor at a glance' (#glance: the takeoff's numbers the row took) into view and pulse its outline once */
  function flashGlance() {
    window.setTimeout(() => {
      const g = document.getElementById("glance");
      if (!g || g.hidden || !g.offsetParent) return;
      g.scrollIntoView({ block: "nearest", behavior: "smooth" });
      try {
        g.animate([{ boxShadow: "0 0 0 0 rgba(0,113,227,0)" }, { boxShadow: "0 0 0 3px rgba(0,113,227,.55)" }, { boxShadow: "0 0 0 0 rgba(0,113,227,0)" }], { duration: 1600, easing: "ease-in-out" });
      } catch { /* no Web Animations: the scroll alone */ }
    }, 600);
  }

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
    // a row that took this sheet's takeoff's number keeps it while the card shows the reader's own answer (Show draws the
    // reader's evidence); a NEW number on the card (the visitor measured it) replaces it, and the takeoff is not taken again
    if (/^this sheet's takeoff/.test(r.card?.sentence ?? "")) {
      if (!next) return false;
      r.xcAuto = undefined;
      r.xcKeep = true;
    }
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
  const tick = window.setInterval(() => { if (runner.rows.some((r) => r.status === "running" || r.againAt !== undefined)) render(); }, 1000);

  load();
  render();
  return {
    runner,
    /** W5's global 'Start over' (window 'stoa:reset', captain 01:18): stop asking, forget every list of this visit (the
     *  lists this browser saved too), an empty box, no highlighted row, no measuring walk, the narrow table. */
    reset() {
      if (measuring) measuring.stop = true;
      runner.stop();
      runner.restore(file ? target(file) : { path: "" }, []);
      fileDraft = null;
      fileNote = false;
      text.value = "";
      shownId = -1;
      try {
        for (const k of Object.keys(localStorage)) if (k.startsWith(STORE) || k === "f4.batch.wide") localStorage.removeItem(k);
      } catch {
        /* storage blocked: nothing was saved */
      }
      if (wide) { wide = false; applyWide(); }
      // the runner's change handler saves the (empty) list once more on the next frame: after a reset an empty list is
      // not saved (no record naming the visitor's file stays in this browser) until something is typed / asked again
      afterReset = true;
      render();
    },
    /** Several questions typed / pasted into the page's own question box: they become this list (answered one by one,
     *  like 'Answer all'), and the promise gives the FIRST question's answer for the answer card (asked once: the
     *  list's row). null = not taken (no drawing, or nothing to ask): the normal ask goes on. */
    askList(t: string): Promise<Answer> | null {
      if (!file) return null;
      const qs = parseText(t);
      if (qs.length < 2) return null;
      text.value = t;
      fileNote = false;
      fileDraft = null;
      onDraft();
      save();
      if (!runBtn.disabled) runBtn.click();
      const p = (onScreen ?? file).path, q1 = qs[0].q.toLowerCase();
      const row = () => runner.rows.find((x) => x.q.toLowerCase() === q1 && rowPath(x) === p && !x.missing);
      if (!row()) return null;
      // the card will show this row: highlighted, and the card's own number (a traced room) comes back into it
      shownId = row()!.id;
      schedule();
      const t0 = Date.now();
      return new Promise<Answer>((res, rej) => {
        const tick = () => {
          const r = row();
          if (r?.status === "done" && r.answer) {
            // one restored from storage is slimmed (no outlines): the card asks it again (the server's cache answers)
            if (r.restored && ctx.ask) return void ctx.ask({ path: p }, r.q, new AbortController().signal).then(res, rej);
            return res(r.answer);
          }
          if (!r || r.status === "error" || r.status === "skipped") return rej(new Error(r?.error ?? "the list's first question was not answered"));
          if ((r.status === "queued" && !runner.isRunning) || Date.now() - t0 > 700_000) return rej(new Error("the list was stopped before its first question was answered: ask again"));
          window.setTimeout(tick, 300);
        };
        tick();
      });
    },
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
      const shown = (x: Row) => { const v = shownOf(x, rowPath(x)); return `${v.approx && v.num !== null ? "≈ " : ""}${v.text}${v.unit ? ` ${v.unit}` : ""}`; };
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
.f4b-head { font-size: 13.5px; color: var(--muted, #6e6e73); } .f4b-on { color: var(--ink, #1d1d1f); font-weight: 600; } .f4b-onfile { color: var(--muted, #6e6e73); font-weight: 400; }
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
.f4b-file { color: var(--muted, #6e6e73); position: relative; display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; cursor: pointer; }
.f4b-file input { position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }
.f4b-filebtn { font: inherit; border-radius: 999px; padding: 5px 12px; border: 1px solid var(--line-strong, #ccc); background: var(--panel, #fff); color: var(--text, inherit); }
.f4b-file:hover .f4b-filebtn { border-color: var(--accent, #0071e3); }
.f4b-file:focus-within .f4b-filebtn { outline: 2px solid var(--accent, #0071e3); outline-offset: 2px; }
.f4b-filehint { font-size: 12px; }
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
.f4b-dr { margin-top: 2px; font-size: 11.5px; color: var(--muted, #6e6e73); overflow-wrap: anywhere; } .f4b-was { color: var(--muted, #6e6e73); font-weight: 400; } .f4b-later { color: var(--muted, #6e6e73); font-style: italic; }
.f4b-conf, .f4b-tier { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; background: var(--fill, rgba(118,118,128,.12)); }
.f4b-conf.sure { background: var(--ok-soft); color: var(--ok); } .f4b-conf.fair { background: var(--warn-soft); color: var(--warn); }
.f4b-tier { color: var(--ink-2, #3c3c43); font-weight: 500; } .f4b-act .f4b-minor { font-size: 11px; color: var(--muted, #6e6e73); } .f4b-tier .tier-dots { margin-right: 5px; vertical-align: 1px; }
.f4b-bad { color: var(--bad, #d70015); } .f4b-ok { color: var(--ok, #248a3d); }
.f4b-spin { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 50%; border: 2px solid var(--accent, #0071e3); border-right-color: transparent; animation: f4bspin .9s linear infinite; vertical-align: -1px; }
@keyframes f4bspin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .f4b-spin { animation: none; } }
`;
  document.head.append(st);
}
