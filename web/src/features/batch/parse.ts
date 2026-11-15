// F4 batch Q&A: turn pasted text or an uploaded .txt / .csv / .tsv / .xlsx / .json / .docx / .odt into a question list.
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
const UNFINISHED_WORD = /\b(?:of|the|a|an|in|on|at|for|to|and|or|with|from|by|between|per|than|into|along|across|around|through|inside|within|including|excluding|without|under|over|above|below|near|beside|behind|about|as|its|their|this|that|these|those|ja|tai|sekä|der|die|das|des|dem|den|und|oder|von|im|zu|zur|zum|och|i|av|på|för|med|eller)\s*$/iu;
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
/** A line that is a question of its own: ends in '?', starts with a question word (en de fi sv), or is a quantity
 *  phrase or an instruction the way a test list is often written ('Total floor area', 'Number of doors on level 2',
 *  'Length of the corridor', 'Calculate the hall's perimeter', 'Anzahl der Türen', 'Huoneiden pinta-ala'). The main
 *  question box opens a list only when at least two lines are like this (a question with a note under it stays one). */
const Q_END = /[?？]\s*$/u;
const Q_WORD = /^\s*(?:what|how|which|where|when|who|is|are|does|do|can|list|find|count|wie|was|welche[rs]?|wo|gibt|mikä|mitkä|kuinka|paljonko|montako|missä|onko|hur|vad|vilken|vilka|finns)(?![\p{L}\d])/iu;
const Q_PHRASE = new RegExp(String.raw`^\s*(?:the\s+)?(?:` +
  // 'Total floor area', 'Net internal area of level 1', 'Overall length of the wall'
  String.raw`(?:total|net|gross|overall|combined|sum of(?: the)?)\s+(?:[\p{L}-]+\s+){0,2}(?:area|areas|length|lengths|count|number|perimeter|volume|skirting)(?![\p{L}])` +
  // 'Number of doors', 'Area of the kitchen', 'Floor area of room 101', 'Length of skirting in the hall'
  String.raw`|(?:floor\s+|room\s+|wall\s+)?(?:area|length|width|height|depth|perimeter|size|volume|number|count|amount|quantity|scale|title|thickness)\s+(?:of|in|for|on|at)(?![\p{L}])` +
  // instructions: 'Calculate …', 'Give the …', 'Measure …'
  String.raw`|(?:give|show|tell|calculate|compute|measure|estimate|determine|name|identify)\s+(?:me\s+)?\S` +
  // de / fi / sv quantity words at the start
  String.raw`|(?:anzahl|gesamtfläche|grundfläche|fläche|länge|breite|höhe|umfang)(?![\p{L}])` +
  String.raw`|(?:[\p{L}-]+\s+)?(?:pinta-?ala|kokonaisala|lukumäärä|määrä|pituus|leveys|korkeus|piiri)(?![\p{L}])` +
  String.raw`|(?:antal|total(?:a)?\s+golvyta|golvyta|längd|bredd|höjd|omkrets)(?![\p{L}])` +
  `)`, "iu");
export function questionLike(line: string): boolean {
  const t = String(line ?? "").trim();
  return t.length >= 3 && (Q_END.test(t) || Q_WORD.test(t) || Q_PHRASE.test(t));
}

export function parseText(text: string): BatchQuestion[] {
  const t = text.replace(/^﻿/, "");
  const lines = t.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length >= 2 && lines.filter((l) => l.includes("\t")).length >= Math.ceil(lines.length * 0.6)) return parseDelimited(t, "\t");
  if (lines.length >= 2 && lines.filter((l) => /^\s*\|.*\|\s*$/.test(l)).length >= Math.ceil(lines.length * 0.6))
    return fromRows(lines.filter((l) => !/^\s*\|?\s*:?-{3,}/.test(l)).map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim())));
  return dedupe(questionLines(lines.filter((l) => !SKIP.test(l))).keep.map(splitTag).map(inlineAnswer));
}

/** An expected answer as people write it next to a question: a number with at most a short unit ('115', '244,6 m²',
 *  '1 670,8 m2', '12.5 m', '7 kpl'); '1st floor' or a note is not one. */
const ANSWERISH = /^[-−]?\d[\d\s.,'’\u00a0\u202f]*(?:\s*(?!(?:st|nd|rd|th)\b)[\p{L}%][\p{L}\d²³%.^]{0,11}(?:\s(?:ft|feet|foot|m|metres?|meters?)\.?)?)?$|^1\s*:\s*\d[\d\s.,]*$/u;
const ANSWER_LEAD = /^(?:->|=>|[-–—=:→>]+|(?:expected(?: answer)?|answer|gold|vastaus|odotettu|antwort|svar|facit)\s*[:=]?)\s*/i;
/** 'How many doors? 115' / '… ? (115)' / '… ? → 244,6 m²' / '… ? Answer: 115': the answer after the question mark is
 *  the expected answer, never part of the question sent to the readers (before: the readers got 'How many doors? 115') */
export function inlineAnswer(it: BatchQuestion): BatchQuestion {
  const k = Math.max(it.q.lastIndexOf("?"), it.q.lastIndexOf("？"));
  if (k < 0 || it.expected) return it;
  const tail = it.q.slice(k + 1).trim().replace(ANSWER_LEAD, "").replace(/^\((.*)\)$/, "$1").trim();
  if (!tail || tail.length > 24 || !ANSWERISH.test(tail)) return it;
  return { ...it, q: it.q.slice(0, k + 1).trim(), expected: tail };
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

const Q_COL = /^(?:questions?(?:_?(?:text|en|fi|de|sv))?|q|query|prompt|kysymys(?:_?teksti)?|kysymykset|frage(?:n|text)?|fråga|frågor|frågan|spørsmål|spørgsmål|vraag|vragen|pregunta|domanda|text)$/i;
// the visitor's expected answer, in the languages a test list is written in ('Expected answer', 'Odotettu vastaus',
// 'Erwartete Antwort', 'Förväntat svar', 'Facit', 'Réponse attendue' …)
const EXP_COL = /^(?:expected(?:_(?:answers?|values?|results?))?|answers?|gold(?:_answer)?|truth|reference(?:_answer)?|ref_answer|correct(?:_answer)?|ground_truth|vastaus|vastaukset|odotettu(?:_(?:vastaus|arvo|tulos))?|oikea_vastaus|antwort(?:en)?|erwartet(?:e_antwort|er_wert)?|richtige_antwort|lösung|svar|förväntat(?:_(?:svar|värde))?|rätt_svar|facit|fasit|forventet(?:_svar)?|antwoord|verwacht(?:_antwoord)?|réponse(?:_attendue)?|respuesta(?:_esperada)?|risposta(?:_attesa)?)$/i;
/** the expected answer's unit in its own column ('Answer' 1670.8 · 'Unit' m2) */
const UNIT_COL = /^(?:units?|unit_of_measure|uom|yksikkö|yksiköt|einheit|enhet|enhed|eenheid|unité|unidad|unità)$/i;
// a unit that means 'a number of things': the bare number stays the expected answer (counts compare exactly)
const COUNT_UNIT = /^(?:count|pcs\.?|pieces?|kpl|kappaletta|stk\.?|st\.?|stück|qty|nos?\.?|nr\.?|#|-|–|ea|each|items?)$/i;
/** '1670.8' + 'm2' → '1670.8 m2'; an answer that already names a unit, a text answer or a count unit: as it is */
export function withUnit(expected: string, unit: string | undefined): string {
  const u = (unit ?? "").trim();
  if (!u || COUNT_UNIT.test(u) || !/^\s*[~≈]?\s*-?\d[\d\s.,'’\u00a0\u202f]*$/.test(expected)) return expected;
  return `${expected.trim()} ${u}`;
}
/** a header's unit in brackets: 'Expected answer (m²)' → 'm²' */
const headUnit = (c: string) => c.match(/[(\[]\s*([^)\]]{1,12}?)\s*[)\]]\s*$/)?.[1];
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
  // 'Expected answer (m²)' is the column 'expected_answer' with the unit m²
  const norm = (c: string) => c.trim().replace(/^["']|["']$/g, "").replace(/\s*[(\[][^)\]]*[)\]]\s*$/, "").trim().replace(/\s+/g, "_");
  // a title, a note or blank rows above the header ('Stoa test questions, Level 00' / 'Date: …' in an Excel sheet): the
  // header is the first of the first 8 rows that names the question column ('q' / 'text' only beside another known
  // column) and asks nothing; the rows above it are left out (before: the header became a question, answers were lost)
  const known = (c: string) => [EXP_COL, ID_COL, FILE_COL, SHEET_COL, UNIT_COL].some((re) => re.test(norm(c)));
  const hk = rows.slice(0, 8).findIndex((r) => !r.some((c) => /[?？]/.test(c)) &&
    r.some((c) => Q_COL.test(norm(c)) && (!/^(?:q|text)$/i.test(norm(c)) || r.some((d) => d !== c && known(d)))));
  if (hk > 0) rows = rows.slice(hk);
  const head = rows[0].map(norm);
  // a header asks nothing: a first row with a '?' is a question ('What is the roof material?' | 'text' is not a header)
  let qi = rows[0].some((c) => /[?？]/.test(c)) ? -1 : head.findIndex((h) => Q_COL.test(h));
  let ei = head.findIndex((h) => EXP_COL.test(h));
  let ii = head.findIndex((h) => ID_COL.test(h));
  let ui = head.findIndex((h) => UNIT_COL.test(h));
  const eUnit = ei >= 0 ? headUnit(rows[0][ei] ?? "") : undefined;
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
    // a header we do not know for the questions ('Kysymys nro', 'Prompt text'): drop a first row that has no '?' when
    // the others do, or whose other cells name columns we know ('No.', 'Item', 'Expected answer', 'Unit'); then its
    // other columns still count (before: the expected answers of such a list were lost)
    const qm = (r: string[]) => /[?？]/.test(r[qi] ?? "");
    const named = [ei, ii, di, si, ui].some((k) => k >= 0 && k !== qi);
    const isHead = rows.length >= 2 && !qm(rows[0]) && (named || (rows.length > 2 && rows.slice(1).filter(qm).length >= (rows.length - 1) / 2));
    if (isHead) body = rows.slice(1);
    if (!isHead) { ei = -1; ii = -1; di = -1; si = -1; ui = -1; }
    // the question column is never also the answer / id / drawing / unit column
    if (ei === qi) ei = -1;
    if (ii === qi) ii = -1;
    if (di === qi) di = -1;
    if (si === qi) si = -1;
    if (ui === qi) ui = -1;
  }
  // no header at all ('How many doors?' ⇥ '115' pasted from two Excel columns): the column right after the questions is
  // the expected answer when most rows fill it and every value there is a number with at most a unit, not 1, 2, 3 …
  if (qi >= 0 && ei < 0 && !Q_COL.test(head[qi] ?? "") && body.length >= 2) {
    const c = qi + 1, vals = body.map((r) => (r[c] ?? "").trim());
    const filled = vals.filter(Boolean);
    const ids = filled.every((v, k) => v === String(k + 1));
    if (filled.length >= 0.6 * body.length && filled.every((v) => v.length <= 24 && ANSWERISH.test(v)) && !ids && c !== ii && c !== di && c !== ui) ei = c;
  }
  const expOf = (r: string[]) => withUnit((r[ei] ?? "").trim(), ui >= 0 ? (r[ui] ?? "").trim() : eUnit);
  return dedupe(body.map((r) => ({
    q: cleanLine(r[qi] ?? ""),
    ...(ei >= 0 && (r[ei] ?? "").trim() ? { expected: expOf(r) } : {}),
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
    const pick = (re: RegExp) => Object.entries(o).find(([k, v]) => re.test(k.trim().replace(/\s+/g, "_")) && v !== null && v !== undefined && String(v).trim())?.[1];
    const ex = pick(EXP_COL), un = pick(UNIT_COL), id = pick(ID_COL), fl = pick(FILE_COL), sh = pick(SHEET_COL);
    const exp = ex !== undefined ? withUnit(String(ex), un !== undefined ? String(un) : undefined) : undefined;
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
/** A Word / LibreOffice document's questions: a table whose header names the question column ('Question', 'Kysymys' …)
 *  is read like a spreadsheet (expected answers, ids, drawings, sheets); else the paragraphs like pasted text (numbering,
 *  greeting, sign-off and header lines left out, wrapped lines joined); a table of questions without a known header
 *  wins only when it holds more lines ending in '?' than the paragraphs. */
export function fromDocument(d: { lines: string[]; tables: string[][][] }): BatchQuestion[] {
  const norm = (c: string) => c.trim().replace(/^["']|["']$/g, "").replace(/\s+/g, "_");
  const headed = d.tables.find((t) => t.length > 1 && t.slice(0, 8).some((r) => r.some((c) => Q_COL.test(norm(c)))));
  if (headed) return fromRows(headed);
  const fromText = parseText(d.lines.join("\n"));
  const nq = (qs: BatchQuestion[]) => qs.filter((x) => /[?？]\s*$/.test(x.q)).length;
  const tabled = d.tables.map((t) => fromRows(t)).sort((x, y) => nq(y) - nq(x))[0] ?? [];
  return nq(tabled) > nq(fromText) ? tabled : fromText;
}

/** A list file's bytes → text the way it was saved: a BOM (UTF-8 / UTF-16 LE / BE, Excel's 'Unicode Text'), UTF-16
 *  without one (every other byte 0), else UTF-8, and Windows-1252 when the bytes are not UTF-8 (Excel's 'CSV' on Windows:
 *  'Mikä on … pinta-ala?' read as UTF-8 was 'Mik� on …', and the readers got that). */
export function decodeText(b: Uint8Array): string {
  const dec = (enc: string, fatal = false) => new TextDecoder(enc, { fatal }).decode(b);
  if (b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) return dec("utf-8").replace(/^\uFEFF/, "");
  if (b[0] === 0xff && b[1] === 0xfe) return dec("utf-16le").replace(/^\uFEFF/, "");
  if (b[0] === 0xfe && b[1] === 0xff) return dec("utf-16be").replace(/^\uFEFF/, "");
  const n = Math.min(b.length, 400);
  let odd0 = 0, even0 = 0;
  for (let i = 0; i < n; i++) if (b[i] === 0) { if (i % 2) odd0++; else even0++; }
  if (n >= 8 && odd0 > n / 4 && even0 < n / 20) return dec("utf-16le");
  if (n >= 8 && even0 > n / 4 && odd0 < n / 20) return dec("utf-16be");
  try { return dec("utf-8", true); } catch { /* not UTF-8 */ }
  try { return dec("windows-1252"); } catch { return dec("utf-8"); }
}

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

// --- a PDF question list (pdflist.ts reads the text items with pdf.js; this part is pure) ------------------------------
// Lines = items on one baseline, left to right. A question wrapped onto the next line (a numbered item / question word
// without its '?' yet) is joined. An item counts when it has a '?': the text up to the last '?' is the question, a short
// number after it ('244.6 m2', 'expected: 7') is the expected answer. A drawing (hundreds of labels, at most a stray
// 'VERIFY?') is not a list: on 21 Stoa / team drawing PDFs none was taken for one.
export interface TextItem { s: string; x: number; y: number; w: number; h: number }

const PDF_QSTART = /^\s*(?:what|how|which|where|when|who|why|is|are|does|do|can|list|find|show|count|give|wie|was|welche[rsn]?|wo|gibt|mikä|mitkä|kuinka|paljonko|montako|missä|onko|hur|vad|vilken|vilka|var|finns|hvor|hva|hvilke[nt]?|hoeveel|wat|welke?|combien|quel(?:le)?s?|cuánt[oa]s|cuál(?:es)?|qual|quant[ie])\b/iu;
const PDF_MARK = /^\s*(?:(?:q(?:uestion)?|kysymys|frage|fråga)\s*\d+\s*[:.)\-–]|\(?\d{1,3}[.)]|[-*•·–—])\s*/iu;
const isQ = (s: string) => PDF_MARK.test(s) || PDF_QSTART.test(s);

/** One page's text items → its lines, top to bottom (PDF y grows upwards); a space where two items leave a gap. */
export function pdfLinesOf(items: TextItem[]): string[] {
  const its = items.filter((it) => it.s.length).slice(0, 8000);
  const sorted = [...its].sort((a, b) => b.y - a.y || a.x - b.x);
  const lines: { y: number; h: number; parts: TextItem[] }[] = [];
  for (const it of sorted) {
    const l = lines.find((L) => Math.abs(L.y - it.y) <= 0.5 * Math.max(L.h, it.h));
    if (l) l.parts.push(it);
    else lines.push({ y: it.y, h: it.h, parts: [it] });
  }
  return lines.sort((a, b) => b.y - a.y).map((L) => {
    let s = "", end = -Infinity;
    for (const p of L.parts.sort((a, b) => a.x - b.x)) {
      if (s && p.x - end > 0.15 * p.h && !/\s$/.test(s) && !/^\s/.test(p.s)) s += " ";
      s += p.s;
      end = p.x + p.w;
    }
    return s.replace(/\s+/g, " ").trim();
  }).filter(Boolean);
}

/** The questions of a PDF's pages, or why it is not a question list (`why`). */
export function pdfQuestions(pages: TextItem[][]): { qs: BatchQuestion[]; lines: number; why?: string } {
  const lines = pages.flatMap(pdfLinesOf);
  const items: string[] = [];
  let cur = "";
  for (const l of lines) {
    // a question wrapped onto this line: the item so far is a question without its end, and this line starts nothing new
    if (cur && isQ(cur) && !/[?？.!:]\s*$/.test(cur) && !isQ(l)) { cur += ` ${l}`; continue; }
    if (cur) items.push(cur);
    cur = l;
  }
  if (cur) items.push(cur);
  const qs: BatchQuestion[] = [];
  for (const it of items) {
    const k = Math.max(it.lastIndexOf("?"), it.lastIndexOf("？"));
    if (k < 0) continue;
    const q = cleanLine(it.slice(0, k + 1));
    // a real question: 3+ words, mostly letters, not a page of labels with a stray '?' ('1.3.4 1.3.3 … Maßstab 1:100 ?')
    const nonSpace = q.replace(/\s/g, "").length, letters = (q.match(/\p{L}/gu) ?? []).length;
    if (q.split(/\s+/).length < 3 || q.length > 300 || letters < 0.6 * nonSpace) continue;
    const rest = it.slice(k + 1).trim().replace(/^(?:expected|answer|gold|vastaus|antwort|svar)\s*[:=]?\s*/i, "");
    qs.push({ q, ...(rest && rest.length <= 24 && /^\(?[-−]?\d/.test(rest) ? { expected: rest.replace(/^\(|\)$/g, "") } : {}) });
  }
  const out = dedupe(qs);
  if (out.length && (out.length >= 2 ? out.length >= 0.2 * items.length : items.length <= 12)) return { qs: out, lines: lines.length };
  return { qs: [], lines: lines.length, why: lines.length > 40 ? "it looks like a drawing (its text has no list of questions)" : "no questions found in it (lines ending in '?')" };
}
