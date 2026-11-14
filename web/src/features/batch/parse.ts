// F4 batch Q&A: turn pasted text or an uploaded .txt / .csv / .tsv / .xlsx / .json into a question list.
// Pure functions (no DOM): unit-tested by batch.test.ts with `node --test` (Node strips the types itself).

export interface BatchQuestion {
  q: string;
  /** a reference answer from the file's 'expected' / 'answer' / 'gold' column, shown next to ours (never sent to /ask) */
  expected?: string;
  /** the visitor's own id column ('id', 'qid', '#'), kept in the export so their sheet lines up with ours */
  ref?: string;
  /** the drawing this question is about, by file name ('drawing' / 'file' / 'sheet' column, or a '[name.pdf] …' tag at
   *  the start of a pasted line); absent = the drawing on screen */
  drawing?: string;
}

export const MAX_QUESTIONS = 200;
const MAX_LEN = 1000;

// "1.", "1)", "(1)", "Q1:", "Q 1 -", "a)", "-", "*", "•", "–", "1.2" … at the start of a line
const LEAD = /^\s*(?:(?:q(?:uestion)?|kysymys|frage)\s*\d+\s*[:.)\-–]\s*|\(?\d{1,3}(?:\.\d{1,3})*\s*[.):\-–]\s+|\(?[a-z]\)\s+|[-*•·–—]\s+)/iu;
const QUOTES = /^["'“”„«»‘’]+|["'“”„«»‘’]+$/gu;

/** One question from a raw line: list markers, numbering and wrapping quotes removed, spaces collapsed. */
export function cleanLine(line: string): string {
  let s = line.replace(/ /g, " ").trim();
  for (let i = 0; i < 2; i++) s = s.replace(LEAD, "").trim(); // "1. - How many…" / "Q1: 1) …"
  s = s.replace(QUOTES, "").trim();
  return s.replace(/\s+/g, " ");
}

/** '[dfe-school.pdf] How many doors?' → the drawing tag and the question (a tag needs a letter and 3+ characters, so
 *  '[1] …' stays numbering). */
const TAG = /^\s*\[([^\]\n?]{3,120})\]\s*(\S.*)$/u;
export function splitTag(line: string): { q: string; drawing?: string } {
  const m = line.match(TAG);
  return m && /\p{L}/u.test(m[1]) ? { q: cleanLine(m[2]), drawing: m[1].trim() } : { q: cleanLine(line) };
}

/** A line that is not a question: empty, a comment, a Markdown heading / rule / table separator. */
const SKIP = /^\s*(?:#|\/\/|-{3,}\s*$|={3,}\s*$|\|?\s*:?-{3,})/;

// a pasted e-mail around the list: greetings, sign-offs (+ the name under them) and 'Our questions:' headers
const GREET = /^\s*(?:hi|hello|hey|dear|good (?:morning|afternoon|evening)|thanks|thank you|many thanks|regards|best(?: regards| wishes)?|kind regards|cheers|moi|hei|terve|kiitos|terveisin|ystävällisin terveisin|hallo|liebe[rs]?|danke|viele grüße|mit freundlichen grüßen|beste grüße|hej|tack|mvh|vänliga hälsningar|med vänlig hälsning)\b[^?？]*$/iu;
const SIGNOFF = /^\s*(?:thanks|thank you|many thanks|regards|best(?: regards| wishes)?|kind regards|cheers|kiitos|terveisin|ystävällisin terveisin|danke|viele grüße|mit freundlichen grüßen|beste grüße|tack|mvh|vänliga hälsningar|med vänlig hälsning)\b/iu;
// a line that stops mid-sentence ('What is the total area of' / 'Wie groß ist die Fläche der'): the next line goes on
const UNFINISHED_WORD = /\b(?:of|the|a|an|in|on|at|for|to|and|or|with|from|by|between|per|than|into|ja|tai|sekä|der|die|das|des|dem|den|und|oder|von|im|zu|zur|zum|och|i|av|på|för|med|eller)\s*$/iu;
const UNFINISHED = /(?:,|-|–)\s*$/u;
// a line that starts a question of its own (not the rest of the line above)
const QWORD = /^\s*(?:what|how|which|where|when|who|why|is|are|does|do|can|list|find|show|count|wie|was|welche[rs]?|wo|gibt|mikä|mitkä|kuinka|paljonko|montako|missä|onko|hur|vad|vilken|vilka|var|finns)\b/iu;

/** Lines of pasted text → question lines: a question wrapped over two lines is joined; greetings, sign-offs (and a
 *  short name under one) and header lines ending in ':' are left out (returned in `dropped`, for the count line). */
export function questionLines(lines: string[]): { keep: string[]; dropped: string[] } {
  const keep: string[] = [], dropped: string[] = [];
  let afterSignoff = false;
  for (let i = 0; i < lines.length; i++) {
    let l = lines[i];
    const c = cleanLine(l);
    if (GREET.test(c) && c.split(/\s+/).length <= 8) { dropped.push(c); afterSignoff = SIGNOFF.test(c); continue; }
    if (afterSignoff && !/[?？]/.test(c) && c.split(/\s+/).length <= 3) { dropped.push(c); continue; }
    afterSignoff = false;
    if (/:\s*$/.test(c) && !/[?？]/.test(c) && lines.length > 2) { dropped.push(c); continue; }
    // wrapped: this line stops mid-sentence and the next one goes on in lower case, without a list marker or a tag
    // ('…area of' + 'Room 101?' / '…Fläche der' + 'Küche?': after an article / preposition a capital goes on too)
    const goesOn = (a: string, b: string) => !/[?？.!;:]\s*$/.test(a) && !LEAD.test(b) && !TAG.test(b)
      && ((UNFINISHED.test(a) || UNFINISHED_WORD.test(a)) && /^\s*\p{Ll}/u.test(b) || UNFINISHED_WORD.test(a) && !QWORD.test(b));
    while (i + 1 < lines.length && goesOn(l, lines[i + 1])) l = `${l.trimEnd()} ${lines[++i].trim()}`;
    keep.push(l);
  }
  return { keep, dropped };
}

/** Pasted text, one question per line (any language). Comments and blank lines skipped, duplicates dropped (first kept),
 *  a question wrapped over two lines joined, an e-mail's greeting / sign-off left out. A Markdown table or a pasted
 *  spreadsheet range (tabs) is read like a CSV. */
export function parseText(text: string): BatchQuestion[] {
  const t = text.replace(/^﻿/, "");
  const lines = t.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length >= 2 && lines.filter((l) => l.includes("\t")).length >= Math.ceil(lines.length * 0.6)) return parseDelimited(t, "\t");
  if (lines.length >= 2 && lines.filter((l) => /^\s*\|.*\|\s*$/.test(l)).length >= Math.ceil(lines.length * 0.6))
    return fromRows(lines.filter((l) => !/^\s*\|?\s*:?-{3,}/.test(l)).map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim())));
  return dedupe(questionLines(lines.filter((l) => !SKIP.test(l))).keep.map(splitTag));
}

/** The lines of pasted text that parseText leaves out as not questions (greetings, sign-offs, headers). */
export function droppedLines(text: string): string[] {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/).filter((l) => l.trim());
  if (lines.length >= 2 && (lines.filter((l) => l.includes("\t")).length >= Math.ceil(lines.length * 0.6)
    || lines.filter((l) => /^\s*\|.*\|\s*$/.test(l)).length >= Math.ceil(lines.length * 0.6))) return [];
  return questionLines(lines.filter((l) => !SKIP.test(l))).dropped;
}

/** RFC 4180 rows: quoted fields may hold the delimiter, newlines and doubled quotes. */
export function splitDelimited(text: string, delim: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let f = "";
  let quoted = false;
  const s = text.replace(/^﻿/, "");
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quoted) {
      if (c === '"') {
        if (s[i + 1] === '"') { f += '"'; i++; } else quoted = false;
      } else f += c;
    } else if (c === '"' && f.trim() === "") { quoted = true; f = ""; }
    else if (c === delim) { row.push(f); f = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && s[i + 1] === "\n") i++;
      row.push(f); rows.push(row); row = []; f = "";
    } else f += c;
  }
  if (f !== "" || row.length) { row.push(f); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim()));
}

/** The delimiter of a CSV-like text: the one that splits the first lines most consistently (',' ';' tab '|'). */
export function sniffDelimiter(text: string): string {
  const head = text.split(/\r?\n/).filter((l) => l.trim()).slice(0, 8);
  let best = ",", score = 0;
  for (const d of [",", ";", "\t", "|"]) {
    const n = head.map((l) => splitDelimited(l, d)[0]?.length ?? 1);
    const ok = n.filter((k) => k > 1 && k === n[0]).length;
    if (ok > score) { best = d; score = ok; }
  }
  return best;
}

export function parseDelimited(text: string, delim = sniffDelimiter(text)): BatchQuestion[] {
  return fromRows(splitDelimited(text, delim));
}

const Q_COL = /^(?:questions?|q|query|prompt|kysymys|kysymykset|frage|fragen|fråga|question_en|text)$/i;
const EXP_COL = /^(?:expected|answer|gold|truth|reference|ref_answer|correct|vastaus|antwort|svar|expected_answer|ground_truth)$/i;
const ID_COL = /^(?:id|qid|#|no\.?|nr\.?|number|n)$/i;
const DRAW_COL = /^(?:drawings?|files?|file_?name|sheets?|sheet_?name|documents?|doc|pdf|image|plan|path|source_file|tiedosto|piirustus|datei|zeichnung|ritning|plan_file)$/i;
/** a page / sheet of a set: alone it names the drawing like DRAW_COL; next to a file column it names the page in it */
const SHEET_COL = /^(?:sheets?|sheet_?(?:no|nr|number|name|title|id)|pages?|page_?(?:no|nr|number)|sivu|sivunumero|seite|blatt|blatt_?nr|sida|arkki|lehti)$/i;
const FILE_COL = /^(?:drawings?|files?|file_?name|documents?|doc|pdf|image|plan|path|source_file|tiedosto|piirustus|datei|zeichnung|ritning|plan_file)$/i;
/** 'plan.pdf' + 'A-101' → 'plan.pdf | A-101' (the form a '[…]' tag and place() read) */
export const SHEET_SEP = " | ";
const joinDrawing = (file: string, sheet: string) => file && sheet ? `${file}${SHEET_SEP}${sheet}` : file || sheet;

/** Rows of cells → questions. A header row names the question column (else: the column with the longest texts). */
export function fromRows(rows: string[][]): BatchQuestion[] {
  if (!rows.length) return [];
  const norm = (c: string) => c.trim().replace(/^["']|["']$/g, "").replace(/\s+/g, "_");
  const head = rows[0].map(norm);
  let qi = head.findIndex((h) => Q_COL.test(h));
  let ei = head.findIndex((h) => EXP_COL.test(h));
  let ii = head.findIndex((h) => ID_COL.test(h));
  // a file column and a page / sheet column: both (the page in that file); else whichever names the drawing
  let fi = head.findIndex((h) => FILE_COL.test(h));
  let si = head.findIndex((h, k) => k !== fi && SHEET_COL.test(h));
  let di = fi >= 0 ? fi : si;
  if (fi < 0) si = -1;
  let body = rows;
  if (qi >= 0) body = rows.slice(1);
  else {
    // no header: the question column is the one whose cells are longest and mostly letters
    const width = Math.max(...rows.map((r) => r.length));
    let best = 0, bestLen = -1;
    for (let c = 0; c < width; c++) {
      const len = rows.reduce((s, r) => s + ((r[c] ?? "").match(/\p{L}/gu)?.length ?? 0), 0);
      if (len > bestLen) { best = c; bestLen = len; }
    }
    qi = best;
    ei = -1;
    ii = -1;
    di = -1;
    si = -1;
    // a header we do not know ('Kysymys nro', 'Prompt text'): drop a first row that has no '?' when the others do
    const qm = (r: string[]) => /[?？]/.test(r[qi] ?? "");
    if (rows.length > 2 && !qm(rows[0]) && rows.slice(1).filter(qm).length >= (rows.length - 1) / 2) body = rows.slice(1);
  }
  return dedupe(body.map((r) => ({
    q: cleanLine(r[qi] ?? ""),
    ...(ei >= 0 && (r[ei] ?? "").trim() ? { expected: r[ei].trim() } : {}),
    ...(ii >= 0 && (r[ii] ?? "").trim() ? { ref: r[ii].trim() } : {}),
    ...(di >= 0 && joinDrawing((r[di] ?? "").trim(), si >= 0 ? (r[si] ?? "").trim() : "") ? { drawing: joinDrawing((r[di] ?? "").trim(), si >= 0 ? (r[si] ?? "").trim() : "") } : {}),
  })));
}

/** JSON: ["q1", "q2"] or [{question|q|text, expected|answer, id}] or {questions: [...]}. */
export function parseJson(text: string): BatchQuestion[] {
  let j: unknown = JSON.parse(text);
  if (j && !Array.isArray(j) && typeof j === "object" && Array.isArray((j as { questions?: unknown }).questions)) j = (j as { questions: unknown[] }).questions;
  if (!Array.isArray(j)) throw new Error("a JSON file needs a list of questions");
  return dedupe(j.map((x) => {
    if (typeof x === "string") return { q: cleanLine(x) };
    const o = (x ?? {}) as Record<string, unknown>;
    const pick = (re: RegExp) => Object.entries(o).find(([k, v]) => re.test(k) && v !== null && v !== undefined && String(v).trim())?.[1];
    const exp = pick(EXP_COL), id = pick(ID_COL), fl = pick(FILE_COL), sh = pick(SHEET_COL);
    const dr = joinDrawing(fl !== undefined ? String(fl).trim() : "", sh !== undefined ? String(sh).trim() : "");
    return { q: cleanLine(String(pick(Q_COL) ?? "")), ...(exp !== undefined ? { expected: String(exp) } : {}), ...(id !== undefined ? { ref: String(id) } : {}),
      ...(dr ? { drawing: dr } : {}) };
  }));
}

/** Blank and over-long lines dropped, exact repeats (case / space insensitive, on the same drawing) kept once. */
export function dedupe(items: BatchQuestion[]): BatchQuestion[] {
  const seen = new Set<string>();
  const out: BatchQuestion[] = [];
  for (const it of items) {
    const q = it.q.trim();
    if (!q || !/\p{L}/u.test(q)) continue;
    const k = `${(it.drawing ?? "").toLowerCase()}|${q.toLowerCase().replace(/\s+/g, " ").replace(/[?？.!]+$/, "")}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ ...it, q: q.length > MAX_LEN ? q.slice(0, MAX_LEN) : q });
  }
  return out;
}

/** By file name: .csv/.tsv → delimited, .json → JSON, else plain lines (sniffed for tabs / Markdown tables). */
export function parseFileText(name: string, text: string): BatchQuestion[] {
  const ext = name.toLowerCase().split(".").pop() ?? "";
  if (ext === "json") return parseJson(text);
  if (ext === "csv") return parseDelimited(text);
  if (ext === "tsv" || ext === "tab") return parseDelimited(text, "\t");
  return parseText(text);
}

/** The text-box form of a list: one question per line, '[drawing] question' when it names a drawing. */
export function toLines(qs: BatchQuestion[]): string {
  return qs.map((x) => (x.drawing ? `[${x.drawing}] ${x.q}` : x.q)).join("\n");
}

/** A drawing named in a question list → the path of one of the drawings the visitor has open / uploaded, or null.
 *  Same path, same file name (any case), same name without extension and punctuation ('DfE School Level 00' =
 *  'dfe-school-level00.pdf'), else the ONE file whose name contains it (or is contained in it). Never a guess between
 *  two files. */
export function matchDrawing(name: string, files: { path: string; name?: string }[]): string | null {
  const n = name.trim();
  if (!n || !files.length) return null;
  const base = (p: string) => p.split(/[\\/]/).pop() ?? p;
  const stem = (s: string) => base(s).toLowerCase().normalize("NFKC").replace(/\.[a-z0-9]{2,5}$/i, "").replace(/[^\p{L}\p{N}]+/gu, "");
  const names = (f: { path: string; name?: string }) => [f.path, base(f.path), ...(f.name ? [f.name] : [])];
  const one = (hits: { path: string }[]) => {
    const u = [...new Set(hits.map((f) => f.path))];
    return u.length === 1 ? u[0] : null;
  };
  const exact = files.filter((f) => names(f).some((x) => x === n || x.toLowerCase() === n.toLowerCase()));
  if (exact.length) return one(exact);
  const s = stem(n);
  if (s.length < 2) return null;
  const same = files.filter((f) => names(f).some((x) => stem(x) === s));
  if (same.length) return one(same);
  if (s.length < 3) return null;
  return one(files.filter((f) => names(f).some((x) => { const t = stem(x); return t.length >= 3 && (t.includes(s) || s.includes(t)); })));
}

/** One page of a drawing set as the sheet index labels it (web/server/sheet_index.py via api.sheetLabels). */
export interface SheetRef { page: number; number?: string; title?: string; storey?: string }

const compact = (s: string) => s.toLowerCase().normalize("NFKC").replace(/[^\p{L}\p{N}]+/gu, "");
/** sheet-number-like tokens ('A-101', 'A101', 'S-100F', 'M-101A', 'A2.01', '(A102)'), compacted */
const sheetNos = (s: string) => (s.match(/(?<![\p{L}\p{N}])[A-Z]{1,3}[- .]?\d{1,4}(?:[.-]\d{1,3})?[A-Z]?(?![\p{L}\p{N}])/giu) ?? []).map(compact);
const uniq = (ps: number[]) => { const u = [...new Set(ps)]; return u.length === 1 ? u[0] : null; };

/** A sheet named in a list ('A-101', 'A101 Floor plan', 'Floor Plan - 1st Floor', '1st floor', 'page 3', 'p. 3', 'sivu 3')
 *  → the 0-based page of a drawing set whose sheet index labels it so, else null. In order: the sheet number (its own
 *  field or the one a title prints, '(A101)'), a sheet number inside the name, a page number, the title / storey whole,
 *  then the ONE title containing the name. Never a guess between two pages. */
export function matchSheet(name: string, sheets: SheetRef[]): number | null {
  const n = name.trim().normalize("NFKC");
  if (!n || !sheets.length) return null;
  const c = compact(n);
  if (!c) return null;
  const nosOf = (s: SheetRef) => [...new Set([...(s.number ? [compact(s.number)] : []), ...sheetNos(s.title ?? "")])].filter(Boolean);
  // 1. the sheet number itself
  let p = uniq(sheets.filter((s) => nosOf(s).includes(c)).map((s) => s.page));
  if (p !== null) return p;
  // 2. a sheet number inside the name ('Sheet A-101', 'A101 Floor plan 1st floor')
  const mine = sheetNos(n);
  if (mine.length) {
    p = uniq(sheets.filter((s) => nosOf(s).some((x) => mine.includes(x))).map((s) => s.page));
    if (p !== null) return p;
  }
  // 3. a page number ('3', 'page 3', 'p. 3', 'pg 3', '#3', 'sivu 3', 'Seite 3', 'sida 3')
  const pm = n.match(/^(?:pages?|pg\.?|p\.?|sivu|seite|sida|blatt|#)?\s*(\d{1,3})$/i);
  if (pm) {
    const k = Number(pm[1]) - 1;
    return sheets.some((s) => s.page === k) ? k : null;
  }
  // 4. the whole title / storey ('Floor Plan - 1st Floor', '1st Floor')
  p = uniq(sheets.filter((s) => [s.title, s.storey].some((t) => t && compact(t) === c)).map((s) => s.page));
  if (p !== null) return p;
  // 5. the one title / storey that contains the name ('Basement', '1st floor plan' is not 'Floor Plan - 1st Floor')
  if (c.length < 4) return null;
  return uniq(sheets.filter((s) => [s.title, s.storey].some((t) => t && compact(t).includes(c))).map((s) => s.page));
}

/** A short label for a page of a set ('A101 · Floor Plan - 1st Floor', 'p. 4'). */
export function sheetLabel(s: SheetRef | undefined): string {
  if (!s) return "";
  const no = s.number || "";
  const t = (s.title ?? "").replace(/^\d{1,2}\/\d{1,2}\/\d{2,4}\s*[-–]\s*/, "").trim();
  return [no, t].filter(Boolean).join(" · ").slice(0, 60) || `p. ${s.page + 1}`;
}

/** The file extension of a name in a list ('plan.PDF' → 'pdf'), '' when it has none. */
export function extOf(name: string): string {
  const m = name.trim().match(/\.([a-z0-9]{2,5})$/i);
  return m && /^(pdf|png|jpe?g|tiff?|webp|gif|bmp|svg|dxf|dwg|ifc|heic)$/i.test(m[1]) ? m[1].toLowerCase().replace("jpeg", "jpg").replace("tiff", "tif") : "";
}
