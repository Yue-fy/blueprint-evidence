/**
 * F8 'xref': after a hop to a schedule, put the answer's mark on the ROW the question names. Pure (no DOM, no registry);
 * used by index.ts, tested by rowmark.test.ts (`node --test`, run from tests/test_feat_xref.py).
 */
import type { Answer, Evidence } from "../../types";

/** The part of a hop markRow reads (stoa.features.xref.Hop: to_page, to_bbox = the row's box, to_row = its cells). */
export type RowHop = {
  to_page: number;
  to_bbox: number[] | null;
  to_row?: {
    key: string; table: string; cells: { text: string; bbox: number[] }[];
    /** the cell under the asked column's heading ('height' -> HEIGHT), read from the PDF's text (xref._row_cells) */
    asked?: { column: string; text: string; bbox: number[]; header: string;
      /** F8 c8: a size = the WIDTH and HEIGHT cells of the row (column 'size', text 'W x H') */
      parts?: { column: string; text: string; bbox: number[]; header: string }[] } | null;
  } | null;
};

/** F8 c7: a door attribute read on the schedule after a hop ('How tall is door 7.04A?' -> 7'-0") came back marked at the
 *  FIRST 7'-0" on the sheet (row 1.01A: the reader's text search), not in row 7.04A the question names. When the answer's
 *  value is printed in that row and none of its marks is in the row, the marks on that page move to the row's matching
 *  cell(s), exact (the PDF's own text). Pure: no DOM. */
const normCell = (s: string) => s.replace(/[″”“]/g, '"').replace(/[′’‘]/g, "'").replace(/\s+/g, "").toLowerCase();
const VALUE_TOKENS = /\d+\s*'\s*-?\s*\d+(?:\s+\d+\/\d+)?\s*"?|\d+(?:\.\d+)?/g;
/** '7' - 0"' / '1 3/4"' -> metres (null = not a feet-inch / inch size). */
export function ftInM(s: string): number | null {
  const t = s.replace(/[″”“]/g, '"').replace(/[′’‘]/g, "'");
  const m = t.match(/^\s*(\d+)\s*'\s*-?\s*(\d+)?(?:\s+(\d+)\/(\d+))?\s*"?\s*$/) ?? null;
  if (m) return Number(m[1]) * 0.3048 + (Number(m[2] ?? 0) + (m[3] ? Number(m[3]) / Number(m[4]) : 0)) * 0.0254;
  const i = t.match(/^\s*(\d+)?(?:\s*(\d+)\/(\d+))?\s*"\s*$/);
  if (i && (i[1] || i[2])) return (Number(i[1] ?? 0) + (i[2] ? Number(i[2]) / Number(i[3]) : 0)) * 0.0254;
  return null;
}
/** Does the answer's value say what the cell prints? Same text, or the same length in the answer's unit (2 %). */
function sameAs(a: Answer, cell: string): boolean {
  if (normCell(String(a.value)) === normCell(cell)) return true;
  const toks = (String(a.value).match(VALUE_TOKENS) ?? []).map(normCell);
  if (toks.includes(normCell(cell))) return true;
  const m = ftInM(cell);
  const v = typeof a.value === "number" ? a.value : Number.NaN;
  const per: Record<string, number> = { m: 1, mm: 0.001, cm: 0.01, ft: 0.3048, in: 0.0254 };
  const u = per[String(a.unit ?? "").toLowerCase()];
  return m !== null && Number.isFinite(v) && u !== undefined && Math.abs(v * u - m) <= 0.02 * m;
}
const tidy = (s: string) => s.replace(/\s*-\s*/g, "-");
/** 'What size is door 7.04A?': does the answer print the width and then the height (3'-0" wide x 7'-0" high)? A NaN
 *  part ('2'-10" wide x nan high'), one cell only, or another pair (height x thickness) does not agree. */
function sizeAgrees(a: Answer, w: string, h: string): boolean {
  const toks = (String(a.value ?? "").match(VALUE_TOKENS) ?? []).map(normCell);
  return toks.length >= 2 && toks[0] === normCell(w) && toks[1] === normCell(h) && !/\bnan\b/i.test(String(a.value));
}
export function markRow(a: Answer, h: RowHop, fallbackFile: string | null): Answer {
  const row = h.to_row;
  // F8 c9: a reader that gave NO value (G1 125d5a4c parses 'What size is door 104S?' as a text question; a text reader
  // may abstain) still gets the printed cell(s) of the named row when the asked column is known
  let noValue = a.value === null || a.value === undefined || String(a.value).trim() === "";
  if (!row?.cells?.length || (noValue && !row.asked) || !h.to_bbox || h.to_bbox.length < 4) return a;
  // F8 c7: the printed cell under the asked column wins over a reader that read another cell of the row (AW A-601 'height
  // of door 104S' -> 1 3/4" @0.85 = the THICKNESS column; the HEIGHT column prints 7' - 0")
  const asked = row.asked;
  // F8 c8: a size question reads TWO cells (width, height): the answer agrees only when it prints both, in that order
  const parts = asked?.parts?.length === 2 ? asked.parts : null;
  const agrees = (x: Answer) => noValue || !asked ? false : parts ? sizeAgrees(x, parts[0].text, parts[1].text) : sameAs(x, asked.text);
  if (asked && !agrees(a)) {
    const said = noValue ? "the reader gave no value" : `the reader said ${a.value}${a.unit ? ` ${a.unit}` : ""}`;
    const other = parts || noValue ? undefined : row.cells.find((c) => normCell(c.text) !== normCell(asked.text) && sameAs(a, c.text));
    if (noValue && (a.missing ?? []).length) {
      a.trace = [`xref: the reader's 'missing' (${(a.missing ?? []).join("; ")}) dropped: the named row prints the value`, ...(a.trace ?? [])];
      a.missing = [];
    }
    a.value = parts ? parts.map((c) => tidy(c.text)).join(" x ") : tidy(asked.text);
    a.unit = null;
    noValue = false;
    a.confidence = Math.max(Number(a.confidence ?? 0), 0.8);
    const why = parts
      ? `row ${row.key} of the ${row.table} prints ${parts.map((c) => `${c.column} ${c.text}`).join(", ")} (columns `
        + `${parts.map((c) => c.column.toUpperCase()).join(" / ")}); ${said}`
      : `the ${asked.header.trim()} heading's column prints ${asked.text} in row ${row.key} of the ${row.table}; `
        + `${said}${other ? ` (the cell '${other.text}' of another column)` : ""}`;
    a.assumptions = [parts ? `Read from the schedule's WIDTH and HEIGHT columns (width x height): ${why}`
      : `Read from the schedule's ${asked.column.toUpperCase()} column: ${why}`, ...(a.assumptions ?? [])];
    a.trace = [`xref: ${why} -> answer ${a.value}`, ...(a.trace ?? [])];
  } else if (asked) {
    // the reader and the column heading read the same printed cell(s): two reads agree on a printed value
    const c0 = Number(a.confidence ?? 0);
    if (c0 < 0.8) {
      a.confidence = 0.8;
      a.trace = [`xref: the schedule's ${parts ? "WIDTH / HEIGHT columns print" : `${asked.column.toUpperCase()} column prints`} `
        + `${asked.text} in row ${row.key}, as the reader said: confidence ${c0} -> 0.8`, ...(a.trace ?? [])];
    }
  }
  if (asked && agrees(a)) {
    // the card's Scope line (customer.ts conventionOf: the answer's first 'convention:' line wins) read 'Net internal area,
    // to the inner wall face' for 'What size is door 7.01A?' (the intent parses 'size' as an area): say what was read
    const what = parts ? "width x height" : asked.column;
    a.assumptions = [`convention: the ${what} printed in the ${row.table}, row ${row.key} (the scheduled size)`,
      ...(a.assumptions ?? []).filter((l) => !/^\s*convention\s*:/i.test(String(l)))];
  }
  const toks = (String(a.value).match(VALUE_TOKENS) ?? []).map(normCell).filter(Boolean);
  const hits = asked && agrees(a) ? (parts ?? [asked]).map((c) => ({ text: c.text, bbox: c.bbox }))
    : row.cells.filter((c) => normCell(c.text) !== normCell(row.key) && /\d/.test(c.text) && (toks.includes(normCell(c.text)) || normCell(String(a.value)) === normCell(c.text))).slice(0, 4);
  if (!hits.length) return a;
  const [x0, y0, x1, y1] = h.to_bbox;
  const ev = a.evidence ?? [];
  const onPage = (e: Evidence) => (e.page ?? 0) === h.to_page && Array.isArray(e.bbox) && e.bbox.length >= 4;
  const inRow = (e: Evidence) => {
    if (!onPage(e)) return false;
    const b = e.bbox as number[];
    const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
    return cx >= x0 - 1 && cx <= x1 + 1 && cy >= y0 - 1 && cy <= y1 + 1;
  };
  const onHit = (c: { bbox: number[] }) => ev.some((e) => onPage(e) && Math.abs((e.bbox as number[])[0] - c.bbox[0]) < 2 && Math.abs((e.bbox as number[])[1] - c.bbox[1]) < 2);
  if (asked ? hits.every(onHit) : ev.some(inRow)) return a; // the reader already marked the named row (the asked cell(s))
  const file = ev.find((e) => onPage(e) && e.file)?.file ?? fallbackFile;
  if (!file) return a;
  const marks: Evidence[] = hits.map((c) => ({ file, page: h.to_page, bbox: c.bbox, bbox_space: "pdf_pt",
    note: `'${c.text}' in row ${row.key} of the ${row.table}: the row the question names`, exact: true }));
  const moved = ev.filter(onPage).length;
  a.evidence = [...marks, ...ev.filter((e) => !onPage(e))];
  // T1's rule (stoa/trust/support.py): 'the printed value is marked' = supported; the old 'found by a search after the
  // answer was read: check it is the line the answer means' was about the stray mark, which is gone
  (a as unknown as { support?: unknown }).support = { state: "supported", shown: null, of: null,
    why: `the printed value${hits.length > 1 ? "s" : ""} ${hits.map((c) => `'${c.text}'`).join(" and ")} ${hits.length > 1 ? "are" : "is"} `
      + `marked in row ${row.key} of the ${row.table}, the row the question names` };
  a.trace = [`xref: marked the value in row ${row.key} of the ${row.table}, the row the question names`
    + (moved ? ` (the reader's own mark${moved > 1 ? "s were" : " was"} elsewhere on that sheet)` : ""), ...(a.trace ?? [])];
  return a;
}
