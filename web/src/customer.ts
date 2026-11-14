/**
 * Customer view (default): for site engineers, project managers and estimators, not for the jury's engineers.
 * Projects instead of files, one big question box, the answer as the hero in plain words, a short numbered list of
 * where it comes from, the time it saves, and the drawing-vs-model differences of the project. Everything technical
 * (solvers, cost in tokens, trace, benchmark) stays one click away: "How we worked it out" and the Expert view.
 */
import { agentAsks, crossChecks, fmtValue, setAnswer, unitLabel, type SetRow } from "./card";
import { dataLineHtml } from "./governance";
import { manualTime } from "./manual-time";
import { floorName, PROJECTS, projectOf, type Project } from "./projects";
import type { Answer, Kg } from "./types";

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const CIRCLED = "①②③④⑤⑥⑦⑧⑨";
export const circled = (i: number) => CIRCLED[i] ?? `(${i + 1})`;

// ---------- view mode ----------
export type View = "customer" | "expert";
export function initialView(): View {
  const q = new URLSearchParams(location.search).get("view");
  if (q === "expert" || q === "customer") return q;
  try {
    return localStorage.getItem("be-view") === "expert" ? "expert" : "customer";
  } catch {
    return "customer";
  }
}
export function saveView(v: View) {
  try {
    localStorage.setItem("be-view", v);
  } catch {
    /* private window: the URL keeps it */
  }
}

// ---------- landing: project cards ----------
/** privacyPanel: T2's privacy feature ('Delete my files') is on */
export function landingHtml(privacyPanel = false): string {
  // a real thumbnail per project: its first drawing (PDF page / image) rendered small by the dev server (/api/thumb)
  const thumb = (p: Project) => {
    const s = p.sheets.find((x) => /\.(pdf|png|jpe?g)$/i.test(x.path));
    const ifc = p.sheets.find((x) => /\.ifc$/i.test(x.path));
    // a model-only project: its 3D snapshot once someone has opened it (main.ts tileSnapshot), else the cube
    return s ? `<span class="pc-thumb"><img src="/api/thumb?path=${encodeURIComponent(s.path)}" alt="" loading="lazy" onerror="this.remove()"></span>`
      : `<span class="pc-thumb ph"><svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M32 8 54 20v24L32 56 10 44V20z"/><path d="M10 20l22 12 22-12M32 32v24"/></svg>${
        ifc ? `<img class="snap" src="/api/thumb?path=${encodeURIComponent(ifc.path)}" alt="" loading="lazy" onerror="this.remove()">` : ""}</span>`;
  };
  const card = (p: Project) => `<button type="button" class="proj-card" data-project="${esc(p.id)}">
      ${thumb(p)}
      <span class="pc-kind">${esc(KIND_LABEL[p.kind])}</span>
      <span class="pc-name">${esc(p.name)}</span>
      <span class="pc-place">${esc(p.place)}</span>
      <span class="pc-facts">${p.facts.slice(0, 2).map(esc).join(" · ")}</span>
      <span class="pc-contents">${esc(p.contents)}</span>
      <span class="pc-ask">“${esc(p.chips[0])}”</span>
    </button>`;
  return `<div class="landing-inner">
    <header class="landing-hero">
      <h1 class="lh-title">Ask your drawings.</h1>
      <p class="lh-sub">Answers that show where on the drawing they come from.</p>
    </header>
    <section class="dropzone" id="landDrop" role="button" tabindex="0" aria-label="Upload your own drawing">
      <svg class="dz-ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M12 30v8h24v-8" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M24 31V9M15 18l9-9 9 9" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div class="dz-title">Drop a drawing or a whole set</div>
      <div class="dz-sub">A floor plan or sheet as PDF, a scan or a phone photo of a print, a 3D building model or a CAD drawing <span class="dz-fmt">(PDF, JPG, PNG, HEIC, IFC, DWG, DXF, SVG)</span></div>
      <span class="dz-btn">Choose files</span>
      <div class="dz-more">Then ask in your own words, in any language: areas, lengths, counts.
        A plan dropped with its 3D model or CAD file is read together.</div>
      <div class="dz-status" id="landStatus" hidden></div>
    </section>
    <h2>Or explore a sample project.</h2>
    <div class="proj-grid">${PROJECTS.map(card).join("")}
    </div>
    ${__HAS_MOBILE__ ? `<p class="landing-phone"><a href="./mobile.html">On a phone? Open the phone page</a></p>` : ""}
    <p class="landing-foot">Your files are stored on this server only, in a temporary folder${privacyPanel ? ", until you press ‘Delete my files’ (privacy panel)" : ""}. They are read by our own readers and models running here; when an answer is unsure and Private is off, one page image, the question and the proposed answer may go to Google Gemini or NVIDIA Nemotron — each answer says where its data went. With <b>Private</b> on, nothing leaves this server: every outside model is switched off while it answers. Sample drawings and models are public (credits under each project). Answers point to where they come from on the drawing (or say when they could not), so you can check them.</p>
  </div>`;
}
const KIND_LABEL: Record<Project["kind"], string> = {
  building: "Building · drawings + 3D", model: "Building · 3D model", floor: "Floor plan", schedule: "Drawing set", plan: "Floor plan", historic: "Historic drawing",
  scan: "Floor plan · scanned print", structural: "Foundation and slab plan", piping: "Piping plan",
};

// ---------- the answer, in plain words ----------
const NOUN: Record<string, [string, string]> = {
  door: ["door", "doors"], window: ["window", "windows"], room: ["room", "rooms"], space: ["room", "rooms"],
  wall: ["wall", "walls"], column: ["column", "columns"], stair: ["staircase", "staircases"], bedroom: ["bedroom", "bedrooms"],
  bathroom: ["bathroom", "bathrooms"], kitchen: ["kitchen", "kitchens"], sauna: ["sauna", "saunas"], apartment: ["apartment", "apartments"],
  unit: ["apartment", "apartments"], storey: ["floor", "floors"], toilet: ["toilet", "toilets"], wc: ["toilet", "toilets"],
  sink: ["sink", "sinks"], slab: ["slab", "slabs"], beam: ["beam", "beams"], railing: ["railing", "railings"],
};

export interface HeroCtx {
  question: string;
  file: string;
  roundTripMs: number;
  /** the sheet label of a path in the current project ("Floor 2"), or "" */
  sheetLabel: (path: string) => string;
  /** many elements on several floors (a whole-building answer): one numbered line per floor instead of per element */
  groups?: FloorGroup[];
  /** a human label for evidence item i ("door between Koridor 119 and Trepikoda 101"), or undefined */
  labelOf?: (i: number) => string | undefined;
  /** a ranking's evidence indices, 1st first (rankOf): the list's badges follow the colour ramp */
  rank?: number[] | null;
  /** the drawing's scale is known (printed, from the CAD file, or set by the visitor), not only guessed */
  scaleKnown?: boolean;
  /** the whole sheet at its known scale (area m², diagonal m): an answer larger than that cannot be right */
  sheet?: { m2: number; diag: number };
}

export interface FloorGroup {
  storey: number;
  /** "Floor 1", "Basement" */
  label: string;
  guids: string[];
}

/** "windows" for a count of the intent's target; "elements" when the answer names none. */
export function targetNoun(target: string | undefined, n: number): string {
  const t = (target ?? "").toLowerCase().replace(/^ifc/, "").replace(/_/g, " ").trim();
  const base = !t ? "element" : t === "space" ? "room" : t === "storey" ? "floor" : t;
  if (n === 1) return base;
  return /(s|x|ch|sh)$/.test(base) ? `${base}es` : /[^aeiou]y$/.test(base) ? `${base.slice(0, -1)}ies` : `${base}s`;
}

export function confidenceWords(c: number): { cls: string; word: string; hint: string } {
  if (c >= 0.9) return { cls: "sure", word: "Very sure", hint: "" };
  if (c >= 0.7) return { cls: "fair", word: "Fairly sure", hint: "check the marked spots" };
  // a typical value, not something read off this drawing (the training prior, a guess without evidence)
  if (c < 0.25) return { cls: "est guess", word: "Rough guess", hint: "not found on the drawing" };
  // Ziying (H 16:28): 'below a threshold it would be more honest to say: can't tell, please measure' — a 0.30 area shown
  // as a plain number reads as a measurement. Under 0.4 the number is our guess and checking it is the main action.
  if (c < LOW_CONF) return { cls: "est guess", word: "Our guess", hint: "we are unsure: please check it" };
  return { cls: "est", word: "Best estimate", hint: "" };
}

/** Below this confidence an answer is shown as a guess ('≈ 16 m²', 'Our guess'), with measuring / checking it as the
 *  main action (Ziying 16:26; the expert workbench uses the same 0.4). */
export const LOW_CONF = 0.4;
/** a guess, shown as such: low confidence and no second method that agrees */
export const isGuess = (a: Answer, agreeing = false) => (typeof a.value === "number" || (typeof a.value === "string" && /^\s*[\d][\d,.]*\s*[\p{L}²'"]{0,6}\s*$/u.test(a.value)))
  && (a.confidence ?? 0) < LOW_CONF && !agreeing;

/** 'how many linear metres / square feet / LF …': a length or an area asked as 'how many' */
export const HOW_MANY_UNITS = /^\s*how many\s+(?:linear|running|lineal|square|sq\.?|cubic)?\s*(?:metres|meters|m|m2|m²|feet|foot|ft|sf|lf|yards?|millimetres|millimeters|mm|cm)\b/i;
const SIZE_Q = /\b(how (big|large|wide|long|tall|high|deep|far|much (space|floor))|area|size|width|length|distance|perimeter|dimensions?|square (metres|meters|feet)|fläche|breite|länge|grösse|größe|pinta-ala|leveys|pituus|koko|superficie|surface|largeur|longueur)\b|m²|\bm2\b|\bsq ?m\b/i;

/** An area or length question with nothing measured on this drawing: the answer is the dataset's typical value (the
 *  prior: 'How big is the lounge?' → 55.72 m² @0.01, 'How long is the garage?' → 244.8 m @0.00 on a plan with labels
 *  only and no scale, X1's wild exam 14:22) or a 'Not determined …' / unrelated stock sentence. A site engineer
 *  reads such a number as a wrong answer, so the customer view shows no number and offers the measure tool instead;
 *  the guess itself stays under "How we worked it out". Counts keep their rough guess. */
/** The answer cannot be a measurement of what was asked (an area / length question):
 *  'echo' = a bare number copied from the question ('What is the area of the 300 SLAB-ON-GRADE …?' → '300', no unit);
 *  'too big' = more than the whole sheet covers at its known scale (S-100F 'total slab area' → 108,000 m² @0.03 on an
 *  A1 sheet at 1:50 ≈ 1,250 m²), or a length longer than the sheet's diagonal. sheet = {m2, diag} at the known scale. */
/** The printed text an area / length answer was read from when that text is a LEVEL (elevation), not a size: QA's
 *  audit #1 (19:51 → 20:32): S-100F 'TOP OF SLAB EL. = 10000' → '10,000 m² Fairly sure'. '' when not. */
export function levelRead(a: Answer): string {
  const v = typeof a.value === "number" ? a.value : typeof a.value === "string" && /^\s*[\d.,]+\s*$/.test(a.value) ? Number(a.value.replace(/,/g, "")) : NaN;
  if (!Number.isFinite(v)) return "";
  const lit = String(Number.isInteger(v) ? v : v).replace(/\./g, "\\.");
  const re = new RegExp(String.raw`\b(?:EL\.?|ELEV\.?|ELEVATION|T\.?\s?O\.?\s?[SFWC]\.?|TOP OF\b[A-Z .]{0,24}?|BOTTOM OF\b[A-Z .]{0,24}?|U\.?\s?S\.?\s?[SF]\.?|FFL|SFL|SSL|INV(?:ERT)?|LEVEL|LVL|NIVEAU|KOTE)\s*[:=]?\s*[+±-]?\s*${lit}(?:\.0+)?(?![\d.,]*\d)(?!\s*(?:m2|m²|sq|sf|ft2|ft²|m\b|mm|cm|'|"|″))`, "i");
  for (const e of a.evidence ?? []) {
    const note = String(e.note ?? "").replace(/(\d)[,\s'](?=\d{3}\b)/g, "$1");
    const q = note.match(/'([^']{2,80})'/)?.[1] ?? note;
    if (re.test(q)) return q.trim();
  }
  return "";
}

export function implausible(a: Answer, question: string, sheet?: { m2: number; diag: number }): "" | "echo" | "too big" | "level" {
  const v = typeof a.value === "number" ? a.value : typeof a.value === "string" && /^\s*[\d.,]+\s*$/.test(a.value) ? Number(a.value.replace(/,/g, "")) : NaN;
  if (!Number.isFinite(v)) return "";
  const u = String(a.unit ?? "").trim();
  const raw = String(a.value).trim().replace(/\.0+$/, "");
  if (!u && new RegExp(`(?<![\\d.,])${raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\d.,]*\\d)`).test(question)) return "echo";
  // a level / elevation printed on the sheet is never an area or a length, however sure the reader was
  if (/^(m2|m²|sq ?m|ft2|ft²|sf|sq ?ft|m|mm|ft|feet)$/i.test(u) && levelRead(a)) return "level";
  // a whole-building / several-floor question can exceed one sheet; so can a confident answer from a model or schedule
  if (!sheet || !(sheet.m2 > 0) || (a.confidence ?? 0) >= 0.5 || a.intent?.scope === "model"
    || /\b(building|buildings|floors|storeys|levels|model|site|project|gebäude|rakennu\w*)\b/i.test(question)
    || (a.evidence ?? []).some((e) => e.ifc_guid)) return "";
  const m2 = /^(m2|m²|sq ?m)$/i.test(u) ? v : /^(ft2|ft²|sf|sq ?ft)$/i.test(u) ? v * 0.09290304 : NaN;
  const m = /^m$/i.test(u) ? v : /^mm$/i.test(u) ? v / 1000 : /^(ft|feet)$/i.test(u) ? v * 0.3048 : NaN;
  if (m2 > 1.05 * sheet.m2) return "too big";
  if (m > 1.05 * sheet.diag && !/\b(skirting|base ?boards?|total|all|sum|whole)\b/i.test(question)) return "too big";
  return "";
}

/** Two readings that disagree (captain 18:56: show them as a calm choice). From the hybrid step's own words:
 *  'ours' = Gemini's reading is the answer and one of OUR readers had another number ('our reader answered 6 count @0.60
 *  (pipeline/label_count)': QA 18:56, A-101F doors 5 vs 6 = gold, DfE skirting); 'gemini' = our reading is kept and
 *  Gemini estimated another number ('Gemini 3.1 Pro estimated 61.69 m2 (a visual estimate, not measured)': S-100F 150
 *  slab 30.85 vs 61.69 = gold). null when they agree within 2 % or nothing says so. */
const READER_WORDS: Record<string, string> = {
  label_count: "printed-tag reader", area_tags: "printed-tag reader", plan_rooms: "room reader", line_rooms: "wall-line tracer",
  lineplan: "wall-line tracer", raster: "image reader", vlm_lora: "local vision model", vlm_direct: "vision model", pdf_vector: "CAD reader",
  pdf_text: "text reader", room_graph: "room-graph reader", raster_cc5k: "room detector", door_detect: "door detector", openings: "door-swing reader",
  slab_area: "slab reader", dxf: "CAD reader", ifc: "3D-model reader", schedule: "schedule reader",
};
export type Rival = { kind: "ours" | "gemini" | "second"; value: number; unit: string; who: string; solver?: string; quote?: string; also?: string };
export function rivalReading(a: Answer): Rival | null {
  if (typeof a.value !== "number" || !Number.isFinite(a.value)) return null;
  const miss = (a.missing ?? []).map(String);
  const close = (v: number) => Math.abs(v - (a.value as number)) <= 0.02 * Math.max(Math.abs(v), Math.abs(a.value as number), 1e-9);
  if (/gemini/i.test(String(a.solver ?? ""))) {
    for (const l of miss) {
      const m = l.match(/\bour reader answered\s+([\d.,]+)\s*([^\s@]*)\s*@\s*([\d.]+)\s*\(([\w/.-]+)\)/i);
      if (!m) continue;
      const v = Number(m[1].replace(/,/g, ""));
      const solver = m[4].split("/").pop() ?? "";
      if (!Number.isFinite(v) || close(v) || !solver || /prior|dummy|gemini/i.test(solver)) continue;
      return { kind: "ours", value: v, unit: m[2] || String(a.unit ?? ""), who: READER_WORDS[solver] ?? "own reader", solver };
    }
    return null;
  }
  for (const l of miss) {
    const m = l.match(/\bGemini [\d.]+ \w+ estimated\s+([\d.,]+)\s*(\S*)/i);
    if (!m) continue;
    const v = Number(m[1].replace(/,/g, ""));
    if (Number.isFinite(v) && !close(v)) return { kind: "gemini", value: v, unit: m[2] || String(a.unit ?? ""), who: "Gemini 3.1 Pro" };
  }
  return secondReading(a);
}

/** G1's 'clarify' (captain 18:50 / 18:56; W5 19:10 shape): Answer.clarify = {ask, why, options: [{label, value, unit,
 *  evidence: [indices into Answer.evidence], question}], click}, or until the contract has the field, its trace line
 *  'clarify: <ask> (<label>: <value> <unit> | <label>: <value> <unit>)'. null when there are not two options. */
export type ClarifyOpt = { label: string; value: number | string | null; unit: string; evidence: number[]; question?: string };
export type Clarify = { ask: string; why: string; options: ClarifyOpt[]; click?: string };
const inRange = (i: number, n: number) => Number.isInteger(i) && i >= 0 && i < n;
export function clarifyOf(a: Answer): Clarify | null {
  const c = (a as unknown as { clarify?: Record<string, unknown> }).clarify;
  if (c && Array.isArray(c.options) && c.options.length >= 2) {
    const n = (a.evidence ?? []).length;
    const options = (c.options as Record<string, unknown>[]).map((o) => ({
      label: String(o.label ?? ""), value: (o.value ?? null) as number | string | null, unit: String(o.unit ?? ""),
      evidence: Array.isArray(o.evidence) ? (o.evidence as unknown[]).map(Number).filter((i) => Number.isInteger(i) && i >= 0 && i < n) : [],
      ...(o.question ? { question: String(o.question) } : {}),
    }));
    return { ask: String(c.ask ?? "Which one do you mean?"), why: String(c.why ?? ""), options, ...(c.click ? { click: String(c.click) } : {}) };
  }
  const line = (a.trace ?? []).map(String).find((l) => /^clarify: /.test(l));
  if (!line) return null;
  // the options are the LAST '( … )' whose content is 'label: number unit | …' (the ask may hold parentheses too)
  const body = line.slice("clarify: ".length);
  for (let i = body.indexOf(" ("); i >= 0; i = body.indexOf(" (", i + 1)) {
    const inner = body.slice(i + 2).replace(/\)\s*$/, "");
    // G1 20:56: an option may end with its evidence indices '[evidence 0,1]'
    const n = (a.evidence ?? []).length;
    const options = inner.split(" | ").map((p) => p.match(/^(.*?):\s*(-?[\d.,]+)\s*(.*?)\s*(?:\[evidence\s*([\d,\s]*)\])?\s*$/)).filter((m): m is RegExpMatchArray => !!m)
      .map((m) => ({ label: m[1].trim(), value: Number(m[2].replace(/,/g, "")), unit: m[3].trim(),
        evidence: (m[4] ?? "").split(",").filter((x) => x.trim() !== "").map((x) => Number(x.trim())).filter((i) => inRange(i, n)) }));
    if (options.length >= 2 && options.length === inner.split(" | ").length && options.every((o) => Number.isFinite(o.value)))
      return { ask: body.slice(0, i).trim(), why: "", options };
  }
  return null;
}

/** A skirting answer that is the room's WHOLE outline because the question said not to deduct the door openings (E1's
 *  assumption 'door openings NOT deducted as asked', or the question's own words), and the value is that outline. */
const NO_DEDUCT_Q = /\b(without|not|don'?t|do not|no)\s+(deduct|subtract|minus|remov)\w*|\b(including|incl\.?)\s+(the\s+)?(doors?|openings?)\b|\bignor\w*\s+(the\s+)?(doors?|openings?)/i;
export function wholeOutline(a: Answer, q: string, outline?: number): boolean {
  const said = (a.assumptions ?? []).some((x) => /openings?\s+NOT\s+deducted/i.test(String(x))) || NO_DEDUCT_Q.test(q);
  if (!said || typeof a.value !== "number") return false;
  return outline === undefined || Math.abs(a.value - outline) <= 0.005 * Math.max(outline, 1e-9);
}

/** T1 20:25 (audit fix #2 'evidence states'): Answer.support {state, why, shown, of} or the trace line 'support: <state>
 *  — <why>' (state supported | approximate | unlocated) → one plain line under the badges. */
export function supportOf(a: Answer): { state: "supported" | "approximate" | "unlocated"; why: string } | null {
  const f = (a as unknown as { support?: { state?: string; why?: string } }).support;
  let state = String(f?.state ?? "").toLowerCase(), why = String(f?.why ?? "");
  if (!state) {
    const l = [...(a.trace ?? [])].map(String).reverse().find((x) => /^support:\s*/i.test(x));
    const m = l?.match(/^support:\s*(supported|approximate|unlocated)\b\s*(?:[—–-]+\s*(.*))?$/i);
    if (!m) return null;
    state = m[1].toLowerCase();
    why = (m[2] ?? "").trim();
  }
  return state === "supported" || state === "approximate" || state === "unlocated" ? { state, why } : null;
}
export function supportHtml(a: Answer, esc: (s: string) => string): string {
  const s = supportOf(a);
  if (!s) return "";
  const why = s.why.replace(/\s+/g, " ").trim().slice(0, 200);
  const text = s.state === "supported" ? "✓ The marked text / outline gives this number"
    : s.state === "approximate" ? `≈ Approximate${why ? `: ${why}` : ""}` : `Not located on the drawing${why ? `: ${why}` : ""}`;
  return `<div class="hero-support ${s.state}"${why ? ` title="${esc(why)}"` : ""}>${esc(text)}</div>`;
}

/** Area units as one spelling ('SF' / 'sq ft' / 'ft²' → ft2, 'sqm' / 'm²' → m2); '' when it is no area unit. */
const areaUnit = (u: string) => (/^(sf|sq\.?\s*ft|sqft|ft²|ft2|square feet)$/i.test(u.trim()) ? "ft2" : /^(m²|m2|sqm|sq\.?\s*m|square met(re|er)s?)$/i.test(u.trim()) ? "m2" : "");
const toM2 = (v: number, u: string) => (u === "ft2" ? v * 0.09290304 : v);

/** QA 19:33 (AFCEC PNG 'dormitory area of the north wing in square feet?'): the agent read '6285 m2' and converted it to
 *  67,651 ft²; our image reader had read 6,285 SF and the NVIDIA second opinion said 'The drawing states NORTH WING
 *  DORMITORY = 6,285 SF' — only the confidence went down. The second opinion's own reading (stoa/agent/verify.py:
 *  '… looked at the evidence and disagrees (it reads 6,285 SF): <reason>') becomes a choice, but only when something
 *  else backs it: its reason quotes that number printed on the drawing, one of our readers read the same number, or a
 *  marked spot prints it. `quote` = the printed line it quotes; `also` = the reader that read the same. */
function secondReading(a: Answer): Rival | null {
  const v0 = a.value as number, u0 = String(a.unit ?? "");
  for (const l of (a.missing ?? []).map(String)) {
    const m = l.match(/^(.*?) looked at the evidence and disagrees \(it reads ([^)]*)\):\s*(.*)$/i);
    if (!m) continue;
    const seen = m[2].match(/(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z²]+(?:\s?ft)?)?/);
    if (!seen) continue;
    const v = Number(seen[1].replace(/,/g, ""));
    if (!Number.isFinite(v)) continue;
    // the same kind of unit, or none said (a count); an area in another unit is compared in m²
    const au0 = areaUnit(u0), au = areaUnit(seen[2] ?? "");
    let unit = u0;
    if (au0 && au) unit = au;
    else if (au0 || au) { if (au0 && !seen[2]) unit = u0; else continue; }
    else if (seen[2] && !/^count$/i.test(u0) && seen[2].toLowerCase() !== u0.toLowerCase()) continue;
    const vm = au0 ? toM2(v, areaUnit(unit) || au0) : v;
    const same = (x: number, xu: string) => Math.abs((au0 ? toM2(x, areaUnit(xu) || au0) : x) - vm) <= 0.02 * Math.max(Math.abs(vm), 1e-9);
    if (same(v0, u0)) continue;
    const num = seen[1];
    const reason = m[3];
    // the printed line it quotes: 'NORTH WING DORMITORY = 6,285 SF'
    const q = reason.match(new RegExp(String.raw`([A-Z][A-Z0-9 .&/'-]{1,60}?\s*[=:]\s*)?(${num.replace(/[.,]/g, "[.,]?")})\s*(SF|sq\.?\s*ft|ft²|ft2|m²|m2|sqm)?(?![\d\w])`));
    const quoted = !!q && /\b(states?|prints?|printed|labell?ed|reads|shows|written|tag|says)\b/i.test(reason);
    const quote = quoted && q ? q[0].replace(/\s+/g, " ").trim() : "";
    // one of our readers read the same number ('raster (question as asked): 6285.0 SF at conf 0.15'; 'agent and raster disagree (67651.111 vs 6285.0)')
    let also = "";
    for (const t of [...(a.trace ?? []).map(String), ...(a.missing ?? []).map(String)]) {
      const r = t.match(/^(\w+)(?: \([^)]*\))?: ([\d.,]+)\s*([A-Za-z²]*) at conf/) ?? t.match(/^\w+ and (\w+) disagree \([\d.,]+ vs ([\d.,]+)\)/);
      if (r && same(Number(r[2].replace(/,/g, "")), r[3] ?? "")) { also = READER_WORDS[r[1]] ?? ""; if (also) break; }
    }
    const marked = (a.evidence ?? []).some((e) => new RegExp(String.raw`'${num.replace(/[.,]/g, "[.,]?")}' printed`).test(e.note ?? ""));
    if (!quote && !also && !marked) continue;
    return { kind: "second", value: v, unit, who: m[1].replace(/^NVIDIA\s+/i, "") || "second model", quote, also };
  }
  return null;
}

/** The second opinion's reading as the answer, because the visitor chose it (rivalChoice): same marks (the spot that
 *  prints the number first), the first answer kept in missing[], a trace line that says it was the visitor's choice
 *  (with read: '<quote>' so printedReading() words it 'as printed on the drawing'). Confidence: 0.7 when two things
 *  back it (quote / one of our readers / a marked spot), else 0.55 — the visitor's choice, not a new measurement. */
export function secondChoiceAnswer(a: Answer, r: Rival): Answer {
  const num = r.value.toLocaleString("en", { maximumFractionDigits: 2 });
  const hit = (e: { note?: string | null }) => new RegExp(String.raw`'${num.replace(/[.,]/g, "[.,]?")}' printed`).test(e.note ?? "");
  const ev = [...(a.evidence ?? []).filter(hit), ...(a.evidence ?? []).filter((e) => !hit(e))];
  const backs = [!!r.quote, !!r.also, (a.evidence ?? []).some(hit)].filter(Boolean).length;
  const first = `${fmtValue(a)}${a.unit && a.unit !== "count" ? ` ${a.unit}` : ""}`;
  const isV = (n: string) => Math.abs(Number(n.replace(/,/g, "")) - r.value) <= 0.02 * Math.abs(r.value);
  // the first answer's own lines describe it, not the chosen value: the second opinion now agrees with what is shown,
  // and a cross-check that set our reader against the agent becomes 'raster 6285 = the chosen reading'
  const trace = (a.trace ?? []).map(String).map((l) => {
    const so = l.match(/^(Second opinion \([^)]*\)): disagrees \(it reads ([^)]*)\) because (.*?)(?:\s*→ confidence.*)?$/);
    if (so) return `${so[1]}: agrees with the value shown (it reads ${so[2]}; it disagreed with our first answer ${first}) because ${so[3]}`;
    const cc = l.match(/^cross-?check:\s*(\w+) ([\d.,]+)\s*(?:≠|!=|vs\.?)\s*(\w+) ([\d.,]+)/i);
    if (cc) {
      const [keep, drop] = isV(cc[4]) ? [[cc[3], cc[4]], [cc[1], cc[2]]] : isV(cc[2]) ? [[cc[1], cc[2]], [cc[3], cc[4]]] : [null, null];
      if (keep && drop) return `cross-check: ${keep[0]} ${keep[1]} = the chosen reading ✓ (the ${drop[0]}'s ${drop[1]} is not used)`;
    }
    return l;
  });
  return {
    ...a,
    value: r.value,
    unit: r.unit,
    confidence: backs >= 2 ? 0.7 : 0.55,
    evidence: ev,
    solver: `${a.solver ?? "pipeline"}+second_opinion`,
    trace: [`you chose the second opinion's reading (${r.who}): ${num}${r.unit && r.unit !== "count" ? ` ${r.unit}` : ""} over our first answer ${first}${r.quote ? `; read: '${r.quote}'` : ""}${r.also ? `; our ${r.also} read the same` : ""}`, ...trace],
    assumptions: [`your choice: the second opinion's reading${r.quote ? ` of the printed line '${r.quote}'` : ""}${r.also ? `, which our ${r.also} read too` : ""}`,
      ...(a.assumptions ?? []).filter((x) => !/^value computed in code/i.test(String(x)))],
    missing: [`our first answer was ${first} (${a.solver ?? "pipeline"}); check the marked spot`],
  } as Answer;
}

/** A whole-building count summed over a set's floor-plan sheets (P1's upload solver: 'upload: whole-house count on a
 *  9-page set: counted on each of its 3 floor-plan sheets and summed: 144 on the FIRST plan (page 2) + … = 432') as
 *  rows for a per-floor table (captain 18:50). `same`: every sheet gave the same number from a printed total — likely
 *  the building's own total repeated on each sheet (DAF dorm: 'TOTAL 144 DORMS' on every plan -> 432). */
export function perSheet(a: Answer): { rows: { storey: string; page: number; value: number }[]; total: number; same: boolean } | null {
  const line = (a.trace ?? []).map(String).find((l) => /counted on each of its \d+ floor-plan sheets and summed:/i.test(l));
  if (!line) return null;
  const rows = [...line.matchAll(/([\d.,]+) on the (.+?) plan \(page (\d+)\)/g)].map((m) => ({ value: Number(m[1].replace(/,/g, "")), storey: m[2], page: Number(m[3]) - 1 }));
  if (rows.length < 2 || rows.some((r) => !Number.isFinite(r.value))) return null;
  const total = Number(line.match(/=\s*([\d.,]+)\s*$/)?.[1]?.replace(/,/g, "") ?? NaN);
  const printedTotal = (a.trace ?? []).some((l) => /printed total|total row/i.test(String(l)));
  return { rows, total: Number.isFinite(total) ? total : rows.reduce((t, r) => t + r.value, 0), same: printedTotal && rows.every((r) => r.value === rows[0].value) };
}

/** A printed area the answer READ off the sheet (QA 18:56: AFCEC 'TOTAL COMMON SPACE = 4,479 SF', Gemini's reading boxed on
 *  the printed line, was shown as 'not measured yet · Scale unknown — set it'; a printed area needs no scale). The quoted
 *  text ('TOTAL COMMON SPACE = 4,479 SF') when a note or trace line quotes a number + area unit that IS the answer
 *  (same number, or square feet converted to the answer's m²), else ''. */
export function printedReading(a: Answer): string {
  if (typeof a.value !== "number" || !Number.isFinite(a.value)) return "";
  const v = a.value;
  const U = String.raw`(SF|sq\.?\s*ft|ft²|ft2|m²|m2|sqm)`;
  const re = [new RegExp(String.raw`\bread:\s*'?([^'\n|]{0,80}?(\d[\d,]*(?:\.\d+)?)\s*${U})(?![\w²])`, "i"),
    new RegExp(String.raw`\bRead '([^'\n]{0,80}?(\d[\d,]*(?:\.\d+)?)\s*${U})'`, "i"),
    // W6 19:57: E1's area_table notes have no read: '…' — 'TOTAL COMMON SPACE = 4,479 SF (printed area table)'
    new RegExp(String.raw`^\s*(?!cross-?check)([^'\n|():;]{0,80}?(\d[\d,]*(?:\.\d+)?)\s*${U})(?![\w²])\s*\(printed\b`, "i")];
  const m2 = /^(m2|m²|sq ?m)$/i.test(String(a.unit ?? ""));
  for (const t of [...(a.evidence ?? []).map((e) => e.note ?? ""), ...(a.trace ?? []).map(String)]) {
    for (const r of re) {
      const m = t.match(r);
      if (!m) continue;
      const n = Number(m[2].replace(/,/g, ""));
      if (!(n > 0)) continue;
      const asM2 = /^(sf|sq|ft)/i.test(m[3]) ? n * 0.09290304 : n;
      if (Math.abs(v - n) <= 0.005 * n || (m2 && Math.abs(v - asM2) <= 0.01 * asM2)) return m[1].replace(/\s+/g, " ").trim();
    }
  }
  return "";
}

export function needsMeasuring(a: Answer, question: string, file = "", sheet?: { m2: number; diag: number }): boolean {
  const task = a.intent?.task;
  if (a.value === null || a.value === undefined || typeof a.value === "boolean") return false;
  // 'How many linear metres of skirting …' / 'how many square feet …' ask for a quantity, not a count (the router said
  // count for these until night-G1 8461ff7; the card then showed '≈ 6 m … on this drawing' with no way to measure)
  const qty = HOW_MANY_UNITS.test(question);
  if (!qty && (task === "count" || a.unit === "count" || /^\s*how many\b/i.test(question))) return false;
  if (!(qty || task === "area" || task === "length" || SIZE_Q.test(question))) return false;
  const bad = implausible(a, question, sheet);
  if (bad === "level" || (bad && (a.confidence ?? 0) < 0.9)) return true;
  if (printedReading(a)) return false; // read off the print (a table total, a room tag): no scale, nothing to measure
  const measured = (a.evidence ?? []).some((e) => e.bbox || e.polygon || e.polyline || e.ifc_guid);
  // a sentence with no number ('Not specified', 'The floor plan does not provide dimensions …': the image reader P1 runs
  // on a vector sheet no PDF reader could read, night 5fc4653) is no answer to 'how big': measure it on the drawing
  const wordsOnly = typeof a.value === "string" && !/\d/.test(a.value) && !measured;
  // a number read off a picture of the sheet with no place marked and low confidence (the image reader's '10.0 m' @0.3
  // for a 7.73 m room): on a drawing, measuring the named room is checkable; the reader's number stays visible
  const unmarked = typeof a.value === "number" && !measured && (a.confidence ?? 0) < 0.5 && /\.(pdf|png|jpe?g)$/i.test(file);
  // '0 m²' is never the size of a room (room_graph on a rendered CAD sheet: the label found, the area 0 @0.3)
  const zero = typeof a.value === "number" && a.value <= 0 && (a.confidence ?? 0) < 0.9;
  // G1 16:00: 'pipeline/gemini_estimate' (≤ 0.2) = nothing of ours could measure it, Gemini's visual estimate with
  // Gemini's boxes: not a measurement, although it carries boxes ('pipeline/gemini_locate' IS measured: our tracer)
  const estimate = /gemini_estimate$/.test(String(a.solver ?? ""));
  return /(^|\/)prior$/.test(String(a.solver ?? "")) || ((a.confidence ?? 0) < 0.05 && !measured) || wordsOnly || unmarked || zero || estimate;
}

/** The answer's own number when the drawing was measured instead (needsMeasuring): a typical value (the prior) or a
 *  reader's unmarked guess. */
export const readerGuess = (a: Answer) => /(^|\/)prior$/.test(String(a.solver ?? "")) ? "typical" : "unmarked";

const Q_STOP = new Set(("how big large wide long tall high deep far much is are was the a an of in on at to for what whats what's which " +
  "area size width length floor total dimensions dimension room rooms space this that drawing plan house building " +
  "square metres meters feet m2 sqm please tell me do does it its there".split(" ")));

/** The room the question names, as compact phrases for a label search ('How big is the lounge?' → ['lounge'];
 *  'What's the area of bedroom 2?' → ['bedroom2', 'bedroom']): word runs without question words, longest first. */
export function roomPhrases(question: string): string[] {
  const words = question.toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "").split(/[^\p{L}\p{N}']+/u)
    .map((w) => w.replace(/'s$|'/g, "")).filter(Boolean);
  const runs: string[][] = [];
  let cur: string[] = [];
  for (const w of words) {
    if (Q_STOP.has(w)) {
      if (cur.length) runs.push(cur);
      cur = [];
    } else cur.push(w);
  }
  if (cur.length) runs.push(cur);
  const out = new Set<string>();
  for (const r of runs) {
    for (let n = r.length; n >= 1; n--) for (let i = 0; i + n <= r.length; i++) {
      const ph = r.slice(i, i + n).join("");
      if (ph.length >= 3 && !/^\d+$/.test(ph)) out.add(ph);
    }
  }
  return [...out].sort((a, b) => b.length - a.length);
}

/** Where the answer applies, in words: "on floor 2", "in the whole building", "on this floor". */
function wherePhrase(a: Answer, ctx: HeroCtx): string {
  // F1's region ask: the visitor drew a box / outline on the sheet ('region/labels', 'region/area_tags', 'region/outline')
  if (/^region\//.test(String(a.solver ?? ""))) return /\/outline$/.test(String(a.solver)) ? "inside your outline" : "in the area you drew";
  const st = a.intent?.filters?.storey;
  if (st) return `on ${floorName(String(st))}`;
  if (/\b(whole|entire) building\b/i.test(ctx.question) || (a.intent?.scope === "model" && !/\bnot drawn\b/i.test(ctx.question))) return "in the whole building";
  const label = ctx.sheetLabel(ctx.file);
  if (/\.ifc$/i.test(ctx.file)) return "in the 3D model";
  if (/^floor \d|floor$/i.test(label)) return `on ${label.toLowerCase().replace(/^(\d)/, "the $1")}`;
  return /\.(png|jpe?g|pdf)$/i.test(ctx.file) ? "on this drawing" : "";
}

/** Big value + one plain sentence. Yes/no answers lead with Yes / No. */
/** Calm names for the customer view: model/drawing names in capitals ('FUAJEE-TREPIKODA (101)') in title case
 *  ('Fuajee-Trepikoda (101)'); short printed codes (MH, KPH, WC) and tokens with digits stay as printed. */
export const calm = (t: string) => t.replace(/(?<![\p{L}\d])\p{Lu}[\p{Lu}-]{3,}(?![\p{L}\d])/gu,
  (w) => w.toLowerCase().replace(/(^|-)(\p{L})/gu, (_m: string, x: string, y: string) => x + y.toUpperCase()));

/** A room as the Swiss CAD file names it ("2.14 'Korridor'", "2.16", "outside / unlabelled space", "polygon #7") in
 *  words: "Korridor 2.14", "Room 2.16", "outside", "an unnamed space". */
export function roomWords(label: string): string {
  const s = label.trim().replace(/^room\s+(?=[\w.-]*\d)/i, "");
  if (/^outside\b|unlabelled/i.test(s)) return "outside";
  if (/^polygon #\d+$/i.test(s)) return "an unnamed space";
  const m = s.match(/^([\w.-]*\d[\w.-]*)?\s*(?:'([^']+)')?$/);
  if (!m || (!m[1] && !m[2])) return s;
  return m[2] ? (m[1] ? `${m[2]} ${m[1]}` : m[2]) : `Room ${m[1]}`;
}
/** Room labels inside a sentence ("door 19 connects 2.15 'Küche' and 2.14 'Korridor'") → "Küche 2.15", "Korridor 2.14". */
const swissRooms = (s: string) => s.replace(/(?<![\w.])(\d+(?:\.\d+)+)\s+'([^']+)'/g, "$2 $1");

/** The dxf solver's room-door graph answers (W2): "Which rooms does the corridor of apartment B connect to?" (trace
 *  "room connections: 2.14 'Korridor' has 7 door(s) to 7 space(s): …") and "How many doors from the entrance of
 *  apartment B to the kitchen?" (trace "route from … to …: 2 door(s): door 22 (… <-> …), …"). */
export function connectionInfo(a: Answer): { room: string; doors: number; rooms: string[]; numbers: string[] } | null {
  for (const t of a.trace ?? []) {
    const m = t.match(/room connections: (.+?) has (\d+) door\(s\) to (\d+) space\(s\):\s*(.*)$/);
    if (!m) continue;
    const raw = m[4].split(/;\s*/).filter(Boolean);
    return { room: roomWords(m[1]), doors: Number(m[2]), rooms: raw.map(roomWords),
      numbers: raw.map((r) => r.match(/^([\w.-]*\d[\w.-]*)(?:\s|$)/)?.[1] ?? "").filter(Boolean) };
  }
  return null;
}
export function routeInfo(a: Answer): { from: string; to: string; doors: string[] } | null {
  for (const t of a.trace ?? []) {
    const m = t.match(/route from (.+?) to (.+?): (\d+) door\(s\):\s*(.*)$/);
    if (!m) continue;
    const from = m[1].replace(/^the entrance door\(s\) of apartment (\w+)$/i, "the entrance of apartment $1").replace(/^room (.+)$/, (_, r: string) => roomWords(r));
    const doors = [...m[4].matchAll(/door \d+ \((.+?) <-> (.+?)\)/g)].map((d) => `${roomWords(d[1])} ↔ ${roomWords(d[2])}`);
    return { from, to: roomWords(m[2]), doors };
  }
  return null;
}
/** How many evidence items get a number: 5, or 8 for a room's doors (each door IS one of the answer's rooms). */
export const numberMax = (a: Answer) => (connectionInfo(a) ? 8 : 5);

/** Nominal pipe size (inch) → its metric DN (ISO 6708 / EN 10255 pairs). */
const DN: Record<string, number> = { "0.5": 15, "0.75": 20, "1": 25, "1.25": 32, "1.5": 40, "2": 50, "2.5": 65, "3": 80, "4": 100, "5": 125,
  "6": 150, "8": 200, "10": 250, "12": 300, "14": 350, "16": 400, "18": 450, "20": 500, "24": 600, "30": 750, "36": 900 };
const PIPE_Q = /\b(pipes?|piping|header|line|riser|main|diameter|dia|valve|DN|NPS|suction|discharge|supply|return|fuel|drain|sprinkler|duct)\b/i;

/** A size in inches (piping plans, captain 16:15 / Stoa 16:10: '12" PUMP / DISCHARGE / HEADER', '16"', 16.0 'in'): the
 *  size big, what the printed tag labels as the sentence, and the metric size under it (DN for a pipe, else mm). */
function inchSize(a: Answer, q: string): { big: string; unit: string; sentence: string; metric: string } | null {
  const v = a.value;
  let n: number | null = null;
  let rest = "";
  let quoted = "";
  if (typeof v === "number" && /^(in|inch|inches|"|″)$/i.test(String(a.unit ?? "").trim())) n = v;
  else if (typeof v === "number" && /^(ft|feet|foot|'|′)$/i.test(String(a.unit ?? "").trim())) {
    // QA 19:33 (M-101A PNG 'What pipe size feeds the truck fill stand?' → raster '6 ft' from the text '6" TO TRUCK FILL S'):
    // a size whose marked text prints the same number with an INCH mark is in inches (the reader took " for ')
    const re = new RegExp(String.raw`(^|[^\d.])${String(v).replace(".", "\\.")}\s*(?:"|″)`);
    const note = (a.evidence ?? []).map((e) => e.note ?? "").find((t) => re.test(t));
    if (!note) return null;
    n = v;
    quoted = calm(note.match(/'([^']+)'/)?.[1] ?? "").trim();
  } else if (typeof v === "string") {
    const m = v.replace(/\u2033/g, '"').match(/^\s*(?:(\d+)\s+)?(\d+(?:\.\d+)?|\d\/\d)\s*(?:"|''|-?\s*inch(?:es)?\b|in\b\.?)\s*[-–:]?\s*([\s\S]*)$/i);
    if (!m) return null;
    const frac = (x: string) => (x.includes("/") ? Number(x.split("/")[0]) / Number(x.split("/")[1]) : Number(x));
    n = (m[1] ? Number(m[1]) : 0) + frac(m[2]);
    rest = m[3].replace(/\s*\/\s*|\s*\n\s*/g, " ").replace(/\s+/g, " ").trim();
  }
  if (n === null || !Number.isFinite(n) || n <= 0 || n > 120) return null;
  const pipe = PIPE_Q.test(q) || PIPE_Q.test(rest);
  const dn = DN[String(n)];
  const big = Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, "");
  const what = rest ? `${calm(rest).toLowerCase()}, as printed on the drawing` : quoted ? `${pipe ? "nominal pipe size" : "size"}, as printed on the drawing: ‘${quoted}’`
    : pipe ? "nominal pipe size, as printed on the drawing" : "as printed on the drawing";
  return { big, unit: n === 1 ? "inch" : "inches", sentence: what,
    metric: pipe && dn ? `DN ${dn} <small>(metric nominal pipe size)</small>` : `${Math.round(n * 25.4)} mm` };
}

function headline(a: Answer, ctx: HeroCtx): { big: string; unit: string; sentence: string; text?: string; name?: boolean; next?: string[]; nextLabel?: string; metric?: string } {
  const v = a.value;
  if (v === null || v === undefined) return { big: "No answer", unit: "", sentence: (a.missing ?? [])[0] ?? "We could not find this on the drawing." };
  if (typeof v === "boolean") return { big: v ? "Yes" : "No", unit: "", sentence: "" };
  const conn = connectionInfo(a);
  if (conn && (typeof v === "string" || a.unit === "count") && !/^\s*(yes|no)\b/i.test(String(v))) {
    const rooms = conn.rooms.filter((r) => r !== "outside");
    const apt = a.intent?.filters?.apartment ? ` (apartment ${a.intent.filters.apartment})` : "";
    if (rooms.length === 1 && typeof v === "string")
      return { big: rooms[0], unit: "", name: true, sentence: `the only room with a door to ${conn.room}${apt}` };
    return { big: String(rooms.length), unit: "", sentence: `${rooms.length === 1 ? "room has a door" : "rooms have a door"} to ${conn.room}${apt}${conn.rooms.includes("outside") ? " (and one door leads outside)" : ""}:`,
      next: rooms, nextLabel: "" };
  }
  const inch = /\b(size|diameter|dia|how (big|large|wide|thick)|thick(ness)?|width|bore|DN|NPS)\b/i.test(ctx.question) || PIPE_Q.test(ctx.question) ? inchSize(a, ctx.question) : null;
  if (inch) return inch;
  const route = routeInfo(a);
  if (route && typeof v === "number")
    return { big: String(v), unit: "", sentence: `${v === 1 ? "door" : "doors"} on the shortest way from ${route.from} to ${route.to}:`, next: route.doors, nextLabel: "" };
  if (typeof v === "string") {
    const yn = v.match(/^\s*(yes|no)\b[\s,.:;-]*\(?(.*?)\)?\s*$/is);
    if (yn) return { big: yn[1][0].toUpperCase() + yn[1].slice(1).toLowerCase(), unit: "", sentence: plainify(yn[2]) };
    // 'length × width' (G1 lengths / plan_rooms: '40.71 m × 16.70 m', '6.43 m x 3.93 m'): one unit, a plain sentence
    const dims = v.match(/^\s*([\d.,]+)\s*(m|ft)?\s*[×x]\s*([\d.,]+)\s*(m|ft)\s*$/i);
    if (dims) {
      const what = /outer|outside|building|footprint|außen|ulko/i.test(ctx.question) ? "outer size of the building" : "size";
      return { big: `${dims[1]} × ${dims[3]}`, unit: dims[4], sentence: `${what} (length × width)` };
    }
    if (v.length <= 28 && !v.includes("\n")) return { big: calm(v), unit: "", sentence: "", name: !/^\d/.test(v) && v.length > 10 };
    // a ranking ("Largest rooms (…):\n- KORIDOR (105) on 1.korrus: 78.40 m²\n…"): the first one is the answer
    const rows = v.split("\n").map((l) => l.match(/^\s*[-•]\s*(.+?)(?:\s+on\s+[^:]+)?:\s*([\d.,]+)\s*(m²|m2|m)/)).filter((m): m is RegExpMatchArray => !!m);
    const sup = ctx.question.match(/\b(biggest|largest|smallest|tallest|highest|lowest|most|least)\b/i);
    if (rows.length >= 1 && sup) {
      const u = rows[0][3].replace("m2", "m²");
      const where = wherePhrase(a, ctx);
      return { big: calm(rows[0][1]), unit: "", name: true, sentence: `the ${sup[1].toLowerCase()} ${a.intent?.target === "room" || /room/i.test(ctx.question) ? "room" : "one"}${where ? ` ${where}` : ""}: ${rows[0][2]} ${u}`,
        next: rows.slice(1, 5).map((r) => `${calm(r[1])} · ${r[2]} ${r[3].replace("m2", "m²")}`) };
    }
    // "Total spaces (whole model): 285\nBy storey:\n- 1.korrus: 50 (17.5%)…": the total big, the split as 'Next'
    const tot = v.match(/^Total (\w+)[^:\n]*:\s*(\d+)\s*\n\s*By (storey|type)[^:\n]*:\s*\n([\s\S]+)$/i);
    if (tot) {
      const noun = NOUN[tot[1].toLowerCase().replace(/s$/, "")]?.[1] ?? tot[1].toLowerCase();
      const per = tot[4].split("\n").map((l) => l.match(/^\s*-\s*(.+?):\s*(\d+)/)).filter((m): m is RegExpMatchArray => !!m);
      return { big: tot[2], unit: "", sentence: `${noun} in the whole building${tot[3].toLowerCase() === "storey" ? ", by floor:" : ", by type:"}`,
        next: per.map((m) => `${plainify(m[1]).replace(/^floor/, "Floor")}: ${m[2]}`), nextLabel: "" };
    }
    return { big: "", unit: "", sentence: "", text: plainify(v) };
  }
  // a length read without a unit (pdf_vector on lone uploads today; G1 adds 'm'): metres when the answer itself is
  // metric (its notes / assumptions / trace speak m or m²), else no unit rather than a guess
  const metric = /\d\s*(m²|m2|m)(?![\w²])/.test([...(a.trace ?? []), ...(a.assumptions ?? []), ...(a.evidence ?? []).map((e) => e.note ?? "")].join("\n"));
  // ('How wide is the corridor?' parses as a text question; the table reader's own code says unit = "m")
  const lengthQ = a.intent?.task === "length" || /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|skirting|base ?boards?|linear (?:metres|meters|feet)|running (?:metres|meters)|LF|breite|länge|leveys|pituus)\b/i.test(ctx.question);
  const unit = unitLabel(a.unit) || (lengthQ && typeof v === "number" && (metric || /\bunit\s*=\s*["']m["']/.test((a.trace ?? []).join("\n"))) ? "m" : "");
  const t = a.intent?.target ?? "";
  const n = NOUN[t];
  const where = wherePhrase(a, ctx);
  const notDrawn = ctx.question.match(/how many (\w+) on '?([^'?]+?)'? are in the (?:3D )?model but not drawn/i);
  if (notDrawn) return { big: fmtValue(a), unit: "", sentence: `${notDrawn[1]} on ${floorName(notDrawn[2])} are in the 3D model but not drawn on the plan` };
  if (a.unit === "count" || (!a.unit && Number.isInteger(v))) {
    const parts = (a.plan?.parts ?? []).map((x) => NOUN[x]?.[1] ?? "").filter(Boolean);
    const noun = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}${a.plan?.op === "sum" ? " together" : ""}`
      : n ? n[Number(v) === 1 ? 0 : 1] : "";
    if (!noun && a.intent?.task !== "count") return { big: fmtValue(a), unit: "", sentence: "" }; // a year, an id
    return { big: fmtValue(a), unit: "", sentence: [noun, where].filter(Boolean).join(" ") };
  }
  if (unit === "€" || unit === "$") return { big: `${unit}${fmtValue(a)}`, unit: "", sentence: costSentence(ctx.question) };
  // a line of 'This floor at a glance' (main.ts glanceCard): its own words ('teaching: 20 rooms')
  if (a.solver === "takeoff" && a.assumptions?.[0]) return { big: fmtValue(a), unit, sentence: String(a.assumptions[0]) };
  // F1's region ask: 'area inside your outline' / 'floor area of the rooms in the area you drew' (not '… on this drawing')
  if (/^region\//.test(String(a.solver ?? "")) && /^(m2|m²|sq ?m|ft2|ft²|sf|sq ?ft)$/i.test(String(a.unit ?? "")))
    return { big: fmtValue(a), unit, sentence: /\/outline$/.test(String(a.solver)) ? "area inside your outline" : `floor area of the rooms ${where}` };
  // the room as asked ("How big is room 119?" → "room 119"), its name from the evidence when there is one ("KORIDOR")
  const asked = ctx.question.match(/\broom\s+'?([\w.()-]*\d[\w.()-]*)'?/i)?.[1];
  const named = (a.evidence ?? []).length && (a.evidence ?? []).every((e) => e.note === a.evidence[0].note)
    ? (a.evidence[0].note ?? "").match(/^([A-ZÄÖÜÕ][A-ZÄÖÜÕ+ -]{2,})\b/)?.[1]?.trim() : undefined;
  const room = a.intent?.filters?.room ? String(a.intent.filters.room) : asked ? `room ${asked}${named ? ` (${named.toLowerCase()})` : ""}` : "";
  const apt = a.intent?.filters?.apartment ? `apartment ${a.intent.filters.apartment}` : "";
  // an area summed over several rooms of one kind ("How big is the kitchen?" on a floor with 4 kitchens): say so
  const rooms = a.intent?.task === "area" ? (a.evidence ?? []).filter((e) => /\d\s*(m²|m2)(?!\w)/i.test(e.note ?? "")).length : 0;
  const kind = a.intent?.target && !/^(unknown|room|space|area|floor)$/i.test(a.intent.target) ? a.intent.target : "";
  const several = !room && !apt && kind && rooms > 1 ? `${rooms} ${kind}${/s$/.test(kind) ? "" : /(ch|sh|x)$/.test(kind) ? "es" : "s"} together` : "";
  // one room named in every note ('Comms Room: 16.68 m² traced at 1:100', E1's line-plan reader; "'OFFICE SPACE' outline
  // traced …") that the question also names: 'floor area of the comms room', not '… on this drawing'
  const ev0 = (a.evidence ?? [])[0]?.note ?? "";
  const noteRoom = ev0.match(/^'?([\p{L}][\p{L}\d .\/-]{1,40}?)'?(?::\s*(?:long side |short side |width |length |skirting |perimeter )?[\d.,]+\s*m|\s+outline\b)/u)?.[1]?.trim();
  const named2 = !room && !apt && !several && noteRoom && roomPhrases(ctx.question).some((ph) => noteRoom.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "").includes(ph))
    ? `the ${noteRoom.toLowerCase()}` : "";
  // a room's printed label read as text ('Text: Ncoic Office / 2.05 / 81 SF (view …)', 'Text: Conference/ / Classroom /
  // 2.01 / 536 SF'): the name = the parts before the first number, in the question's own casing ('NCOIC Office')
  const printedRoom = (() => {
    if (room || apt || several || named2 || a.intent?.task !== "area") return "";
    const tm = ev0.match(/^text:\s*(.+)$/i);
    if (!tm) return "";
    const parts: string[] = [];
    for (const t of tm[1].split(/\s+\/\s+/)) {
      if (/^\d/.test(t.trim())) break;
      parts.push(t.trim());
    }
    const nm = parts.join(" ").replace(/\/\s+/g, "/").replace(/\/$/, "").trim();
    const key = nm.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
    if (nm.length < 3 || !roomPhrases(ctx.question).some((ph) => ph.length >= 4 && key.includes(ph))) return "";
    const inQ = ctx.question.match(new RegExp(nm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\//g, "\\s*/?\\s*").replace(/\s+/g, "\\s*/?\\s*"), "i"))?.[0];
    return inQ ?? nm.toLowerCase();
  })();
  if (printedRoom && /^(m2|m²|ft2|sf|sq ?ft)$/i.test(String(a.unit ?? "")))
    return { big: fmtValue(a), unit, sentence: `floor area of the ${printedRoom}, as printed on the drawing` };
  // a printed total read off the sheet ('TOTAL COMMON SPACE = 4,479 SF'): its own words + the printed figure
  const pr = /^(m2|m²|ft2|sf|sq ?ft)$/i.test(String(a.unit ?? "")) ? printedReading(a) : "";
  if (pr) {
    const what = pr.split(/\s*[=:]\s*|\s+(?=\d[\d,.]*\s*\S+$)/)[0].trim();
    const fig = pr.match(/(\d[\d,]*(?:\.\d+)?\s*\S+)$/)?.[1] ?? pr;
    const label = what && !/^\d/.test(what) && what.length <= 60 ? (/\p{Ll}/u.test(what) ? what : what.toLowerCase()) : "";
    return { big: fmtValue(a), unit, sentence: `${label ? `${label}, ` : ""}as printed on the drawing${/^m/i.test(String(a.unit)) && /sf|ft/i.test(fig) ? ` (${fig})` : ""}` };
  }
  // skirting / baseboard (Stoa kind 2, E1's lineplan): 'Laundry: skirting 10.13 m = outline 10.95 m minus 1 opening'
  // → 'skirting for the laundry: its outline 10.95 m minus 1 opening in its walls'; several rooms → 'N rooms together'
  if (SKIRT_Q.test(ctx.question) && a.intent?.task !== "count") {
    const runs = (a.evidence ?? []).map((e) => (e.note ?? "").match(/^'?(.+?)'?:\s*skirting\s+([\d.,]+)\s*m\s*=\s*outline\s+([\d.,]+)\s*m\s+minus\s+(\d+)\s+openings?/i)).filter(Boolean) as RegExpMatchArray[];
    if (runs.length === 1) {
      const [, nm, net, outl, k] = runs[0];
      const who = named2 || `the ${nm.toLowerCase()}`;
      // QA 19:59 (2): 'without deducting doors' → the answer is the whole outline; say so (and what deducting would give)
      if (wholeOutline(a, ctx.question, Number(outl)))
        return { big: fmtValue(a), unit, sentence: `of skirting for ${who}: its whole outline, door openings not deducted (as asked; with ${k} opening${k === "1" ? "" : "s"} deducted: ${net} m)` };
      return { big: fmtValue(a), unit, sentence: `of skirting for ${who}: its outline ${outl} m minus ${k} opening${k === "1" ? "" : "s"} in its walls` };
    }
    if (runs.length > 1) return { big: fmtValue(a), unit, sentence: `of skirting for ${runs.length} rooms together (each outline minus its openings)` };
    return { big: fmtValue(a), unit, sentence: [named2 ? `of skirting for ${named2}` : "of skirting", where].filter(Boolean).join(" ") };
  }
  // a slab type's area (Stoa kind 4, E1's slab_area): the callout it was asked by, as printed ('300 SLAB-ON-GRADE W/ D16
  // @ 300 TOP FACE. BOTTOM: 212.45 m² traced at 1:50') → 'area of the slab "300 slab-on-grade w/ D16 @ 300", to its edge'
  if (/\bslabs?\b/i.test(ctx.question) && /^(m2|m²|ft2|sf)$/i.test(String(a.unit ?? ""))) {
    const slabs = (a.evidence ?? []).map((e) => (e.note ?? "").match(/^(.*?\bSLAB\b.*?):\s*[\d.,]+\s*(?:m²|m2|SF|ft²)/i)?.[1]).filter(Boolean) as string[];
    if (slabs.length === 1) {
      const callout = slabs[0].replace(/\s+(TOP|BOTT?OM|T\.?O\.?S)\b.*$/i, "").replace(/\bSLAB-ON-GRADE\b/gi, "slab on grade").replace(/\bW\/\s*/g, "with ").trim();
      return { big: fmtValue(a), unit, sentence: `area of the slab ‘${callout}’, traced to its edge` };
    }
    if (/\btotal\b/i.test(ctx.question)) return { big: fmtValue(a), unit, sentence: "total slab area, to the slab edges" };
  }
  const what = a.intent?.task === "area" ? (room ? `floor area of ${room}` : apt ? `floor area of ${apt}` : several ? `floor area of ${several}` : named2 ? `floor area of ${named2}` : "floor area")
    : a.intent?.task === "length" ? (named2 ? `length of ${named2}` : "length") : a.intent?.task === "volume" ? "volume" : "";
  return { big: fmtValue(a), unit, sentence: [what, where].filter(Boolean).join(" ") };
}
/** skirting / baseboard questions (Stoa kind 2): a length of wall face minus openings */
const SKIRT_Q = /\b(skirting|base ?boards?|sockelleiste\w*|jalkalista\w*|plinth)\b/i;
const plainify = (s: string) => swissRooms(s).replace(/'?(\d+)\.korrus'?/g, (_, n) => `floor ${n}`).replace(/\bstorey\b/g, "floor").replace(/\bIFC model\b/g, "3D model").replace(/\bplan draws\b/g, "the drawing shows")
  .replace(/\bthe plan's\b/g, "the drawing's").trim();
function costSentence(q: string) {
  const m = q.match(/(\d[\d,.]*)\s*(euros?|eur|€|dollars?|usd|\$)/i);
  return m ? `in total, at ${m[1]} ${/^(e|€)/i.test(m[2]) ? "€" : "$"} each` : "in total";
}

/** Which kind of second source agreed, in words. */
/** A cross-check in the answer's trace or notes, in words. ok → '2 independent methods agree' + which two (captain 15:12:
 *  a 0.22 next to two agreeing methods read as 'probably wrong' to Ziying, so the agreement is said plainly). */
/** a second, independent method agrees with the answer (cross-check ✓, or the hybrid's 'two independent methods agree') */
export const agrees = (a: Answer, file: string) => !!agreement(a, file)?.ok;
function agreement(a: Answer, file: string): { ok: boolean; text: string; detail: string; which?: string } | null {
  const xs = crossChecks(a, agentAsks(a)).filter((x) => !/^skipped\b/i.test(x.text));
  if (!xs.length) {
    // G1's hybrid routing (night-G1 558f6bc): 'two independent readings agree (ours X, Gemini X) ✓' in the trace, or the
    // assumption 'two independent methods agree (…)'
    const lines = [...(a.trace ?? []), ...(a.assumptions ?? [])].map(String);
    const hit = lines.find((l) => /two independent (readings|methods) agree/i.test(l) && !/disagree/i.test(l));
    if (!hit) {
      // a printed figure read by Gemini that one of our own readers had read the same way ('raster (question as asked):
      // 4,479 SF …' next to Gemini's 'Read 'TOTAL COMMON SPACE = 4,479 SF''): two readings of the print agree
      const pr = printedReading(a);
      const fig = pr.match(/(\d[\d,]*(?:\.\d+)?)\s*(\S+)$/);
      if (!fig || !lines.some((l) => /gemini/i.test(l))) return null;
      const ours = lines.find((l) => !/gemini/i.test(l) && /^\s*\w+[\w ()]*:/.test(l) && new RegExp(String.raw`(^|[^\d,.])${fig[1].replace(/[.,]/g, "\\$&")}\s*${fig[2].replace(/[^\w]/g, "")}\b`, "i").test(l));
      return ours ? { ok: true, text: "2 independent methods agree", detail: `${ours.slice(0, 140)} · Gemini: ${pr}`, which: "our reading and Gemini's" } : null;
    }
    return { ok: true, text: "2 independent methods agree", detail: hit.slice(0, 300), which: /gemini/i.test(hit) ? "our reading and Gemini's" : "two readings" };
  }
  const bad = xs.find((x) => !x.ok);
  if (bad) return { ok: false, text: "Two sources disagree: check the marked spots", detail: bad.text };
  const t = xs[0].text;
  const sheet = /\.(pdf|png|jpe?g)$/i.test(file);
  const both = /\bIFC\b|3D model|model/i.test(t) && /\b(plan|drawing|drawn|symbols?|sheet)\b/i.test(t);
  const which = both && sheet ? "drawing and 3D model"
    : /\bIFC\b|model/i.test(t) ? "drawing and 3D model"
    : /\bDXF\b|CAD/i.test(t) ? "drawing and its CAD file"
    : /sheet's own text labels/i.test(t) ? "lines and printed labels"
    : /segmentation|detector|reader|label/i.test(t) ? "two readings of the drawing"
    : /schedule/i.test(t) ? "drawing and schedule"
    : "two sources";
  return { ok: true, text: "2 independent methods agree", detail: t, which };
}

/** A short, human label for one piece of evidence. */
function evidenceLabel(note: string): string {
  let s = (note ?? "").split(/;\s*cross-?check:/i)[0].split(" | ")[0].trim();
  s = s.replace(/^[^—?]{3,160}\?\s+—\s+/, ""); // the agent prefixes the question it asked: "What is …? — 2.02 Küche: …"
  // the Swiss CAD file's rooms and doors (dxf solver): "room 2.14 'Korridor': 10.86 m² (plan prints 10.86 m² ✓)",
  // "door 15: 2.14 'Korridor' <-> 2.10 'Zimmer'", "door 22 (2.14 'Korridor' <-> 2.31 'Korridor')"
  const door = s.match(/^door \d+(?::\s*|\s*\()(.+?)\s*<->\s*(.+?)\)?$/i);
  if (door) {
    const [x, y] = [roomWords(door[1]), roomWords(door[2])];
    return y === "outside" ? `Door from ${x} to outside` : `Door between ${x} and ${y}`;
  }
  // E1 4c83367: a printed area that wins over the drawn outline leads the note: "room 2.10 'Treppenhaus': 29.03 m² as
  // printed (DXF polygon 35.38 m² ✗)" → "Treppenhaus 2.10 · 29.03 m² (as printed; drawn outline 35.38 m²)"
  const won = s.match(/^room (.+?):\s*([\d.]+) m² as printed \((?:DXF polygon|drawn outline) ([\d.]+) m²\s*✗?\)$/i);
  if (won) return `${roomWords(won[1])} · ${won[2]} m² (as printed; drawn outline ${won[3]} m²)`;
  const room = s.match(/^room (.+?):\s*([\d.]+) m²(?:\s*\(plan prints ([\d.]+) m² (✓|✗)\))?$/i);
  if (room) return `${roomWords(room[1])} · ${room[2]} m²${room[4] === "✓" ? " (as printed ✓)" : room[3] ? ` (printed ${room[3]} m²)` : ""}`;
  // rooms read off a scan or photo (raster): "2.02 Küche: printed '12.05 m²'", "2.07: no printed area; flood-filled
  // outline 1.61 m²" (OCR may glue a unit to the room number: "2.13 m²: printed '11.05 m²'")
  const scan = s.match(/^([\w.]*\d[\w.]*)(?:\s+m²)?(?:\s+([^:]+?))?:\s*(?:printed '([\d.,]+)\s*m²'|no printed area; flood-filled outline ([\d.,]+)\s*m²)/i);
  if (scan) return `${scan[2] ? `${scan[2]} ${scan[1]}` : `Room ${scan[1]}`} · ${scan[3] ? `${scan[3]} m² (printed)` : `about ${scan[4]} m² (traced outline)`}`;
  s = s.replace(/^(label reader|CubiCasa5K detector|door-swing reader):\s*/i, "")
    .replace(/^named in the answer:\s*/i, "").replace(/^sheet label '([^']+)'.*$/i, "$1")
    .replace(/\bIfc(Door|Window|Space|Wall|WallStandardCase|Slab|Column|Stair|Beam|Railing|SanitaryTerminal|FlowTerminal)\b/g, (_, k) => k === "WallStandardCase" ? "Wall" : k === "Space" ? "Room" : k)
    .replace(/\s*\(GlobalId[^)]*\)/i, "").replace(/\s*\((CubiCasa5K|pretrained)[^)]*\)/i, "").replace(/\s*\(\d+ lines?\)/i, "")
    // a segmented room on a scan: 'sauna region R18' → 'Sauna' (the region number means nothing to a visitor, M1 14:40)
    .replace(/\s+region R\d+\b/i, "").replace(/,\s*printed label '([^']+)'/i, " (labelled '$1')")
    .replace(/^(\w+):\s*(?=\1\b)/i, "").replace(/\s+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  return s.length > 60 ? s.slice(0, 57).replace(/\s+\S*$/, "") + "…" : s || "marked spot";
}

export function heroLoadingHtml(q: string) {
  return `<div class="hero-loading"><div class="spinner"></div><div><b>Looking it up…</b><small>${esc(q)}</small><small class="elapsed"></small></div></div>`;
}

/** Row swatches of a list answer (calm, distinct, readable on white and dark). */
const SWATCH = ["#0071e3", "#34c759", "#ff9f0a", "#af52de", "#5ac8fa", "#ff375f", "#a2845e", "#30b0c7", "#8e8e93", "#ffd60a", "#64d2ff", "#bf5af2"];

interface Qty { value: number; kind: "area" | "length" | "count"; label: string; toMetric?: number; metric?: string }
const fmtQty = (v: number, label: string) => `${v.toLocaleString("en", { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 })} ${label}`.trim();

/** The quantity in a list row's detail, in the drawing's unit, with its metric value when that unit is imperial:
 * '1,234 SF' → 114.6 m², "12'-6\"" → 3.81 m, '45 LF' → 13.72 m; '63.84 m²' stays; '12 doors' is a count. */
export function quantityOf(detail: string, strict = false): Qty | null {
  const d = detail.replace(/\u2032/g, "'").replace(/\u2033/g, '"').trim();
  // feet-inches only as the WHOLE value or its start ("12'-6\"", "12' 6\" (3.81 m)"), never a quote inside text
  // ('Zimmer 2.10' … is not 10 feet: tour step 6)
  const ftin = d.match(/^(\d+)\s*'\s*-?\s*(\d+(?:\.\d+)?)?\s*(?:"|'')?(?=\s|$|\()/);
  const num = d.match(/^(-?\d[\d,]*(?:\.\d+)?)\s*(sq\.?\s*ft|square feet|sf\b|ft²|ft2|m²|m2|sqm|lf\b|linear feet|ft\b|feet|mm\b|cm\b|m\b)?/i);
  if (strict && !(ftin && ftin[0].length >= d.replace(/\s*\(.*\)\s*$/, "").length) && !(num && num[0].trim().length === d.trim().length)) return null;
  if (ftin && !/sq|sf|ft²|ft2/i.test(d)) {
    const m = (Number(ftin[1]) * 12 + Number(ftin[2] ?? 0)) * 0.0254;
    return { value: Number(ftin[1]) + Number(ftin[2] ?? 0) / 12, kind: "length", label: "ft", toMetric: 0.3048, metric: `${m.toFixed(2)} m` };
  }
  if (!num) return null;
  const v = Number(num[1].replace(/,/g, ""));
  if (!Number.isFinite(v)) return null;
  const u = (num[2] ?? "").toLowerCase().replace(/\s+/g, " ");
  if (/sq ?\.? ?ft|square feet|^sf$|ft²|ft2/.test(u)) return { value: v, kind: "area", label: "SF", toMetric: 0.09290304, metric: `${(v * 0.09290304).toFixed(1)} m²` };
  if (/^(lf|linear feet|ft|feet)$/.test(u)) return { value: v, kind: "length", label: u === "lf" || u === "linear feet" ? "LF" : "ft", toMetric: 0.3048, metric: `${(v * 0.3048).toFixed(2)} m` };
  if (/^(m²|m2|sqm)$/.test(u)) return { value: v, kind: "area", label: "m²" };
  if (u === "m") return { value: v, kind: "length", label: "m" };
  if (u === "mm") return { value: v, kind: "length", label: "mm", toMetric: 0.001, metric: `${(v / 1000).toFixed(2)} m` };
  if (u === "cm") return { value: v, kind: "length", label: "cm", toMetric: 0.01, metric: `${(v / 100).toFixed(2)} m` };
  if (!u && /^\s*-?\d[\d,]*\s*(\p{L}+(\s\p{L}+)?)?\s*$/u.test(d) && Number.isInteger(v)) return { value: v, kind: "count", label: "" };
  return null;
}

/** A list the answer writes as 'name: value' items, one per line or separated by ';' ('Dormitory: 12,345 SF',
 * '- Room 201: 14.2 m', 'CV-1 check valve: 5'): ≥ 2 items, each with a number; each linked to the evidence that names it. */
export function listAnswer(a: Answer, currentFile = ""): { head: string; rows: SetRow[]; total?: string } | null {
  if (typeof a.value !== "string") return null;
  let v = a.value.trim();
  let head = "";
  let total = "";
  if (!v.includes("\n")) {
    const h = v.match(/^([^:;]{3,80}?):\s+(.+;.+)$/);
    if (h && !/\d/.test(h[1])) { head = h[1].trim(); v = h[2]; }
  }
  // sub-headers ('By storey:') become the header; a printed total line ('Total spaces (whole model): 285') is the total,
  // never a row (summing it with its parts would double it)
  const items: string[] = [];
  for (const raw of v.split(/\n+|;\s*/)) {
    const x = raw.replace(/^\s*(?:[-•*·]|\d+[.)])\s+/, "").trim();
    if (!x) continue;
    if (/:$/.test(x) && !/\d/.test(x)) { head ||= x.replace(/:$/, ""); continue; }
    const t = x.match(/^((?:sub)?total\b|sum\b|overall\b|in total\b)[^:=]{0,60}[:=]\s*(.+)$/i);
    if (t && /\d/.test(t[2])) { total = t[2].trim(); continue; }
    items.push(x);
  }
  if (items.length < 2 || items.length > 60) return null;
  const parsed = items.map((it) => it.match(/^(.{1,60}?)\s*(?::|=|–|—|\s-\s)\s*(.{1,80})$/));
  if (parsed.some((m) => !m || !/\d/.test(m[2]))) return null;
  const ev = a.evidence ?? [];
  const key = (x: string) => ` ${x.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim()} `;
  const hay = ev.map((e) => key(e.note ?? ""));
  const rows: SetRow[] = parsed.map((m) => {
    const name = m![1].trim(), detail = m![2].trim().replace(/\bm2\b/g, "m²");
    const k = key(name);
    const i = k.trim().length >= 2 ? hay.findIndex((h) => h.includes(k)) : -1;
    return { name, detail, ev: i, where: i >= 0 && ev[i].bbox ? "2D" : "", other: i >= 0 && !!ev[i].bbox && ev[i].file !== currentFile };
  });
  return { head, rows, total };
}

export function heroHtml(a: Answer, ctx: HeroCtx): string {
  if (needsMeasuring(a, ctx.question, ctx.file, ctx.sheet)) return measureFirstHtml(a, ctx);
  const h = headline(a, ctx);
  const ag = agreement(a, ctx.file);
  let c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  // two methods that agree are not a 'rough guess … not found on the drawing', whatever the calibrated number says
  if (ag?.ok && c.cls.includes("guess")) c = { cls: "est", word: "Best estimate", hint: "" };
  const conv = conventionOf(a, ctx.question);
  // the convention line already says it: don't repeat a reader's 'Convention: …' as the assumption
  // a printed figure read off the sheet is not 'NOT MEASURED: … visual estimate' (the solver's words for any Gemini reading)
  const assumption = c.cls.startsWith("est") ? plainAssumption((a.assumptions ?? []).find((x) => !/^\s*convention\s*:/i.test(x)
    && !(/^NOT MEASURED\b|^read by gemini/i.test(x) && printedReading(a))) ?? "") : "";
  // AUDIT 19:51 (3): the time the visitor waited (the request's round trip, queueing included); the solver's own time
  // only for a re-render without a request (a choice, a scale change)
  const wall = ctx.roundTripMs > 0 ? ctx.roundTripMs / 1000 : a.cost?.wall_seconds && a.cost.wall_seconds > 0 ? a.cost.wall_seconds : 0;
  const mt = manualTime(a.intent?.task, a.intent?.scope, ctx.question);
  const set: { head: string; rows: SetRow[]; total?: string } | null = typeof a.value === "string" ? setAnswer(a, ctx.file) ?? (h.next?.length ? null : listAnswer(a, ctx.file)) : null; // a headline that already lists the items ('Floor 1: 50 · …') keeps its own form
  const ev = a.evidence ?? [];
  // the numbered list: the first few distinct places (the viewer numbers the same ones)
  const shown = numbered(ev, numberMax(a), spreadOf(a));
  const approx = approxEvidence(a);
  // a room's doors: "Door to Zimmer 2.10" (the room asked about is in the sentence above)
  const conn = connectionInfo(a);
  const label = (i: number) => {
    const l = ctx.labelOf?.(i) ?? calm(evidenceLabel(ev[i].note ?? ""));
    return conn ? l.replace(`Door between ${conn.room} and `, "Door to ").replace(`Door from ${conn.room} to outside`, "Door to outside") : l;
  };
  const more = numbered(ev, Infinity).length - shown.length;
  // several items drawn on ONE box (a Swiss double door = 2 doors at one spot, E1 15:22): '×2' on the place, and the
  // total in the 'more' line, so the marks add up to the count (tour step 7: 55 places, 64 doors and windows). Items
  // are told apart by GlobalId (an IFC element's 2D box and 3D item are one thing), else by their position in the list.
  const spotKey = (e: Answer["evidence"][number]) => `${e.file}|${e.page ?? 0}|${(e.bbox ?? []).join(",")}`;
  const spots = new Map<string, Set<string>>();
  ev.forEach((e, i) => {
    if (!e.bbox) return;
    const k = spotKey(e);
    if (!spots.has(k)) spots.set(k, new Set());
    spots.get(k)!.add(e.ifc_guid ?? `#${i}`);
  });
  const atSpot = (i: number) => (ev[i].bbox ? spots.get(spotKey(ev[i]))?.size ?? 1 : 1);
  const stacked = [...spots.values()].filter((x) => x.size > 1).length;
  const items = [...spots.values()].reduce((n, x) => n + x.size, 0);
  const stackNote = stacked && (a.unit === "count" || a.intent?.task === "count")
    ? ` · ${items} in all: ${stacked} spot${stacked > 1 ? "s hold" : " holds"} two or more` : "";
  const place = (i: number) => {
    const e = ev[i];
    if (e.ifc_guid && !e.bbox) return "3D model";
    return ctx.sheetLabel(e.file) || (e.file === ctx.file ? "this drawing" : e.file.split("/").pop()!.replace(/\.[^.]+$/, ""));
  };
  const text = h.text && !set ? `<div class="hero-text">${esc(h.text).split(/\n+/).filter((l) => l.trim()).slice(0, 8).map((l) => `<div>${l}</div>`).join("")}</div>` : "";
  // list answers (areas by room type, baseboard per room, slab types, counted components): a small table with a colour
  // swatch per row, the drawing's own units plus metric, and the sum when the rows share a unit (captain 16:15)
  const qty = set ? set.rows.map((r) => quantityOf(r.detail)) : [];
  const total = set && qty.length >= 2 && qty.every((x) => x && x.kind === qty[0]!.kind && x.kind !== "count" && x.label === qty[0]!.label)
    ? qty.reduce((n, x) => n + x!.value, 0) : null;
  const totalCount = set && qty.length >= 2 && qty.every((x) => x && x.kind === "count") ? qty.reduce((n, x) => n + x!.value, 0) : null;
  const shownRows = set ? Math.min(set.rows.length, 12) : 0;
  // an imperial answer (US / UK sheets: '26,562 SF', "12'-6\"", '45 LF') also in metric, under the number (captain 16:15)
  const iq = typeof a.value === "number" || typeof a.value === "string" ? quantityOf(`${a.value}${a.unit && !String(a.value).includes(a.unit) ? ` ${a.unit}` : ""}`, true) : null;
  const imperial = h.metric ? "" : iq?.metric && iq.label !== "mm" && iq.label !== "cm" && !set ? iq.metric.replace(/^(\d+)(\.\d)/, (_m, x: string, y: string) => `${Number(x).toLocaleString("en")}${y}`) : "";
  const imperialUnit = iq?.label === "SF" ? "square feet" : iq?.label === "LF" ? "linear feet" : "feet";
  const givenTotal = set?.total ? `<div class="hs-total"><span>Total</span><b>${esc(set.total)}${quantityOf(set.total)?.metric ? `<small> · ${esc(quantityOf(set.total)!.metric!)}</small>` : ""}</b></div>` : "";
  const setRows = set ? `<div class="hero-set">${set.head ? `<div class="hs-head">${esc(plainify(set.head))}</div>` : ""}${set.rows.slice(0, shownRows).map((r, k) =>
    `<button type="button" class="hs-row"${r.ev >= 0 ? ` data-ev="${r.ev}"` : ""}><span class="hs-name"><i class="hs-sw" style="background:${SWATCH[k % SWATCH.length]}"></i>${esc(r.name)}</span><b>${esc(r.detail)}${qty[k]?.metric ? `<small> · ${esc(qty[k]!.metric!)}</small>` : ""}</b></button>`).join("")}${set.rows.length > shownRows ? `<div class="hs-more">+ ${set.rows.length - shownRows} more</div>` : ""}${
    givenTotal ? givenTotal : total !== null ? `<div class="hs-total"><span>Total <small>(sum of the rows)</small></span><b>${esc(fmtQty(total, qty[0]!.label))}${qty[0]!.toMetric ? `<small> · ${esc(fmtQty(total * qty[0]!.toMetric, qty[0]!.kind === "area" ? "m²" : "m"))}</small>` : ""}</b></div>`
    : totalCount !== null ? `<div class="hs-total"><span>Total</span><b>${totalCount.toLocaleString("en")}</b></div>` : ""}</div>` : "";
  // a guess (under 0.4, no second method agreeing): '≈ 16', in grey, so it does not read as a measurement (Ziying 16:26)
  const guess = isGuess(a, !!ag?.ok) && !h.name && /^\d/.test(h.big);
  // a number read as text with its unit ('4,479 SF', '26,562 SF'): the unit goes next to the number in small type, like
  // m², so the big line does not wrap ('≈ 4,479' / 'SF' on two lines at 1440 px)
  const tu = !h.unit && !h.name ? h.big.match(/^(\d[\d,.]*)\s*(SF|sq\.?\s?ft|ft²|ft2|LF|m²|m2|mm|cm|m|ft|in)\.?$/i) : null;
  const bigTxt = tu ? tu[1] : h.big;
  const unitTxt = tu ? tu[2].replace(/^ft2$/i, "ft²").replace(/^m2$/i, "m²").replace(/^sq\.?\s?ft$/i, "sq ft") : h.unit;
  return `
    ${h.big ? `<div class="hero-value"><span class="big${h.name ? " name" : ""}${!h.name && bigTxt.length > 8 ? " long" : ""}${guess ? " guess-val" : ""}"${guess ? ` title="Our guess: we are unsure of this number (confidence ${Math.round((a.confidence ?? 0) * 100)} %)"` : ""}>${guess ? "≈ " : ""}${esc(bigTxt)}</span>${unitTxt ? `<span class="unit">${esc(unitTxt)}</span>` : ""}</div>` : ""}
    ${imperial ? `<div class="hero-metric">= ${esc(imperial)} <small>(drawing in ${esc(imperialUnit)})</small></div>` : ""}
    ${h.metric ? `<div class="hero-metric">= ${h.metric}</div>` : ""}
    ${h.sentence ? `<div class="hero-sentence">${esc(h.sentence)}</div>` : ""}
    ${h.next?.length ? `<div class="hero-next">${h.nextLabel === "" ? "" : "<span>Next:</span> "}${h.next.map(esc).join(" &nbsp;·&nbsp; ")}</div>` : ""}
    ${text}${setRows}
    <div class="hero-badges">
      <span class="conf ${c.cls}" title="Our calibrated confidence: ${Math.round((a.confidence ?? 0) * 100)} %">${esc(c.word)}</span>${c.hint ? `<span class="conf-hint">${esc(c.hint)}</span>` : ""}
      ${ag ? `<span class="agree ${ag.ok ? "ok" : "bad"}" title="${esc(ag.detail)}">${ag.ok ? "" : "⚠ "}${esc(ag.text)}${ag.which ? `<small> · ${esc(ag.which)}</small>` : ""}</span>` : ""}
      ${tierHtml(a)}
      ${secondOpinions(a)}
    </div>
    ${supportHtml(a, esc)}
    ${conv ? `<div class="hero-conv" title="How we measure or count this kind of question (our convention; the other value is in 'How we worked it out')"><b>Convention:</b> ${esc(conv.text)}${conv.alt ? ` <span class="hc-alt">· other way: ${esc(conv.alt)}</span>` : ""}</div>` : ""}
    ${assumption ? `<div class="hero-assume">Assumed: ${esc(assumption)}</div>` : ""}
    ${unnamedAlt(a)}
    <div class="hero-value-strip" title="Manual time is our rough estimate for this kind of question (${esc(mt.work)}), not a measurement">
      <span>Answered in <b>${wall < 1 ? `${Math.max(0.1, wall).toFixed(1)} s` : `${wall.toFixed(1)} s`}</b></span>
      <span>about <b>${mt.minutes} min</b> by hand <small>(estimate)</small></span>
      ${dataLineHtml(a, esc)}
    </div>
    ${ctx.groups && ctx.groups.length > 1 ? `<div class="hero-where"><div class="hw-title">Where it comes from · by floor</div>
      ${ctx.groups.map((g, k) => `<button type="button" class="hw-item" data-floor="${k}"><span class="num pin-badge">${k + 1}</span><span class="lbl">${esc(g.label)} · <b>${g.guids.length}</b> ${esc(targetNoun(a.intent?.target, g.guids.length))}</span></button>`).join("")}
      <div class="hw-more">every one lit in the 3D model; pick a floor to see only that floor</div></div>`
    : shown.length ? `<div class="hero-where${ctx.rank ? " ranked" : ""}"><div class="hw-title">Where it comes from${ctx.rank ? rankLegendHtml(Math.min(ctx.rank.length, 5)) : ""}</div>
      ${shown.map((i, k) => `<button type="button" class="hw-item" data-ev="${i}"><span class="num pin-badge${ctx.rank && ctx.rank.indexOf(i) >= 0 ? ` rk${ctx.rank.indexOf(i) + 1}` : ""}">${k + 1}</span><span class="lbl">${esc(place(i))} · ${esc(label(i))}${atSpot(i) > 1 ? ` <span class="stack-tag" title="${atSpot(i)} items drawn at this one spot">×${atSpot(i)}</span>` : ""}${approx.has(i) ? ` <span class="approx-tag" title="Traced or recognised on the drawing: the place and outline are approximate">approximate</span>` : ""}</span></button>`).join("")}
      ${shown.some((i) => approx.has(i)) ? `<div class="hw-legend"><span class="lgi"><span class="lg exact"></span>exact place</span><span class="lgi"><span class="lg approx"></span>approximate (traced or recognised, not exact)</span></div>` : ""}
      ${more > 0 ? `<div class="hw-more">+ ${more} more, all marked ${/\.ifc$/i.test(ctx.file) || !ev.some((e) => e.bbox && e.file === ctx.file)
        || ev.some((e) => e.ifc_guid && !(e.bbox && e.file === ctx.file) && !ev.some((x) => x.ifc_guid === e.ifc_guid && x.bbox && x.file === ctx.file))
        ? "in the 3D model" : "on the drawing"}${esc(stackNote)}</div>` : ""}</div>` : ""}`;
}

/** The measuring convention behind an answer (captain 16:59, KNOWLEDGE DECISION): the reader's own 'Convention: …' line
 *  (assumptions / trace) first; else the team's convention for the question's kind, so every baseboard / door / window /
 *  room-area / slab answer says how it was counted. alt = the other convention's value when the reader gives one. */
export function conventionOf(a: Answer, q: string): { text: string; alt?: string } | null {
  if (a.value === null || a.value === undefined || typeof a.value === "boolean") return null;
  const lines = [...(a.assumptions ?? []), ...(a.trace ?? [])].map(String);
  const own = lines.find((l) => /^\s*convention\s*:/i.test(l));
  const altLine = lines.find((l) => /\balternative\b[^:\n]{0,80}:\s*\S/i.test(l) && !/^\s*convention\s*:/i.test(l));
  const alt = altLine?.match(/\b(alternative\b[^:\n]{0,80}):\s*([^\n;]{1,80})/i);
  const altText = alt ? `${alt[1].replace(/^alternative\s*/i, "").replace(/^\(|\)$/g, "").trim() || "other convention"}: ${alt[2].trim()}` : undefined;
  if (own) return { text: own.replace(/^\s*convention\s*:\s*/i, "").replace(/\s+/g, " ").trim().slice(0, 220), alt: altText };
  const task = a.intent?.task;
  const t = (a.intent?.target ?? "").toLowerCase();
  // the area of the visitor's own outline (F1 region/outline) is that polygon at the scale: no room convention applies
  if (/^region\/outline$/.test(String(a.solver ?? ""))) return null;
  let text = "";
  if (/\b(base ?boards?|skirting|skirtings|sockelleiste|jalkalista|plinth)\b/i.test(q))
    text = wholeOutline(a, q) ? "room perimeter at the inner wall face, door openings NOT deducted (as you asked)"
      : "room perimeter at the inner wall face, minus the door openings (a double door deducts both leaves)";
  else if ((task === "count" || (/^\s*how many\b/i.test(q) && !HOW_MANY_UNITS.test(q))) && (/door|window/.test(t) || /\b(doors?|windows?|türen|fenster|ovet|ikkunat)\b/i.test(q))) {
    const d = /door/.test(t) || /\b(doors?|türen|ovet)\b/i.test(q), w = /window/.test(t) || /\b(windows?|fenster|ikkunat)\b/i.test(q);
    text = [d ? "each door opening counts once (a double door = 1 door, 2 leaves)" : "", w ? "window units drawn in the walls" : ""].filter(Boolean).join("; ");
  } else if ((task === "area" || /\b(area|how (big|large)|square (metres|meters|feet)|m²|m2|sq ?ft|sf)\b/i.test(q))
    // QA 18:56: 'What size is the suction header?' parses as area; its answer '16 inches' (a pipe tag) has no area convention
    && (!a.unit || /^(m2|m²|sq ?m|ft2|ft²|sf|sq ?ft)$/i.test(String(a.unit)))) {
    if (/\b(slabs?|foundation|footing|bodenplatte|laatta)\b/i.test(q)) text = "plan area to the slab edge (thickened edges included); openings deducted only where drawn as voids";
    else if (!/\b(total|whole|gross|building|storey|floor plate|gifa|gia)\b/i.test(q) && (/^(room|space)$/.test(t) || roomPhrases(q).length))
      text = "net internal area, to the inner face of the walls";
  }
  return text ? { text, alt: altText } : null;
}

/** A reader's first assumption in a site engineer's words (the full text stays under 'How we worked it out'). */
function plainAssumption(t: string): string {
  if (/^read from \d+ printed text group/i.test(t)) return "read from the printed text on the drawing (the marked tag)";
  if (/^recognised as:/i.test(t)) return "";
  return t;
}

/** The hero when nothing was measured (needsMeasuring). Never an empty answer (captain 14:39): the reader's number as
 *  a rough guess ('a typical size, not measured on this drawing'), and main.ts measureFirst replaces it with the room
 *  traced at the drawing's scale, or at an estimated one (door swings, else the usual 1:100), with 'Scale unknown —
 *  set it' next to the number until the scale is known. */
function measureFirstHtml(a: Answer, ctx: HeroCtx): string {
  const length = a.intent?.task === "length" || (a.intent?.task !== "area" && /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|skirting|base ?boards?|linear (?:metres|meters|feet)|running (?:metres|meters)|LF|breite|länge|leveys|pituus|largeur|longueur)\b/i.test(ctx.question));
  const bad = implausible(a, ctx.question, ctx.sheet);
  const v = typeof a.value === "number" && a.value > 0 && !bad ? a.value : null;
  const conv = conventionOf(a, ctx.question);
  const unit = unitLabel(a.unit) || (length ? "m" : "m²");
  const big = v !== null ? v.toLocaleString("en", { maximumFractionDigits: v >= 100 ? 0 : v >= 10 ? 1 : 2 }) : "";
  const sentence = big
    ? readerGuess(a) === "typical" ? `a typical ${length ? "length" : "size"} for this — not measured on this drawing yet`
      : `read off a picture of the drawing without marking where — not measured yet`
    : bad === "echo" ? `The reading found only ‘${String(a.value).trim()}’, the number in your question, not the ${length ? "length" : "area"}.`
    : bad === "level" ? `The reading took ‘${levelRead(a)}’ — a level printed on the drawing, not ${length ? "a length" : "an area"} — so it is not shown as the answer.`
    : bad === "too big" ? `The reading gave ${typeof a.value === "number" ? a.value.toLocaleString("en", { maximumFractionDigits: 0 }) : String(a.value)} ${unitLabel(a.unit) || ""}, more than the whole sheet covers at its scale (about ${Math.round(length ? ctx.sheet!.diag : ctx.sheet!.m2).toLocaleString("en")} ${length ? "m" : "m²"}), so it is not shown as the answer.`
    : `We could not measure this on the drawing yet.`;
  return `
    <div class="hero-value">${big ? `<span class="big guess-val">≈ ${esc(big)}</span><span class="unit">${esc(unit)}</span>` : `<span class="big name">${ctx.scaleKnown || bad ? "Not found" : "Scale unknown"}</span>`}</div>
    <div class="hero-sentence">${esc(sentence)}</div>
    <div class="hero-badges"><span class="conf est guess" title="Nothing on this drawing gave the ${length ? "length" : "area"} yet${big ? ": the number is our usual guess (see 'How we worked it out')" : ""}">${big ? "Rough guess" : "No number yet"}</span>${ctx.scaleKnown ? "" : SCALE_PILL}${tierHtml(a)}</div>
    ${conv ? `<div class="hero-conv"><b>Convention:</b> ${esc(conv.text)}</div>` : ""}`;
}

/** Next to an area / length number whose drawing scale is unknown or only estimated (captain 14:39): one tap opens the
 *  'set the scale' tool (main.ts binds .scale-unknown). */
export const SCALE_PILL = `<button type="button" class="scale-unknown" title="Two clicks on a length you know (a door is about 0.9 m), then type it">Scale unknown — set it</button>`;

/** Swiss floor / apartment totals that include numbered rooms without a name (dxf / pdf_vector: 'included 6 numbered
 *  rooms without a name (21.31 m²: 2.04, 2.07, …); without them the total is 320.13 m²'): a site engineer often reads
 *  those as balconies or shafts (Ziying a02, S0 D30), so the other total is shown next to ours, not hidden in the trace. */
function unnamedAlt(a: Answer): string {
  const t = (a.assumptions ?? []).find((x) => /without a name[^;]*;\s*without them the total is/i.test(x));
  const m = t?.match(/included (\d+) numbered rooms? without a name \(([\d.,]+) m²(?::\s*([^)]*))?\);\s*without them the total is ([\d.,]+) m²/i);
  if (!m || typeof a.value !== "number") return "";
  const n = Number(m[1]);
  const rooms = (m[3] ?? "").split(/,\s*/).filter(Boolean);
  return `<div class="hero-alt"><span class="ha-val">${esc(Number(m[4]).toLocaleString("en", { maximumFractionDigits: 2 }))} m²</span>
    <span class="ha-txt">without the ${n} room${n > 1 ? "s" : ""} that ha${n > 1 ? "ve" : "s"} a number but no name${rooms.length ? ` (${esc(rooms.slice(0, 6).join(", "))}${n > 6 ? ", …" : ""})` : ""}:
    often balconies, shafts or storage. We count them (${esc(m[2])} m²); check them on the plan.</span></div>`;
}

/** The pipeline's second opinions on an unsure first answer (G1, on by default from 13:14): Gemini 3.1 Pro plans
 *  another way; NVIDIA Nemotron 3.5 Super VL looks at the drawing and agrees or not. Shown as pills: both are part of
 *  how the answer was made (and of its cost tier). */
function secondOpinions(a: Answer): string {
  const t = (a.trace ?? []).map(String);
  const out: string[] = [];
  // 'escalation (confidence 0.30 < 0.5): Gemini 3.1 Pro planned …' (an unavailable planner or no answer: no pill)
  const gem = t.find((l) => /Gemini 3\.1 Pro planned\b/.test(l) && !/unavailable|reached no answer|needs \$GEMINI/i.test(l));
  if (gem) {
    const kept = /not better supported|answer kept/i.test(gem);
    const same = /same value/i.test(gem);
    const word = /replaces/i.test(gem) || /gemini_agent/.test(String(a.solver ?? "")) ? "Answer worked out by Gemini" : same ? "Gemini found the same" : kept ? "Gemini checked it" : "Gemini double-checked";
    out.push(`<span class="op gem${same ? " ok" : ""}" title="${esc(gem.slice(0, 300))}">✦ ${word}</span>`);
  }
  // 'Second opinion (NVIDIA Nemotron 3.5 Super VL): agrees …' or 'verifier Nemotron 3.5 Super VL: agree — …'
  const nem = t.find((l) => /Second opinion \(NVIDIA Nemotron|verifier Nemotron\b/.test(l) && !/unavailable|failed|error/i.test(l));
  if (nem) {
    const raw = nem.match(/(?:\)|Nemotron[^:]*):\s*(agrees?|disagrees?|is unsure|unsure)\b/i)?.[1]?.toLowerCase() ?? "";
    const v = raw.startsWith("dis") ? "disagrees" : raw.startsWith("agree") ? "agrees" : raw ? "is unsure" : "";
    const cls = v === "agrees" ? "ok" : v === "disagrees" ? "warn" : "";
    out.push(`<span class="op nem ${cls}" title="${esc(nem.slice(0, 300))}">✦ Nemotron ${esc(v || "looked")}</span>`);
  }
  return out.join("");
}

/** Stoa judges cost on three tiers, not in dollars: cheap = rules on the drawing's own geometry and text (CPU only),
 *  in-between = a local AI model on our GPU, expensive = a hosted AI model (Gemini, Nemotron). From Answer.cost. */
export function costTier(a: Answer): { tier: "cheap" | "mid" | "high"; word: string; why: string } {
  const c = (a.cost ?? {}) as Record<string, number>;
  const n = (k: string) => Number(c[k] ?? 0) || 0;
  if (n("api_calls") > 0 || n("api_tokens_in") > 0 || n("gemini_usd") > 0)
    return { tier: "high", word: "Expensive", why: `a hosted AI model was called (${n("api_calls") || 1} call${n("api_calls") > 1 ? "s" : ""})` };
  // a vision call or a long prompt is real GPU work; a one-line text check (~150 tokens, ~0.01 GPU s) is not
  if (n("gpu_seconds") > 0.25 || n("llm_tokens_in") > 600)
    return { tier: "mid", word: "In-between", why: `a local AI model on our own GPU read the drawing${n("llm_calls") ? ` (${n("llm_calls")} call${n("llm_calls") > 1 ? "s" : ""})` : ""}` };
  return { tier: "cheap", word: "Cheap", why: `rules on the drawing's own lines and text, on a normal CPU${n("llm_calls") ? ` (plus a one-line check by a small local language model, ${n("llm_tokens_in")} tokens)` : ", no AI model"}` };
}
function tierHtml(a: Answer): string {
  const t = costTier(a);
  const dots = t.tier === "cheap" ? 1 : t.tier === "mid" ? 2 : 3;
  // a replay from the answer cache did no work this time (QA 19:11: 'Cost: expensive' on a 0.8 s cached replay): the tier
  // is what the first answer cost, said as such
  const again = !!a.cached && t.tier !== "cheap";
  return `<span class="tier t-${t.tier}" title="Cost tier: ${esc(t.why)}${again ? ". This time it came from our answer cache: no new model calls" : ""}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= dots ? "on" : ""}"></i>`).join("")}</span>Cost: <b>${t.word.toLowerCase()}</b>${again ? " <small>(first time; now cached)</small>" : ""}</span>`;
}

/** Evidence placed by image recognition (a detector box, a segmented region, a door swing found in pixels, an element
 *  boxed from the model's footprint because the plan does not draw it) is an APPROXIMATE place; CAD / IFC geometry,
 *  schedule rows and text read off the drawing are exact. The customer view draws the approximate ones dashed. */
const APPROX_NOTE = /segmentation|detector|room graph|door[- ]?swing|flood|estimat|approx|projected: not drawn|~\s*\d|\bregion R\d/i;
export function approxEvidence(a: Answer): Set<number> {
  const bySolver = /raster_cc5k|vlm/i.test(a.solver ?? "");
  const out = new Set<number>();
  (a.evidence ?? []).forEach((e, i) => {
    if (!(e.bbox || e.ifc_guid)) return;
    if (e.exact === false) { out.add(i); return; } // D25: the solver says so
    if (e.exact === true) return;
    const note = e.note ?? "";
    if (/^\s*label reader:/i.test(note)) return; // printed text: its place is exact
    if (APPROX_NOTE.test(note) || (bySolver && !!e.bbox)) out.add(i);
  });
  return out;
}

/** Number spread-out items for a count (every item is equivalent: 46 doors), the first ones otherwise (a ranking). */
export const spreadOf = (a: Answer) => a.intent?.task === "count";

/** The evidence items that get a number (1…5): the first ones with a place, one per element. With `spread` (a count),
 *  the first item and then the ones farthest from those already numbered, so the numbers cover the drawing instead of
 *  piling up on five neighbours. */
export function numbered(ev: Answer["evidence"], max = 5, spread = false): number[] {
  const all = numberedAll(ev, spread ? Infinity : max);
  if (!spread || all.length <= max) return all.slice(0, max);
  const f0 = ev[all[0]].file;
  const ctr = all.map((i) => { const b = ev[i].bbox; return b && b.length >= 4 && ev[i].file === f0 ? [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2] : null; });
  if (ctr.some((c) => !c)) return all.slice(0, max);
  const chosen = [0];
  const dmin = ctr.map((c) => Math.hypot(c![0] - ctr[0]![0], c![1] - ctr[0]![1]));
  while (chosen.length < max) {
    let best = -1;
    dmin.forEach((d, k) => { if (!chosen.includes(k) && (best < 0 || d > dmin[best])) best = k; });
    if (best < 0) break;
    chosen.push(best);
    ctr.forEach((c, k) => { dmin[k] = Math.min(dmin[k], Math.hypot(c![0] - ctr[best]![0], c![1] - ctr[best]![1])); });
  }
  return chosen.map((k) => all[k]);
}

function numberedAll(ev: Answer["evidence"], max: number): number[] {
  const out: number[] = [];
  const seen = new Set<string>();
  ev.forEach((e, i) => {
    if (out.length >= max || !(e.bbox || e.ifc_guid)) return;
    const k = e.ifc_guid ?? `${e.file}|${(e.bbox ?? []).join(",")}`;
    if (seen.has(k)) return;
    // a 2D box and its element's GlobalId are the same thing: number the box (it is what the plan shows)
    if (e.ifc_guid && !e.bbox && ev.some((x) => x.bbox && x.ifc_guid === e.ifc_guid)) return;
    seen.add(k);
    out.push(i);
  });
  return out;
}

// ---------- differences: drawing vs 3D model, every floor of the project ----------
export interface Difference {
  sheet: string;
  label: string;
  storey: string;
  kind: string;
  drawn: number;
  model: number;
  explained: number;
}
export async function projectDifferences(p: Project, kgOf: (path: string) => Promise<Kg | null>): Promise<Difference[]> {
  const sheets = p.sheets.filter((s) => /\.pdf$/i.test(s.path));
  const kgs = await Promise.all(sheets.map((s) => kgOf(s.path).catch(() => null)));
  const out: Difference[] = [];
  kgs.forEach((k, i) => {
    const st = (k as unknown as { storey?: { name?: string; cut?: Record<string, { model: number; drawn: number; model_cut?: number }> } } | null)?.storey;
    for (const [kind, c] of Object.entries(st?.cut ?? {})) {
      if (c.model === c.drawn) continue;
      out.push({ sheet: sheets[i].path, label: sheets[i].label, storey: st?.name ?? "", kind, drawn: c.drawn, model: c.model,
        explained: c.model_cut === c.drawn ? c.model - c.drawn : 0 });
    }
  });
  return out;
}
export function diffsHtml(d: Difference[], p: Project): string {
  if (!d.length) return "";
  const allExplained = d.every((x) => x.explained === x.model - x.drawn);
  return `<div class="diffs-head"><b>Differences found</b><span>${d.length}</span></div>
    <p class="diffs-sub">Drawings vs 3D model, checked floor by floor for ${esc(p.name)}.${allExplained ? " Each one is explained: those elements sit above or below the height where the floor plan is cut (normal for floor plans)." : ""}</p>
    ${d.map((x, i) => `<div class="diff"><div><b>${esc(x.label)}:</b> the drawing shows ${x.drawn} ${esc(x.kind)}, the 3D model has ${x.model}${x.explained ? ` <small>(${x.explained} above/below the cut)</small>` : ""}</div>
      <button type="button" class="diff-go" data-diff="${i}">Show me</button></div>`).join("")}`;
}
/** The question behind "Show me" (W5 cycle 8: windows floor 2 → 7, walls floor 1 / 5 → 4, all with the elements marked). */
export const diffQuestion = (x: Difference) => `How many ${x.kind} on ${floorName(x.storey)} are in the 3D model but not drawn on this plan?`;

export { projectOf, PROJECTS };

/** The ingest recognition, shown as a quiet chip above the answer (captain 13:33): what kind of drawing we took the
 *  file for, its floor, its scale and the page read, so a visitor sees at once when we misread it and can correct it
 *  ("Change" re-asks with their choice). G1's ingest step will return it in the answer; it is read here as
 *  a.recognition / a.recognised / a.ingest = {kind | label, storey, scale, page, confidence}. Until then it is worked
 *  out from which reader answered and from the trace. */
export interface Recognition { kind: string; storey?: string; scale?: string; page?: string; conf?: number; guessed?: boolean }

const KIND_WORDS: Record<string, string> = {
  // G1's kinds (stoa/router/recognize.py LABELS), in customer words (no dataset names)
  bim_elevation: "Elevation from a 3D model", bim_section: "Section from a 3D model", swiss_plan: "CAD floor plan",
  cad_sheet: "Architectural drawing sheet", schedule_sheet: "Schedule sheet", cubicasa_plan: "Floor plan image", // a raster plan in any style (Stoa's school sheet was called an apartment)
  plan_render: "Floor plan image", ifc_model: "3D building model (IFC)", dxf_plan: "CAD floor plan (DXF)",
  svg_bim: "Floor plan from a 3D model (SVG)", pdf_scan: "Scanned drawing (PDF)",
  bim_plan: "Floor plan from a 3D model", ifc_plan: "Floor plan from a 3D model", ifcconvert: "Floor plan from a 3D model",
  cad_plan: "CAD floor plan", swiss: "CAD floor plan", raster_plan: "Floor plan image", cubicasa: "Floor plan image",
  schedule: "Schedule sheet", scan: "Scanned or historical drawing", historical: "Historical drawing (scan)", photo: "Photo of a printed drawing",
  model: "3D building model (IFC)", ifc: "3D building model (IFC)", other: "Drawing",
};
/** the reader that answered → what that says about the file */
const KIND_BY_SOLVER: Record<string, string> = {
  plan_rooms: "Floor plan from a 3D model", ifcplan_pdf: "Floor plan from a 3D model", svg_ifc: "Floor plan from a 3D model (SVG)",
  pdf_vector: "CAD floor plan", dxf: "CAD floor plan with its DXF file", raster: "Floor plan image",
  raster_cc5k: "Floor plan image", vlm_lora: "Floor plan image", schedule: "Schedule sheet",
  pdf_text: "Drawing with readable text", vlm_direct: "Drawing read by an AI vision model",
  area_tags: "Floor plan with room area tags", // G1: Revit/CAD room tags 'U123 - 680 SF'
};

export function recognitionOf(a: Answer, file: string, opts: { sheet?: string; scale?: string } = {}): Recognition | null {
  const t = (a.trace ?? []).map(String);
  const all = [...t, ...(a.assumptions ?? [])].join("\n");
  // G1 (night-G1 40e975d): the first trace line is 'recognised: {"kind","label","confidence","scale","scale_source",
  // "storey","pages","page"?,"reader"}'; a field on the answer is read too, should one be added later
  const fromTrace = (() => {
    const l = t.find((x) => /^\s*recognised:\s*\{/.test(x));
    if (!l) return undefined;
    try {
      return JSON.parse(l.replace(/^\s*recognised:\s*/, "")) as Record<string, unknown>;
    } catch {
      return undefined;
    }
  })();
  // CONTRACT D31: Answer.recognition first, the trace line as the fallback
  const g = ((a as unknown as Record<string, unknown>).recognition ?? fromTrace ?? (a as unknown as Record<string, unknown>).recognised
    ?? (a as unknown as Record<string, unknown>).ingest) as Record<string, unknown> | undefined;
  const page = (() => {
    const m = all.match(/(?:\((\d+) pages?\))?[^\n]{0,40}?answered from page (\d+)(?: of (\d+))?/i);
    return m ? `page ${m[2]}${m[3] || m[1] ? ` of ${m[3] || m[1]}` : ""}` : undefined;
  })();
  const cap = (x: string) => x.replace(/^\w/, (c) => c.toUpperCase());
  // the floor asked about (a visitor's floor choice re-asks "… (on storey 'X')") beats the sheet's own
  const about = all.match(/the question is about storey '([^']+)'/i)?.[1];
  const storey = about ? cap(floorName(about)) : opts.sheet || (() => {
    const m = all.match(/storey from the [^\n]*?=\s*storey '([^']+)'/i) ?? all.match(/\b(?:in|on) storey '([^']+)'/i);
    return m ? cap(floorName(m[1])) : undefined;
  })();
  if (g && typeof g === "object" && (g.kind || g.label) && g.kind !== "other") {
    const c = Number(g.confidence ?? g.conf);
    const pages = Number(g.pages ?? 1);
    const pg = g.page != null && pages > 1 ? `page ${Number(g.page) + 1} of ${pages}` : page;
    const sc = g.scale ? `${String(g.scale).replace(/\s+/g, "")}${/estimat|guess|assum/i.test(String(g.scale_source ?? "")) ? " (estimated)" : ""}` : undefined;
    return { kind: cap(String(KIND_WORDS[String(g.kind)] ?? g.label ?? g.kind)), storey: about ? storey : g.storey ? cap(floorName(String(g.storey))) : storey,
      scale: opts.scale ?? sc, page: pg, conf: Number.isFinite(c) ? c : undefined };
  }
  let s = String(a.solver ?? "").split("/").pop() ?? "";
  // the general agent relays another reader's answer ("→ ask(…): … by pipeline/raster")
  if (/^(agent|gemini_agent)$/.test(s)) s = t.join("\n").match(/\bby pipeline\/(\w+)/)?.[1] ?? s;
  let kind = KIND_BY_SOLVER[s];
  if (s === "ifc") kind = /\.ifc$/i.test(file) ? "3D building model (IFC)" : "Floor plan linked to its 3D model";
  if (s === "raster") kind = "Floor plan image or scan";
  // where a known collection's file lives says what it is when the reader does not (a text / vision answer)
  const generic = !kind || /^(raster|vlm_direct|pdf_text)$/.test(s);
  if (/(^|\/)cubicasa5k\//.test(file) && generic && s !== "prior") kind = "Apartment floor plan (image)";
  if (/^data\/(habs|finna|europeana|wikimedia)\//.test(file) && generic && s !== "prior") kind = "Historical drawing (scan)";
  if (!kind) return null; // the prior, the agent on its own: the answer says nothing about the file
  return { kind, storey, scale: opts.scale, page, guessed: true };
}

/** Readers a visitor can pick in "Change" (the file's type decides which make sense). "" = our own choice. */
export function kindChoices(file: string): { solver: string; label: string; short: string }[] {
  const ext = file.toLowerCase().match(/\.\w+$/)?.[0] ?? "";
  const auto = { solver: "", label: "Automatic (our choice)", short: "" };
  const vlm = { solver: "vlm_direct", label: "Something else: an AI vision model reads it", short: "Drawing read by an AI vision model" };
  if (ext === ".pdf") return [auto, { solver: "plan_rooms", label: "Floor plan from a 3D model (Revit, ArchiCAD…)", short: "Floor plan from a 3D model" },
    { solver: "pdf_vector", label: "CAD floor plan", short: "CAD floor plan" }, { solver: "schedule", label: "Schedule or table", short: "Schedule sheet" }, vlm];
  if (/^\.(png|jpe?g)$/.test(ext)) return [auto, { solver: "raster", label: "Floor plan image or scan", short: "Floor plan image" }, vlm];
  if (ext === ".svg") return [auto, { solver: "svg_ifc", label: "Floor plan from a 3D model (SVG)", short: "Floor plan from a 3D model (SVG)" }];
  return [];
}

export function recognitionHtml(r: Recognition, o: { chosen?: string; canChange: boolean }): string {
  const bits = [r.storey, r.scale, r.page].filter(Boolean) as string[];
  const conf = r.conf != null ? ` title="${esc(`recognised with ${Math.round(r.conf * 100)} % confidence`)}"` : r.guessed ? ` title="worked out from the reader that answered"` : "";
  return `<div class="recog"${conf}><span class="rg-lbl">${o.chosen ? "Read as" : "Recognised as"}</span>
    <span class="rg-what"><b>${esc(o.chosen || r.kind)}</b>${bits.map((b) => ` · ${esc(b)}`).join("")}${o.chosen ? ` <small>(your choice)</small>` : ""}</span>
    ${o.canChange ? `<button type="button" class="rg-change" aria-haspopup="true" aria-expanded="false">Change</button>` : ""}</div>`;
}

/** The "Change" menu: the kind of drawing (re-asks with that reader), the scale (paper scales for a PDF; two clicks
 *  for anything), the floor (when a 3D model is linked: re-asks about that floor). */
export function recognitionMenuHtml(o: { kinds: { solver: string; label: string }[]; current: string; scales: number[];
  scaleNow?: string; userScale: boolean; floors: string[]; floorNow?: string }): string {
  const kinds = o.kinds.length ? `<div class="rm-group"><div class="rm-title">What kind of drawing is it?</div>${o.kinds.map((k) =>
    `<button type="button" class="rm-opt${k.solver === o.current ? " on" : ""}" data-kind="${esc(k.solver)}">${esc(k.label)}</button>`).join("")}</div>` : "";
  const scales = `<div class="rm-group"><div class="rm-title">Scale${o.scaleNow ? ` <small>now ${esc(o.scaleNow)}</small>` : ""}</div><div class="rm-row">${
    o.scales.map((n) => `<button type="button" class="rm-pill${o.scaleNow === `1:${n}` && o.userScale ? " on" : ""}" data-scale="${n}">1:${n}</button>`).join("")}</div>
    <button type="button" class="rm-opt" data-scale="measure">Measure a length I know…</button>${o.userScale ? `<button type="button" class="rm-opt" data-scale="reset">Use the drawing's own scale</button>` : ""}</div>`;
  const floors = o.floors.length ? `<div class="rm-group"><div class="rm-title">Which floor is it?</div><div class="rm-row">${o.floors.map((f) =>
    `<button type="button" class="rm-pill${f === o.floorNow || floorName(f).replace(/^\w/, (c) => c.toUpperCase()) === o.floorNow ? " on" : ""}" data-floor-name="${esc(f)}">${esc(floorName(f).replace(/^\w/, (c) => c.toUpperCase()))}</button>`).join("")}</div></div>` : "";
  return `<div class="rg-menu" role="dialog" aria-label="Change how the drawing is read">${kinds}${scales}${floors}</div>`;
}

/** A ranking answer ('largest room', 'top 5 …', 'which … the most', 'smallest …'): its places in rank order (the
 *  first ≤5 numbered ones), else null. Counts ('46 doors') are not rankings: every item is equivalent. Captain 13:50:
 *  rank is shown by colour intensity on one blue ramp (1st darkest), in 2D, in 3D and in the list. */
export function rankOf(a: Answer, question: string): number[] | null {
  if (spreadOf(a)) return null;
  if (!/\b(biggest|largest|smallest|tallest|highest|lowest|longest|shortest|widest|most|least|top\s*\d+|rank(ed|ing)?|größte|kleinste|suurin|pienin)\b/i.test(question)) return null;
  const idx = numbered(a.evidence, 5);
  return idx.length > 1 ? idx : null;
}
export const RANK_WORDS = ["1st", "2nd", "3rd", "4th", "5th"];
export function rankLegendHtml(n: number): string {
  return `<div class="rank-legend" aria-label="Darker = higher rank"><span>${RANK_WORDS[0]}</span>${Array.from({ length: n }, (_, k) => `<i class="rk${k + 1}"></i>`).join("")}<span>${RANK_WORDS[n - 1]}</span></div>`;
}
