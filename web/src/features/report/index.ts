/**
 * F8 'report' — the MEASUREMENT REPORT (captain 21:51, idea 5; flag `?features=report`, API STOA_FEATURES=report).
 *
 * One click on an answer → a print-ready A4 page (browser print → PDF) for claims and change-order disputes:
 * the drawing crop with the answer's outline / measured lines / numbered marks (rendered by the API at print quality,
 * POST /feat/report/crop), a small locator of the whole sheet, the formula ('Laundry 10.95 - (0.82) = 10.13 m'), the scale
 * and where it came from, the convention, assumptions, what would make it exact, the method (reader, solver, cost tier,
 * model calls) and code version, the date, and a 'Checked by ____' block.
 *
 * Entry points: a toolbar button 'Report' while an answer is on screen; the right-click item 'Measurement report of this
 * answer'; and the answer card's action row when the registry offers one (W5: addAnswerAction, feature-detected).
 * The report opens in an overlay (an iframe, so printing prints the report only); nothing leaves this server.
 * Everything is read from the answer on screen: no new question is asked, no model is called.
 */
import { registerFeature } from "../registry";
import type { Answer, Evidence } from "../../types";
import { confidenceWords, conventionOf, costTier, supportOf } from "../../customer";
import { projectOf } from "../../projects";
import { cardCheck, clean, fileScaleN, fmtN, formulaLines, outlineCheck, SCALE_LINE, scaleN } from "./check";
/** UX 23:35 #1: one line-icon set (no colour emoji): a printer, stroked in currentColor like the other plug-ins. */
const PRINT_ICON = `<svg viewBox="0 0 24 24" width="100%" height="100%" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>`;
export { cardCheck, fileScaleN, geometryCheck, outlineCheck, ppmCheck, scaleN } from "./check";

type Meta = { file: string; page: number; pages: number; sheet: string | null; title: string | null; version: string; generated_at: string };

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const CONVENTION_LINE = /^\s*(?:convention\s*:|skirting\s*=|net\b|gross\b|area\s*=|count(?:ed)?\s*=)/i;

function fmtValue(a: Answer): string {
  const v = a.value;
  if (v === null || v === undefined) return "no value";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    const s = Math.abs(v) >= 100 ? v.toLocaleString("en-GB", { maximumFractionDigits: 1 }) : v.toLocaleString("en-GB", { maximumFractionDigits: 2 });
    return `${s}${a.unit && a.unit !== "count" ? " " + unitWord(a.unit) : ""}`;
  }
  return String(v);
}

const unitWord = (u: string) => ({ m2: "m²", sqm: "m²", ft2: "ft²", sf: "ft²", sqft: "ft²", m3: "m³" } as Record<string, string>)[u.toLowerCase()] ?? u;


/** A 3D model / CAD file: geometry in real units, so there is no drawing scale to read (and no sheet to crop). */
const isModel = (file: string) => /\.(ifc|ifczip|dxf|dwg)$/i.test(file);

function scaleText(a: Answer, file = ""): { scale: string; source: string; checks: string[] } {
  const r = (a.recognition ?? {}) as Record<string, unknown>;
  const model = isModel(file);
  const scale = String(r.scale ?? "").trim() || (model ? "true size (1:1)" : "");
  const src = String(r.scale_source ?? "").trim() || (model ? (/\.dxf$|\.dwg$/i.test(file) ? "cad" : "model") : "");
  const SRC: Record<string, string> = {
    printed: "printed on the sheet (title block / view title)", bar: "the drawn scale bar", scale_bar: "the drawn scale bar",
    user: "set by you (two clicks on a known length)", dimension: "a printed dimension string", dims: "printed dimension strings",
    assumed: "assumed (not printed: check it)", model: "the 3D model (true size)", cad: "the CAD file (true size)",
  };
  // the paper-size line ('... prints its scale for A1 ... this PDF page is A3: the drawing is at 1:400 on this file') and
  // a scanned page's 'A1 sheet at 150 dpi (from its scale bar ...)' belong with the scale: the geometry check uses them
  const PAPER = /\bon this file\b|\bsheet at \d+ dpi \(from its scale bar\b|\bread as an image at \d+ dpi\b/i;
  const checks = (a.assumptions ?? []).map(clean).filter((l) => (SCALE_LINE.test(l) || PAPER.test(l)) && !/^recognised as/i.test(l));
  // the scale the outline was measured at on THIS file's page (check.ts fileScaleN) vs the sheet's recognised one: an A1
  // sheet on an A3 page is 1:400 here although the sheet prints 1:200 — print both, the measured one first
  const fileN = fileScaleN(a), recN = scale ? scaleN(scale) : null;
  if (fileN && recN && fileN !== recN)
    return { scale: `1:${fileN} on this file's page · the sheet prints ${scale}`, source: SRC[src] ?? (src || "read from the drawing"), checks };
  return { scale: scale || "not found", source: SRC[src] ?? (src || (scale ? "read from the drawing" : "no scale found: lengths and areas are estimates")), checks };
}

function sourceOfEv(e: Evidence): string {
  if (e.exact === true) return "exact (the drawing's own geometry or text)";
  if (e.exact === false) return "approximate (traced / detected)";
  return "";
}

/** F8 xref's cross-check of this answer on another copy of the drawing (window.stoaCrossCheck): printed as a second,
 *  independent reading when it is for the same question on the same sheet. */
export type CrossCheck = { q: string; file: string; page: number; here: string; there: string; copy: string; verdict: string; at: number };
/** The answer's located evidence on a SECOND page or file (a door count from the plan + its schedule; an xref hop): its own
 *  crop, printed under the first one (at most 2 places: the report stays one page). */
export type More = { meta: Meta; crop: string; n: number; notes: string[] };

export function reportHtml(a: Answer, q: string, meta: Meta, crop: string | null, sheet: string | null, credit: string, xc: CrossCheck | null = null, more: More | null = null): string {
  const cw = confidenceWords(a.confidence ?? 0);
  const sup = supportOf(a);
  const conv = conventionOf(a, q);
  const sc = scaleText(a, meta.file);
  // a count needs no scale: say so instead of 'no scale found' (which reads as a defect of the answer)
  const isCount = a.unit === "count" || (typeof a.value === "number" && !a.unit && Number.isInteger(a.value));
  if (isCount && sc.scale === "not found") { sc.scale = "not needed (a count)"; sc.source = "symbols are counted, not measured"; }
  // F8 c8: a value READ from printed text (a schedule cell: 'What size is door 7.01A?' -> 3'-0" x 7'-0") is not measured:
  // 'no scale found: lengths and areas are estimates' read as a doubt about a printed value
  const lines = [...(a.assumptions ?? []), ...(a.trace ?? [])].map(String);
  const printedRead = lines.some((l) => /^xref: (?:marked the value in row|the schedule's|.*column prints)/.test(l) || /^Read from the schedule's /.test(l))
    || (typeof a.value === "string" && !/\d\s*(?:m²|m2|sq|sf|ft²)/i.test(a.value) && (a.evidence ?? []).length > 0 && (a.evidence ?? []).every((e) => e.exact && !e.polygon && !e.polyline));
  if (printedRead && sc.scale === "not found") { sc.scale = "not needed (a printed value)"; sc.source = "read from the drawing's own text, not measured"; }
  const forms = formulaLines(a);
  // a formula whose result is not the answer: the reader's 'with the openings deducted it would be 20.76 m' for a skirting
  // answered WITHOUT deductions (as asked) — printed as the other rule, never as the answer's arithmetic
  const notThis = (f: string) => {
    const m = f.match(/=\s*(-?\d[\d,]*(?:\.\d+)?)\s*(?:m²|m2|m|mm|ft²|ft|sf)?\s*$/i);
    const r = m ? Number(m[1].replace(/,/g, "")) : NaN, v = typeof a.value === "number" ? a.value : NaN;
    return Number.isFinite(r) && Number.isFinite(v) && v !== 0 && Math.abs(r - v) / Math.abs(v) > 0.02
      && (a.assumptions ?? []).some((l) => /\bwould be\b/i.test(l) && l.includes(m![1]));
  };
  const tier = costTier(a);
  const c = (a.cost ?? {}) as Record<string, number>;
  const calls = (Number(c.api_calls) || 0) + (Number(c.llm_calls) || 0);
  const used = new Set([...forms, ...sc.checks]);
  const convLines = (a.assumptions ?? []).map(clean).filter((l) => CONVENTION_LINE.test(l) && !used.has(l));
  convLines.forEach((l) => used.add(l));
  const rest = (a.assumptions ?? []).map(clean).filter((l) => !used.has(l) && !/^recognised as/i.test(l) && !forms.some((f) => l.includes(f)));
  const guids = (a.evidence ?? []).filter((e) => e.ifc_guid && !e.bbox);
  // this page of THIS file (an answer across two files of a set has items on page 1 of both)
  const evAll = (a.evidence ?? []).filter((e) => (e.page ?? 0) === meta.page && !(e.ifc_guid && !e.bbox)
    && (!e.file || !more || e.file.split("/").pop() === meta.file));
  const ev = evAll.slice(0, evAll.length > 18 ? 5 : 18);
  // a count of 115 doors lists 30 items: say the rest are on the crop, not silently cut (the crop marks up to 400)
  const evMore = evAll.length - ev.length;
  let chk: string | null = null;
  try { chk = crop ? outlineCheck(a, ev, meta.file, forms.some((f) => /\d\s*(?:[-−]|minus)\s*\(?\s*\d/.test(f))) : null; } catch { chk = null; }
  const r = (a.recognition ?? {}) as Record<string, unknown>;
  const id = `${meta.generated_at.replace(/\D/g, "").slice(0, 12)}-${Math.abs([...`${q}|${fmtValue(a)}`].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) | 0, 7)).toString(36).slice(0, 5)}`;
  const sheetName = [meta.sheet, meta.title].filter(Boolean).join(" · ") || meta.file;
  const li = (xs: string[]) => xs.map((x) => `<li>${esc(x)}</li>`).join("");
  const where = (m: Meta) => `${[m.sheet, m.title].filter(Boolean).join(" · ") || m.file}${m.file !== meta.file ? ` (${m.file})` : ""}, page ${m.page + 1}`;
  const two = !!(more && crop);
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Measurement report ${esc(id)}</title><style>
@page{size:A4;margin:12mm 14mm 12mm}
*{box-sizing:border-box}
body{font:10.5pt/1.45 "Inter Variable",Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#111827;margin:0;background:#fff}
.page{max-width:182mm;margin:0 auto;padding:10mm 0}
@media screen{body{background:#eef1f4}.page{background:#fff;padding:14mm;margin:12px auto;box-shadow:0 1px 6px rgba(0,0,0,.12)}}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid #0d9488;padding-bottom:6px;margin-bottom:10px}
h1{font-size:17pt;margin:0;letter-spacing:-.01em}
.sub{color:#4b5563;font-size:9pt}
h2{font-size:10.5pt;margin:12px 0 4px;color:#0f766e;text-transform:uppercase;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:10pt}
.grid b{color:#374151;font-weight:600}
.answer{display:flex;gap:14px;align-items:baseline;margin:6px 0 2px}
.value{font-size:22pt;font-weight:700;letter-spacing:-.02em}
.pill{display:inline-block;border:1px solid #9ca3af;border-radius:99px;padding:0 8px;font-size:9pt;color:#374151}
.q{font-style:italic;color:#374151}
figure{margin:6px 0 0;display:grid;grid-template-columns:1fr 46mm;gap:8px;align-items:start}
figure img{width:100%;border:1px solid #d1d5db;display:block}
figcaption{font-size:8.5pt;color:#4b5563;margin-top:3px}
.formula.check{background:#f8fafc;border-left-color:#64748b;font:9.5pt/1.45 "Inter Variable",Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.formula.alt{background:#f8fafc;border-left-color:#cbd5e1;color:#475569}
.formula{font:11pt/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f0fdfa;border-left:3px solid #0d9488;padding:4px 8px;margin:2px 0}
ol,ul{margin:2px 0 0 18px;padding:0}
li{margin:1px 0}
.ev small{color:#6b7280}
.sign{display:grid;grid-template-columns:repeat(4,1fr);gap:8px 14px;margin-top:8px;font-size:9.5pt}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.line{border-bottom:1px solid #111827;height:22px}
.box{display:inline-block;width:10px;height:10px;border:1px solid #111827;margin-right:4px;vertical-align:-1px}
footer{margin-top:12px;border-top:1px solid #d1d5db;padding-top:4px;font-size:8pt;color:#6b7280}
section{break-inside:avoid}
table.guids{border-collapse:collapse;font-size:8.5pt;width:100%}table.guids td,table.guids th{border-bottom:1px solid #e5e7eb;padding:1px 4px;text-align:left}table.guids th{color:#0f766e}code{font:8.5pt ui-monospace,Menlo,Consolas,monospace}
figure img.crop{max-height:88mm;object-fit:contain;background:#fff}
figure.more{grid-template-columns:1fr}figure.more img.crop{max-height:70mm}
@media print{.page{padding:0;max-width:none}body{font-size:8.7pt;line-height:1.33}h2{margin:6px 0 2px}.value{font-size:17pt}header{margin-bottom:4px}figure img.crop{max-height:${two ? 38 : evMore > 0 ? 36 : 50}mm}figure.more img.crop{max-height:34mm}figcaption{margin-top:1px;font-size:7.5pt}.formula{font-size:9.5pt;padding:2px 8px}.formula.check{font-size:8.5pt}.line{height:16px}.sign{margin-top:4px}footer{margin-top:6px}}
</style></head><body><div class="page">
<header><div><h1>Measurement report</h1><div class="sub">Blueprint Evidence · report ${esc(id)}</div></div>
<div class="sub" style="text-align:right">${esc(meta.generated_at)}<br>code ${esc(meta.version)}</div></header>
<section><div class="grid">
<b>Drawing</b><span>${esc(sheetName)}</span>
<b>File</b><span>${esc(meta.file)}${isModel(meta.file) ? " · 3D model" : ` · page ${meta.page + 1} of ${meta.pages}`}</span>
${credit ? `<b>Source</b><span>${esc(credit)}</span>` : ""}
</div></section>
<section><h2>Question and answer</h2>
<div class="q">“${esc(q)}”</div>
<div class="answer"><span class="value">${esc(fmtValue(a))}</span><span class="pill">${esc(cw.word)} · ${Math.round((a.confidence ?? 0) * 100)} %</span>${sup ? `<span class="pill">${esc(sup.state)}</span>` : ""}</div>
${sup?.why ? `<div class="sub">${esc(sup.why)}</div>` : ""}
</section>
<section><h2>${isModel(meta.file) && !crop ? "Where in the model" : "Where on the drawing"}</h2>
${crop ? `<figure><div><img class="crop" src="${crop}" alt="drawing crop with the measured outline"><figcaption>Teal = the outline / items the answer used; orange = lines deducted or measured on their own${ev.length > 1 ? "; numbers = the evidence items below" : ""}. Drawn on the sheet's own rendering.</figcaption></div>
<div>${sheet ? `<img src="${sheet}" alt="whole sheet with the crop framed"><figcaption>Whole sheet; orange frame = the crop.</figcaption>` : ""}</div></figure>
${two ? `<figure class="more"><div><img class="crop" src="${more!.crop}" alt="the answer's items on ${esc(where(more!.meta))}"><figcaption><b>Also used: ${more!.n} item${more!.n === 1 ? "" : "s"} on ${esc(where(more!.meta))}</b>${more!.notes.length ? ` — ${esc(more!.notes.slice(0, 3).join("; "))}${more!.notes.length > 3 ? " …" : ""}` : ""}.</figcaption></div></figure>` : ""}`
    : guids.length ? `<p style="margin:2px 0">This answer comes from <b>${guids.length} element${guids.length === 1 ? "" : "s"} of the 3D model</b>. Each is named below by its IFC GlobalId: select it in the model viewer (or any IFC viewer) to check it.</p>
<table class="guids"><thead><tr><th>#</th><th>GlobalId</th><th>Element</th></tr></thead><tbody>${guids.slice(0, 40).map((e, i) => `<tr><td>${i + 1}</td><td><code>${esc(e.ifc_guid)}</code></td><td>${esc(e.note || "")}</td></tr>`).join("")}</tbody></table>${guids.length > 40 ? `<p class="sub">… and ${guids.length - 40} more (the full list is in the answer's evidence).</p>` : ""}`
    : `<p class="sub">This answer points at no place on the drawing (no located evidence): it cannot be checked on the sheet.</p>`}
</section>
<section><h2>How it was measured</h2>
${forms.length ? forms.map((f) => `<div class="formula${notThis(f) ? " alt" : ""}">${notThis(f) ? `<span class="sub">Not this answer (the other rule, for comparison):</span> ` : ""}${esc(f)}</div>`).join("") : chk || xc ? "" : `<p class="sub">No arithmetic shown by the reader: the value was read or counted directly (see the evidence).</p>`}
${chk ? `<div class="formula check">${esc(chk)}</div>` : ""}
${xc ? `<div class="formula check">Second reading on another copy of this drawing: this sheet ${esc(xc.here)} · ${esc(xc.copy)} ${esc(xc.there)}; ${esc(xc.verdict)}</div>` : ""}
${ev.length ? `<ol class="ev">${ev.map((e) => `<li>${esc(e.note || "marked item")}${sourceOfEv(e) ? ` <small>· ${esc(sourceOfEv(e))}</small>` : ""}</li>`).join("")}</ol>` : ""}
${evMore > 0 ? `<p class="sub">… and ${evMore} more item${evMore === 1 ? "" : "s"} (${evAll.length} in all${evAll.length <= 400 ? ", every one marked on the crop above" : ""}).</p>` : ""}
</section>
<div class="two"><section><h2>Scale</h2><div class="grid"><b>Scale</b><span>${esc(sc.scale)}</span><b>Source</b><span>${esc(sc.source)}</span></div>
${sc.checks.length ? `<ul>${li(sc.checks)}</ul>` : ""}</section>
<section><h2>Method</h2><div class="grid">
<b>Reader</b><span>${esc(String(r.label ?? r.kind ?? "") || "—")}${r.reader ? ` · ${esc(plainMethod(String(r.reader)) || r.reader)}` : ""}</span>
<b>Solver</b><span>${a.solver ? (/^read from the drawing \(/.test(plainMethod(a.solver)) || plainMethod(a.solver) === a.solver ? esc(a.solver) : `${esc(plainMethod(a.solver))} <small class="sub">(${esc(a.solver)})</small>`) : "—"}</span>
<b>Model calls</b><span>${calls ? `${calls} (${esc(tier.why)})` : `none — rules on the ${isModel(meta.file) ? "model's own elements and properties" : "drawing's own lines and text"}`}</span>
<b>Cost tier</b><span>${esc(tier.word)}${a.cached ? " · repeated from the answer cache" : ""}</span>
</div></section></div>
<section><h2>Convention</h2>${convLines.length
  // the reader's own rule ('skirting = the room's traced outline … minus every opening …') is the one it applied
  ? convLines.map((l) => `<p style="margin:2px 0">${esc(l)}</p>`).join("")
  : conv ? `<p style="margin:2px 0">${esc(conv.text)}</p>` : `<p class="sub">No special measuring rule applied.</p>`}${conv?.alt ? `<p class="sub" style="margin:2px 0">Alternative: ${esc(conv.alt)}</p>` : ""}</section>
${rest.length ? `<section><h2>Assumptions</h2><ul>${li(rest)}</ul></section>` : ""}
${(a.missing ?? []).length ? `<section><h2>What would make it exact</h2><ul>${li((a.missing ?? []).map(clean))}</ul></section>` : ""}

<section><h2>Check</h2>
<div><span class="box"></span>Accepted as shown &nbsp;&nbsp; <span class="box"></span>Corrected to: ____________________</div>
<div class="sign"><div>Checked by<div class="line"></div></div><div>Date<div class="line"></div></div>
<div>Role / company<div class="line"></div></div><div>Signature<div class="line"></div></div></div>
</section>
<footer>Blueprint Evidence · ${isModel(meta.file) ? "from the model's own elements and properties" : calls ? `read with ${calls} AI model call${calls === 1 ? "" : "s"} and the drawing's own lines and text` : "from the drawing's own lines and text"} · verify ${isModel(meta.file) ? `in ${esc(meta.file)}` : `on ${esc(meta.sheet || meta.file)}, page ${meta.page + 1}${two ? ` and on ${esc(where(more!.meta))}` : ""}`} before relying on it.</footer>
</div></body></html>`;
}

type TkRow = { n: number | null; section: string; item: string; copy?: number; copies?: number; quantity: string; unit: string; value: number | string | null; confidence: number; method: string; assumptions?: string[]; evidence: Evidence[]; storey?: string | null; ifc_guid?: string | null;
  review?: string | null; room_type?: string | null; type_label?: string | null; type_color?: string | null; fixed?: unknown; derived?: boolean };
type TkType = { type: string; label: string | null; color: string | null };
type Tk = { available: boolean; rows: TkRow[]; scale_n?: number; source?: string; rooms?: number; kind?: string | null; types?: TkType[] };

/** A reader's method in plain words (UX 23:35 MINOR 27: no internal reader names on a printed record):
 *  'lineplan tracer (per label)' -> "traced from the drawing's lines", 'pipeline/openings' -> 'door and window symbols'. */
export function plainMethod(m: string | null | undefined): string {
  let t = String(m ?? "").trim();
  if (!t) return "";
  t = t.replace(/\s*\((?:stoa|web)\.[\w./]+\)/g, "");                       // '(stoa.evidence.areatable)'
  const solver: Record<string, string> = {
    plan_rooms: "room outline from the drawing's lines", openings: "door and window symbols on the sheet", dxf: "the DXF's lines",
    pdf_vector: "the PDF's lines", raster: "traced on the picture", lineplan: "traced from the drawing's lines",
    schedule: "the printed schedule", ifc: "the 3D model", vlm_direct: "AI model reading the picture",
    vlm_lora: "AI model reading the picture", vlm: "AI model reading the picture", agent: "AI agent reading the drawing",
    prior: "a typical value (not measured)", dummy: "a typical value (not measured)", facade: "symbols on the elevation",
    area_table: "the printed area table", slabs: "the printed dimensions", slab_area: "slab outline and printed dimensions",
    piping: "pipe symbols and tags on the sheet", piping_img: "pipe symbols and tags on the picture", label_count: "printed labels counted",
    area_tags: "areas printed in the room tags", pdf_text: "the sheet's printed text", stated_count: "a count printed on the sheet",
    room_range: "the printed room numbers", footprint: "the building outline", room_graph: "rooms and their doors on the plan",
    pipe_runs: "pipe runs measured on the sheet", lengths: "lines measured at the scale", room_size: "the room's printed size",
    line_rooms: "room outline from the drawing's lines", door_detect: "door symbols found by the detector",
    gemini_vision: "AI reading of the picture (Gemini)", schedule_rows: "rows of the printed schedule",
  };
  const sm = t.match(/^(?:pipeline\/)?([a-z_]+)(?:\/[a-z_]+)?$/);
  if (sm && solver[sm[1]]) return solver[sm[1]];
  return t
    .replace(/^lineplan tracer\b(?:\s*\(per label\))?/i, "traced from the drawing's lines")
    .replace(/^lineplan skirting\b(?:\s*\(per label\))?/i, "traced outline's perimeter minus the door openings")
    .replace(/^openings swing arcs\b/i, "door swing arcs")
    .replace(/\(linework\)\s*/g, "")
    .replace(/^pipeline\/([a-z_]+)$/i, "read from the drawing ($1)");
}

/** The takeoff as one report: every room outlined and numbered on the plan, a room-by-room table (area, baseboard,
 *  doors, windows, the lowest confidence of its row), the sheet totals, the scale, and the sign-off block. */
/** F4's Conventions tab announces the visitor's choice (window.stoaConventions, event 'stoa:conventions'): the report prints
 *  F2's takeoff as traced (net, sets), so a different choice is named next to it instead of silently disagreeing. */
type ConvDetail = { conventions?: { area?: string; wetSkirting?: boolean; deductOpenings?: boolean; doors?: string }; labels?: Record<string, string> | null };
export function convNote(d: ConvDetail | null | undefined): string {
  const c = d?.conventions;
  if (!c || (c.area === "net" && c.wetSkirting !== false && c.deductOpenings !== false && c.doors !== "leaves")) return "";
  const l = d?.labels ?? {};
  const parts = [l.area ?? `area ${c.area}`, l.skirt ? `skirting: ${l.skirt}` : "", l.doors ?? (c.doors === "leaves" ? "door leaves" : "")].filter(Boolean);
  return `Your Conventions tab is set to: ${parts.join("; ")}. This report prints the takeoff as traced (net floor area, door sets, openings deducted); the Conventions tab shows the totals your way.`;
}

export function takeoffHtml(tk: Tk, meta: Meta, crop: string | null, credit: string, conv = ""): string {
  const QN: Record<string, string> = { area: "area", baseboard: "skirting", doors: "doors", windows: "windows" };
  // an IFC model's takeoff (F2 23:14: kind 'ifc model'): rooms are IfcSpaces named by GlobalId + storey, nothing to draw
  const model = /ifc/i.test(String(tk.kind ?? "")) || isModel(meta.file);
  type Room = { item: string; copy?: number; copies?: number; q: Record<string, TkRow>; guid?: string; storey?: string; type?: string; label?: string; color?: string };
  const rooms = new Map<number, Room>();
  for (const r of tk.rows) {
    if (r.n == null || !QN[r.quantity]) continue;
    const g = rooms.get(r.n) ?? { item: r.item, copy: r.copy, copies: r.copies, q: {} };
    g.q[r.quantity] = r;
    g.type ??= r.room_type ?? undefined;
    g.label ??= r.type_label ?? undefined;
    g.color ??= r.type_color ?? undefined;
    g.guid ??= r.ifc_guid ?? r.evidence.find((e) => e.ifc_guid)?.ifc_guid ?? undefined;
    g.storey ??= r.storey ?? undefined;
    rooms.set(r.n, g);
  }
  const num = (r?: TkRow, d = 2) => (r && typeof r.value === "number" ? r.value.toLocaleString("en-GB", { maximumFractionDigits: d }) : "—");
  const low = (g: { q: Record<string, TkRow> }) => Math.min(...Object.values(g.q).map((r) => r.confidence ?? 0));
  // F2's review queue (finish(): 'rough reading', 'no reading', 'a typical value …'): those rows stay OUT of every total,
  // as in the panel; an older takeoff without it: confidence under 40 %
  const hasReview = tk.rows.some((r) => "review" in r);
  const inReview = (r?: TkRow) => !!r && (hasReview ? !!r.review : (r.confidence ?? 0) < 0.4);
  const rv = (g: Room) => Object.values(g.q).some((r) => inReview(r));
  const review = [...rooms.values()].filter(rv).length;
  const tot = tk.rows.filter((r) => r.section !== "room");
  const sw = (c?: string | null) => (c ? `<span class="sw" style="background:${esc(c)}"></span>` : "");
  const val = (r?: TkRow) => (r && typeof r.value === "number" && !inReview(r) ? r.value : null);
  // a subtotal per room name (estimators price by room type: '16 × ELUTUBA+KÖÖK = 357.6 m²'); only when a name repeats
  type Sub = { item: string; n: number; review: number; missing: number; area: number[]; baseboard: number[]; doors: number[]; windows: number[] };
  const subs = new Map<string, Sub>();
  // by room TYPE (F2's taxonomy and colours: the plan above is coloured the same way, so the legend is this table)
  const typs = new Map<string, Sub & { color?: string }>();
  const add = (m: Map<string, Sub & { color?: string }>, key: string, label: string, g: Room) => {
    const b = m.get(key) ?? { item: label, n: 0, review: 0, missing: 0, area: [], baseboard: [], doors: [], windows: [], color: g.color };
    b.n += 1;
    if (rv(g)) b.review += 1;
    for (const k of ["area", "baseboard", "doors", "windows"] as const) {
      const v = val(g.q[k]);
      if (v !== null) b[k].push(v);
      else if (k === "area" && !inReview(g.q.area)) b.missing += 1;
    }
    m.set(key, b);
  };
  for (const g of rooms.values()) {
    add(subs, g.item, g.item, g);
    if (g.type) add(typs, g.type, g.label ?? g.type, g);
  }
  const order = (tk.types ?? []).map((t) => t.type);
  const byType = [...typs.entries()].sort((x, y) => (order.indexOf(x[0]) + 1 || 99) - (order.indexOf(y[0]) + 1 || 99)).map(([, b]) => b);
  const typeArea = byType.reduce((p, b) => p + b.area.reduce((q, v) => q + v, 0), 0);
  const areaTot = tot.find((r) => r.quantity === "area_sum");
  const adds = typeof areaTot?.value === "number" && byType.length > 1
    ? (Math.abs(typeArea - areaTot.value) <= 0.01 * Math.max(1, areaTot.value) ? `the types add up to ${fmtN(typeArea)} m² = the net floor area ✓` : `the types add up to ${fmtN(typeArea)} m² (net floor area ${fmtN(areaTot.value)} m²)`)
    : "";
  // names that repeat get their own row; the one-off rooms are added up in one 'other names' row (else the table is as long
  // as the room list)
  const rep = [...subs.values()].filter((b) => b.n > 1).sort((x, y) => y.n - x.n || x.item.localeCompare(y.item));
  const ones = [...subs.values()].filter((b) => b.n === 1);
  const other: Sub = { item: `other names (${ones.length}, one room each)`, n: ones.length, review: 0, missing: 0, area: [], baseboard: [], doors: [], windows: [] };
  for (const o of ones) {
    other.review += o.review; other.missing += o.missing;
    for (const k of ["area", "baseboard", "doors", "windows"] as const) other[k].push(...o[k]);
  }
  const byName = rep.length ? (ones.length ? [...rep, other] : rep) : [];
  const sum = (xs: number[], d = 2) => (xs.length ? xs.reduce((p, v) => p + v, 0).toLocaleString("en-GB", { maximumFractionDigits: d }) : "—");
  const roomCell = (g: Room) => `${sw(g.color)}${esc(g.item)}${(g.copies ?? 1) > 1 ? ` <span class="sub">(${g.copy}/${g.copies})</span>` : ""}${g.guid ? `<br><span class="sub">${g.storey ? `${esc(g.storey)} · ` : ""}<code>${esc(g.guid)}</code></span>` : ""}`;
  const cell = (r?: TkRow, d = 2) => (inReview(r) && typeof r?.value === "number" ? `<span class="sub">(${num(r, d)})</span>` : num(r, d));
  const id = `${meta.generated_at.replace(/\D/g, "").slice(0, 12)}-tk`;
  const sheetName = [meta.sheet, meta.title].filter(Boolean).join(" · ") || meta.file;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Takeoff report ${esc(id)}</title><style>
@page{size:A4;margin:12mm 12mm 12mm}
*{box-sizing:border-box}
body{font:9.5pt/1.4 "Inter Variable",Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#111827;margin:0;background:#fff}
.page{max-width:186mm;margin:0 auto}
@media screen{body{background:#eef1f4}.page{background:#fff;padding:12mm;margin:12px auto;box-shadow:0 1px 6px rgba(0,0,0,.12)}}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid #0d9488;padding-bottom:5px;margin-bottom:8px}
h1{font-size:16pt;margin:0}.sub{color:#4b5563;font-size:8.5pt}
h2{font-size:9.5pt;margin:10px 0 3px;color:#0f766e;text-transform:uppercase;letter-spacing:.04em}
img.plan{width:100%;max-height:150mm;object-fit:contain;border:1px solid #d1d5db}
code{font:7.5pt ui-monospace,Menlo,Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:8.5pt}th,td{border-bottom:1px solid #e5e7eb;padding:2px 4px;text-align:right}
th:nth-child(2),td:nth-child(2),th:last-child,td:last-child{text-align:left}th{background:#f0fdfa;color:#0f766e}
tr.rv td{background:#fff7ed}thead{display:table-header-group}tr{break-inside:avoid}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:baseline;border:1px solid rgba(0,0,0,.25);-webkit-print-color-adjust:exact;print-color-adjust:exact}
.sign{display:grid;grid-template-columns:repeat(4,1fr);gap:8px 14px;margin-top:8px}.line{border-bottom:1px solid #111827;height:20px}
footer{margin-top:10px;border-top:1px solid #d1d5db;padding-top:4px;font-size:8pt;color:#6b7280}
</style></head><body><div class="page">
<header><div><h1>Takeoff report</h1><div class="sub">Blueprint Evidence · report ${esc(id)}</div></div><div class="sub" style="text-align:right">${esc(meta.generated_at)}<br>code ${esc(meta.version)}</div></header>
<div class="sub"><b>Drawing</b> ${esc(sheetName)} · ${esc(meta.file)} · page ${meta.page + 1}${credit ? ` · ${esc(credit)}` : ""}</div>
${model ? `<div class="sub"><b>Scale</b> true size (3D model) · <b>Rooms</b> ${rooms.size} (${esc(tk.source ?? "the model's spaces")}) · <b>Convention</b> quantities as the model's spaces carry them (method per row); each row names its element's IFC GlobalId</div>`
  : `<div class="sub"><b>Scale</b> ${tk.scale_n ? `1:${Math.round(tk.scale_n)}` : "not found"} · <b>Rooms</b> ${rooms.size} (${esc(tk.source ?? "room labels")}) · <b>Convention</b> area = traced floor outline at the wall faces (net); skirting = that perimeter minus the door openings</div>`}
${conv ? `<div class="sub" style="margin-top:2px;color:#9a3412">${esc(conv)}</div>` : ""}
${crop ? `<h2>Plan · numbers = rows below${byType.length ? " · colours = room types" : ""}</h2><img class="plan" src="${crop}" alt="plan with every room outlined, coloured by room type and numbered">` : ""}
${byType.length ? `<h2>By room type${adds ? ` · ${esc(adds)}` : ""}</h2><table><thead><tr><th>Rooms</th><th>Type</th><th>Area m²</th><th>Skirting m</th><th>Doors</th><th>Windows</th><th>In review</th><th></th></tr></thead><tbody>
${byType.map((b) => `<tr><td>${b.n}</td><td>${sw(b.color)}${esc(b.item)}</td><td>${sum(b.area)}</td><td>${sum(b.baseboard)}</td><td>${sum(b.doors, 0)}</td><td>${sum(b.windows, 0)}</td><td>${b.review || ""}</td><td class="sub">${b.missing ? `${b.missing} without a value` : ""}</td></tr>`).join("")}
</tbody></table>` : ""}
<h2>Rooms</h2><table><thead><tr><th>#</th><th>Room</th><th>Area m²</th><th>Skirting m</th><th>Doors</th><th>Windows</th><th>Conf.</th><th>Method</th></tr></thead><tbody>
${[...rooms.entries()].sort((a, b) => a[0] - b[0]).map(([n, g]) => `<tr${rv(g) ? ' class="rv"' : ""}><td>${n}</td><td>${roomCell(g)}</td><td>${cell(g.q.area)}</td><td>${cell(g.q.baseboard)}${g.type === "wet" && typeof g.q.baseboard?.value === "number" ? ' <span class="sub">wet</span>' : ""}</td><td>${cell(g.q.doors, 0)}</td><td>${cell(g.q.windows, 0)}</td><td>${Math.round(low(g) * 100)} %</td><td>${esc(plainMethod((g.q.area ?? Object.values(g.q)[0])?.method) || (g.q.area?.review ?? Object.values(g.q).find((r) => r.review)?.review ?? ""))}${(g.q.area?.fixed || g.q.baseboard?.derived) && !/\b(?:by a user|your)\b/i.test(String(g.q.area?.method ?? "")) ? ' <span class="sub">(measured by you)</span>' : ""}</td></tr>`).join("")}
</tbody></table>
${byName.length ? `<h2>By room name · rooms with the same name added up</h2><table><thead><tr><th>Rooms</th><th>Name</th><th>Area m²</th><th>Skirting m</th><th>Doors</th><th>Windows</th><th>In review</th><th></th></tr></thead><tbody>
${byName.map((b) => `<tr><td>${b.n}</td><td>${esc(b.item)}</td><td>${sum(b.area)}</td><td>${sum(b.baseboard)}</td><td>${sum(b.doors, 0)}</td><td>${sum(b.windows, 0)}</td><td>${b.review || ""}</td><td class="sub">${b.missing ? `${b.missing} without a value` : ""}</td></tr>`).join("")}
</tbody></table>` : ""}
${tot.length ? `<h2>Totals</h2><table><tbody>${tot.map((r) => `<tr><td></td><td>${esc(r.item.replace(/baseboard/gi, "skirting"))}${r.section === "sheet" ? ` · ${esc(QN[r.quantity] ?? r.quantity)}` : ""}</td><td>${num(r)} ${esc(r.unit === "count" ? "" : unitWord(r.unit ?? ""))}</td><td>${Math.round((r.confidence ?? 0) * 100)} %</td><td>${esc(plainMethod(r.method))}</td></tr>`).join("")}</tbody></table>` : ""}
${review ? `<p class="sub">${review} room${review === 1 ? "" : "s"} (shaded) in review: ${hasReview ? "a rough or missing reading; its value is shown in brackets and" : "below 40 % confidence and"} left out of the totals. Measure ${review === 1 ? "it" : "them"} before pricing.</p>` : ""}
<h2>Check</h2><div class="sign"><div>Checked by<div class="line"></div></div><div>Date<div class="line"></div></div><div>Role / company<div class="line"></div></div><div>Signature<div class="line"></div></div></div>
<footer>Blueprint Evidence · ${model ? `from the model's own elements · every row names its GlobalId in ${esc(meta.file)}; select it in any IFC viewer to verify` : `from the drawing's own lines and text · every row is outlined on ${esc(meta.sheet || meta.file)}, page ${meta.page + 1}; verify there`} before relying on it.</footer>
</div></body></html>`;
}

/** The source line of a printed report: a catalogue sample's project credit (UX 23:35: a sample opened through the upload
 *  path said 'your upload: not in the dataset'), else the file's own credit + licence (an upload's licence is its owner's). */
export function creditOf(f: { path: string; credit?: string; license?: string } | null | undefined): string {
  if (!f) return "";
  let p: { name?: string; credit?: string } | undefined;
  try { p = projectOf(f.path); } catch { p = undefined; }
  if (p?.credit) return `${p.name ? `sample project “${p.name}” · ` : ""}${p.credit}`;
  return [f.credit, /^your upload/i.test(f.credit ?? "") ? "" : f.license].filter(Boolean).join(" · ");
}

/** Is the pointer on one of the answer's marks on this page of this file (its box, padded by ~1 % of the mark or 6 units)? */
export function onAnswerMark(a: Answer, p: [number, number] | null, page: number, file: string, space?: string): boolean {
  if (!p) return false;
  const pt = { x: p[0], y: p[1] };
  const base = (f: string) => String(f).split("/").pop();
  return (a.evidence ?? []).some((e) => {
    const b = e.bbox;
    if (!b || b.length !== 4 || (e.page ?? 0) !== page) return false;
    if (e.file && file && base(e.file) !== base(file)) return false;
    if (space && e.bbox_space && e.bbox_space !== space) return false;
    const pad = Math.max(6, 0.01 * Math.max(Math.abs(b[2] - b[0]), Math.abs(b[3] - b[1])));
    return pt.x >= Math.min(b[0], b[2]) - pad && pt.x <= Math.max(b[0], b[2]) + pad && pt.y >= Math.min(b[1], b[3]) - pad && pt.y <= Math.max(b[1], b[3]) + pad;
  });
}

/** This browser's id (F2 23:52: takeoff fixes are kept per visitor under 'stoa.visitor', set by F2's takeoff / F7 / T3): read
 *  only, so the takeoff report shows THIS person's Measure fixes like their Quantities tab. '' when there is none. */
function visitorQ(): string {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  let v: unknown = null;
  try { v = localStorage.getItem("stoa.visitor"); } catch { /* storage blocked */ }
  if (!ok(v)) v = (window as unknown as { stoaVisitor?: string }).stoaVisitor;
  return ok(v) ? `&visitor=${encodeURIComponent(v)}` : "";
}

registerFeature("report", (api) => {
  let host: HTMLDivElement | null = null;

  let returnFocus: HTMLElement | null = null;
  // closing gives the keyboard focus back to what had it (the card's Report pill, the toolbar button)
  const close = () => {
    if (!host) return;
    host.remove();
    host = null;
    try { returnFocus?.focus(); } catch { /* gone */ }
    returnFocus = null;
  };

  const blobUrl = async (path: string, page: number, evidence: Evidence[], mode: "crop" | "sheet", width: number): Promise<string | null> => {
    try {
      const r = await fetch("/api/feat/report/crop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, page, evidence, mode, width }) });
      if (!r.ok) return null;
      const b = await r.blob();
      // a data: URL survives the iframe and the print dialog (a blob: URL is revoked when the overlay closes)
      return await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result)); fr.onerror = () => res(null); fr.readAsDataURL(b); });
    } catch {
      return null;
    }
  };

  /** The overlay: a print / close bar over an iframe holding the A4 page (printing prints the page only). */
  function shell(title: string): HTMLIFrameElement {
    close();
    host = document.createElement("div");
    host.className = "feat-report";
    host.setAttribute("role", "dialog");
    host.setAttribute("aria-label", title);
    host.style.cssText = "position:fixed;inset:0;z-index:2147483000;background:rgba(17,24,39,.55);display:flex;flex-direction:column;align-items:center;padding:12px";
    host.innerHTML = `<div style="display:flex;gap:8px;width:min(860px,100%);justify-content:flex-end;margin-bottom:8px">
      <span style="color:#fff;margin-right:auto;font:600 14px system-ui">${esc(title)} — print or save as PDF</span>
      <button type="button" data-r="print" class="feat-row" style="padding:6px 14px">Print / Save PDF</button>
      <button type="button" data-r="close" class="feat-row" style="padding:6px 14px">Close</button></div>
      <iframe title="${esc(title)}" style="flex:1;width:min(860px,100%);border:0;border-radius:8px;background:#fff"></iframe>`;
    document.body.append(host);
    const frame = host.querySelector("iframe")!;
    frame.srcdoc = `<p style="font:14px system-ui;padding:24px">Preparing the report…</p>`;
    host.querySelector('[data-r="close"]')!.addEventListener("click", close);
    // a dialog takes the keyboard focus (Esc / Enter work at once; screen readers announce it)
    if (!returnFocus) returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    host.setAttribute("aria-modal", "true");
    host.querySelector<HTMLButtonElement>('[data-r="close"]')?.focus();
    host.querySelector('[data-r="print"]')!.addEventListener("click", () => frame.contentWindow?.print());
    host.addEventListener("keydown", (e) => { if ((e as KeyboardEvent).key === "Escape") close(); });
    return frame;
  }

  async function open(a: Answer | null, q: string) {
    const f = api.file();
    if (!a || !f) return;
    const frame = shell("Measurement report");
    // the file the evidence is in: the open one when it has located items, else the file of the first located item (an
    // answer that followed a reference into another file of the upload, F8 xref)
    const located = (a.evidence ?? []).filter((e) => e.bbox && e.bbox.length === 4);
    const path = located.some((e) => !e.file || e.file === f.path) ? f.path : (located[0]?.file ?? f.path);
    const ev = (a.evidence ?? []).filter((e) => (!e.file && path === f.path) || e.file === path);
    // the page the evidence is on (the open page when it has some, else the first located item's)
    const cur = path === f.path ? api.page() : -1;
    const page = ev.some((e) => (e.page ?? 0) === cur && e.bbox) ? cur : (ev.find((e) => e.bbox)?.page ?? Math.max(cur, 0)) ?? 0;
    const has = ev.some((e) => e.bbox && (e.page ?? 0) === page);
    // the other place the answer's items are (another page / file of the set): the one with the most located items
    const groups = new Map<string, { file: string; page: number; ev: Evidence[] }>();
    for (const e of located) {
      const g = { file: e.file || f.path, page: e.page ?? 0 };
      if (g.file === path && g.page === page) continue;
      const k = `${g.file}|${g.page}`;
      (groups.get(k) ?? groups.set(k, { ...g, ev: [] }).get(k)!).ev.push(e);
    }
    const other = has ? [...groups.values()].sort((x, y) => y.ev.length - x.ev.length)[0] : undefined;
    const [metaR, crop, sheet, moreMeta, moreCrop] = await Promise.all([
      fetch(`/api/feat/report/meta?path=${encodeURIComponent(path)}&page=${page}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      has ? blobUrl(path, page, ev, "crop", 1600) : Promise.resolve(null),
      has ? blobUrl(path, page, ev, "sheet", 520) : Promise.resolve(null),
      other ? fetch(`/api/feat/report/meta?path=${encodeURIComponent(other.file)}&page=${other.page}`).then((r) => (r.ok ? r.json() : null)).catch(() => null) : Promise.resolve(null),
      other ? blobUrl(other.file, other.page, other.ev, "crop", 1400) : Promise.resolve(null),
    ]);
    if (!host) return; // closed while loading
    const more: More | null = other && moreCrop ? {
      meta: moreMeta ?? { file: other.file.split("/").pop() ?? other.file, page: other.page, pages: 1, sheet: null, title: null, version: "", generated_at: "" },
      crop: moreCrop, n: other.ev.length, notes: [...new Set(other.ev.map((e) => clean(e.note ?? "")).filter(Boolean))],
    } : null;
    const meta: Meta = metaR ?? { file: path.split("/").pop() ?? path, page, pages: 1, sheet: null, title: null, version: "unknown", generated_at: new Date().toLocaleString() };
    const credit = creditOf(f);
    let xc: CrossCheck | null = null;
    try {
      const c = (window as unknown as { stoaCrossCheck?: CrossCheck }).stoaCrossCheck;
      // the same question on the same sheet, and about THIS answer (its value is one of the two readings: not a stale check)
      const num = (t: string) => Number((String(t).match(/-?[\d,]*\.?\d+/) ?? ["NaN"])[0].replace(/,/g, ""));
      const v = Number(a.value);
      const same = (t: string) => Number.isFinite(v) ? Math.abs(num(t) - v) <= 0.005 * Math.max(Math.abs(v), 1) : String(t).startsWith(String(a.value));
      if (c && c.q.trim() === q.trim() && c.file === f.path && (same(c.here) || same(c.there))) xc = c;
    } catch { /* xref off */ }
    frame.srcdoc = reportHtml(a, q, meta, crop, sheet, credit, xc, more);
  }

  // the takeoff report: only when F2's takeoff of this sheet page is finished (GET /feat/report/takeoff never starts one)
  let tk: { key: string; data: Tk | null } = { key: "", data: null };
  const tkKey = () => `${api.file()?.path ?? ""}|${api.page()}`;
  const tkSync = () => {
    const b = document.querySelector<HTMLButtonElement>('.feat-btn[data-feature="report"][data-id="takeoff-report"]');
    if (b) b.hidden = !(tk.key === tkKey() && tk.data?.available);
  };
  const tkLoad = async () => {
    const f = api.file();
    const k = tkKey();
    if (!f || !/\.pdf$|\.png$|\.jpe?g$|\.ifc$/i.test(f.path)) { tk = { key: k, data: null }; tkSync(); return; }
    try {
      const r = await fetch(`/api/feat/report/takeoff?path=${encodeURIComponent(f.path)}&page=${api.page()}${visitorQ()}`);
      tk = { key: k, data: r.ok ? await r.json() : null };
    } catch { tk = { key: k, data: null }; }
    tkSync();
    try { tabSync(); } catch { /* tab not built yet */ }
  };
  async function openTakeoff() {
    const f = api.file();
    if (!f) return;
    await tkLoad();
    if (!tk.data?.available) return;
    const frame = shell("Takeoff report");
    const page = api.page();
    const ev = tk.data.rows.filter((r) => r.quantity === "area").flatMap((r) => r.evidence).filter((e) => e.polygon && e.bbox);
    const [metaR, crop] = await Promise.all([
      fetch(`/api/feat/report/meta?path=${encodeURIComponent(f.path)}&page=${page}`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      ev.length ? blobUrl(f.path, page, ev, "crop", 2000) : Promise.resolve(null),
    ]);
    if (!host) return;
    const meta: Meta = metaR ?? { file: f.path.split("/").pop() ?? f.path, page, pages: 1, sheet: null, title: null, version: "unknown", generated_at: new Date().toLocaleString() };
    let conv = "";
    try { conv = convNote((window as unknown as { stoaConventions?: ConvDetail }).stoaConventions); } catch { /* F4 off */ }
    frame.srcdoc = takeoffHtml(tk.data, meta, crop, creditOf(f), conv);
  }
  // a 'Report' tab in F2's takeoff sheet (W5 22:27: Quantities · Conventions · Order list · Check list · Report): reachable
  // wherever the takeoff panel is, also on an IFC model (its 3D view hides the 2D toolbar)
  let tabEl: HTMLElement | null = null;
  const tabSync = () => {
    if (!tabEl) return;
    const ok = tk.key === tkKey() && !!tk.data?.available;
    const b = tabEl.querySelector<HTMLButtonElement>('[data-r="tk"]');
    const st = tabEl.querySelector<HTMLElement>('[data-r="st"]');
    if (b) b.disabled = !ok;
    if (st) st.textContent = ok ? `${tk.data!.rows.filter((r) => r.n != null && r.quantity === "area").length} rooms ready to print.`
      : "The takeoff of this sheet is not finished yet: run it in Quantities first, then print it here.";
  };
  let tabbed = false;
  try {
    tabbed = typeof api.addTakeoffTab === "function";
    api.addTakeoffTab?.({ id: "takeoff-report-tab", label: "Report", title: "Print the takeoff as a signed-off record", order: 40,
      when: () => tk.key === tkKey() && !!tk.data?.available,
      render: (el) => {
        tabEl = el;
        el.innerHTML = `<div style="font:13px/1.4 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;padding:4px 2px">
          <p style="margin:0">Print this takeoff as a signed-off record: every room outlined and numbered on the plan (on a 3D model: named by its GlobalId), rooms coloured by type (the takeoff's own colours), subtotals by room type and by room name, a room-by-room table, the totals (rooms in review left out, as in Quantities), the scale, the convention and a Checked-by block.</p>
          <button type="button" class="feat-row" data-r="tk" style="align-self:flex-start;padding:6px 14px;display:inline-flex;align-items:center;gap:6px"><span style="display:inline-flex;width:16px;height:16px">${PRINT_ICON}</span>Print the takeoff report</button>
          <p data-r="st" style="margin:0;color:#57606a;font-size:12px"></p></div>`;
        el.querySelector('[data-r="tk"]')!.addEventListener("click", () => void openTakeoff());
        tabSync();
        void tkLoad().then(tabSync);
      },
      onClose: () => { tabEl = null; } });
  } catch { /* older registry: the toolbar button below is the entry */ }
  // UX 23:35 (MINOR 12): takeoff had three entry points; with the takeoff sheet's 'Report' tab the separate toolbar entry
  // is only the fallback for an older registry
  if (!tabbed) api.addToolbarButton({ id: "takeoff-report", label: "Takeoff report", icon: PRINT_ICON, title: "Every room of the finished takeoff outlined and numbered on the plan, a room-by-room table, totals and sign-off — print or save as PDF", when: () => tk.key === tkKey() && !!tk.data?.available, onClick: () => void openTakeoff() });
  api.onDrawing(() => { tkSync(); void tkLoad(); });
  // a takeoff finishing while the sheet is open: look again after each answer, after an upload (an IFC model draws no 2D
  // sheet, so onDrawing never fires for it) and every 20 s while none is available (one small GET; the job key is memoised)
  api.onAnswer(() => void tkLoad());
  api.onUpload?.(() => { setTimeout(() => void tkLoad(), 1500); });
  setInterval(() => {
    try {
      if (document.visibilityState === "visible" && api.file() && !(tk.key === tkKey() && tk.data?.available)) void tkLoad();
    } catch { /* fail closed */ }
  }, 20000);

  api.addToolbarButton({
    id: "report",
    label: "Report",
    title: "Measurement report of this answer: drawing crop, formula, scale, convention, assumptions, sign-off — print or save as PDF",
    icon: PRINT_ICON,
    when: () => !!api.answer() && api.answer()!.value !== null,
    onClick: () => void open(api.answer(), api.question()),
  });
  // the registry re-checks a toolbar button's when() on drawing changes only: show / hide it with every answer too
  const sync = (a: Answer | null) => {
    const b = document.querySelector<HTMLButtonElement>('.feat-btn[data-feature="report"][data-id="report"]');
    if (b) b.hidden = !a || a.value === null;
  };
  api.onAnswer((a) => sync(a));
  api.addContextMenuItem({
    id: "report",
    label: "Measurement report of this answer",
    // UX 00:10 S-m5: only on one of the answer's own marks (the card's 'Report' pill is the entry everywhere else), never
    // on blank paper, inside a region box or during Count
    when: (ctx) => !!ctx.answer && ctx.answer.value !== null && onAnswerMark(ctx.answer, ctx.point, ctx.page, ctx.file?.path ?? "", ctx.space),
    run: (ctx) => void open(ctx.answer, api.question()),
    order: 80,
  });
  // the answer card's action row, when W5's registry offers one (captain 21:51: 'Report' on the answer card)
  // (W5 22:27: addCardAction = addAnswerAction, a pill under the card's time-saved line; feature-detected for older registries)
  const card = api.addAnswerAction ?? api.addCardAction;
  if (typeof card === "function") card({ id: "report", label: "Report", icon: PRINT_ICON, title: "Measurement report of this answer — print or save as PDF", when: (a) => a.value !== null && a.value !== undefined, run: (a, q) => void open(a, q), order: 40 });
  // the geometry check on the card itself (one plain line under the badges; agreeing checks only)
  api.addAnswerNote?.((a: Answer) => {
    try { return cardCheck(a, api.file()?.path ?? ""); } catch { return null; }
  });
  // Escape closes the overlay from the app too
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && host) close(); });
  // captain 01:18 'Start over' (window event 'stoa:reset'): back to how the drawing first opened — no report overlay open,
  // no Report button for an answer that is gone (the takeoff report's availability is re-checked on the next drawing)
  window.addEventListener("stoa:reset", () => {
    returnFocus = null;
    close();
    sync(null);
  });
});
