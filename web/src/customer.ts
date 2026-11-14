/**
 * Customer view (default): for site engineers, project managers and estimators, not for the jury's engineers.
 * Projects instead of files, one big question box, the answer as the hero in plain words, a short numbered list of
 * where it comes from, the time it saves, and the drawing-vs-model differences of the project. Everything technical
 * (solvers, cost in tokens, trace, benchmark) stays one click away: "How we worked it out" and the Expert view.
 */
import { agentAsks, crossChecks, fmtValue, setAnswer, unitLabel, type SetRow } from "./card";
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
export function landingHtml(): string {
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
      <p class="lh-sub">Answers with the exact place they come from.</p>
    </header>
    <section class="dropzone" id="landDrop" role="button" tabindex="0" aria-label="Upload your own drawing">
      <svg class="dz-ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M12 30v8h24v-8" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M24 31V9M15 18l9-9 9 9" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div class="dz-title">Drop your drawing here</div>
      <div class="dz-sub">A floor plan or sheet as PDF, a scan or a phone photo of a print, a 3D building model or a CAD drawing <span class="dz-fmt">(PDF, JPG, PNG, HEIC, IFC, DWG, DXF, SVG)</span></div>
      <span class="dz-btn">Choose files</span>
      <div class="dz-more">Several files at once, like a plan with its 3D model or CAD file, are read together.
        Then ask about areas, lengths or counts in your own words.</div>
      <div class="dz-status" id="landStatus" hidden></div>
    </section>
    <h2>Or explore a sample project.</h2>
    <div class="proj-grid">${PROJECTS.map(card).join("")}
    </div>
    <p class="landing-foot">Your files stay on the demo machine (a temporary folder) and are read by local models. Sample drawings and models are public (credits under each project). Every answer shows where it comes from, so anyone can check it in seconds.</p>
  </div>`;
}
const KIND_LABEL: Record<Project["kind"], string> = {
  building: "Building · drawings + 3D", model: "Building · 3D model", floor: "Floor plan", schedule: "Drawing set", plan: "Floor plan", historic: "Historic drawing",
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
  return { cls: "est", word: "Best estimate", hint: "" };
}

const SIZE_Q = /\b(how (big|large|wide|long|tall|high|deep|far|much (space|floor))|area|size|width|length|distance|perimeter|dimensions?|square (metres|meters|feet)|fläche|breite|länge|grösse|größe|pinta-ala|leveys|pituus|koko|superficie|surface|largeur|longueur)\b|m²|\bm2\b|\bsq ?m\b/i;

/** An area or length question with nothing measured on this drawing: the answer is the dataset's typical value (the
 *  prior: 'How big is the lounge?' → 55.72 m² @0.01, 'How long is the garage?' → 244.8 m @0.00 on a plan with labels
 *  only and no scale, X1's wild exam 14:22) or a 'Not determined …' / unrelated stock sentence. A site engineer
 *  reads such a number as a wrong answer, so the customer view shows no number and offers the measure tool instead;
 *  the guess itself stays under "How we worked it out". Counts keep their rough guess. */
export function needsMeasuring(a: Answer, question: string, file = ""): boolean {
  const task = a.intent?.task;
  if (a.value === null || a.value === undefined || typeof a.value === "boolean") return false;
  if (task === "count" || a.unit === "count" || /^\s*how many\b/i.test(question)) return false;
  if (!(task === "area" || task === "length" || SIZE_Q.test(question))) return false;
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

function headline(a: Answer, ctx: HeroCtx): { big: string; unit: string; sentence: string; text?: string; name?: boolean; next?: string[]; nextLabel?: string } {
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
  const lengthQ = a.intent?.task === "length" || /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|breite|länge|leveys|pituus)\b/i.test(ctx.question);
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
  const noteRoom = ev0.match(/^'?([\p{L}][\p{L}\d .\/-]{1,40}?)'?(?::\s*(?:long side |short side |width |length )?[\d.,]+\s*m|\s+outline\b)/u)?.[1]?.trim();
  const named2 = !room && !apt && !several && noteRoom && roomPhrases(ctx.question).some((ph) => noteRoom.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "").includes(ph))
    ? `the ${noteRoom.toLowerCase()}` : "";
  const what = a.intent?.task === "area" ? (room ? `floor area of ${room}` : apt ? `floor area of ${apt}` : several ? `floor area of ${several}` : named2 ? `floor area of ${named2}` : "floor area")
    : a.intent?.task === "length" ? (named2 ? `length of ${named2}` : "length") : a.intent?.task === "volume" ? "volume" : "";
  return { big: fmtValue(a), unit, sentence: [what, where].filter(Boolean).join(" ") };
}
const plainify = (s: string) => swissRooms(s).replace(/'?(\d+)\.korrus'?/g, (_, n) => `floor ${n}`).replace(/\bstorey\b/g, "floor").replace(/\bIFC model\b/g, "3D model").replace(/\bplan draws\b/g, "the drawing shows")
  .replace(/\bthe plan's\b/g, "the drawing's").trim();
function costSentence(q: string) {
  const m = q.match(/(\d[\d,.]*)\s*(euros?|eur|€|dollars?|usd|\$)/i);
  return m ? `in total, at ${m[1]} ${/^(e|€)/i.test(m[2]) ? "€" : "$"} each` : "in total";
}

/** Which kind of second source agreed, in words. */
/** A cross-check in the answer's trace or notes, in words. ok → '2 independent methods agree' + which two (captain 15:12:
 *  a 0.22 next to two agreeing methods read as 'probably wrong' to Ziying, so the agreement is said plainly). */
function agreement(a: Answer, file: string): { ok: boolean; text: string; detail: string; which?: string } | null {
  const xs = crossChecks(a, agentAsks(a)).filter((x) => !/^skipped\b/i.test(x.text));
  if (!xs.length) {
    // G1's hybrid routing (night-G1 558f6bc): 'two independent readings agree (ours X, Gemini X) ✓' in the trace, or the
    // assumption 'two independent methods agree (…)'
    const lines = [...(a.trace ?? []), ...(a.assumptions ?? [])].map(String);
    const hit = lines.find((l) => /two independent (readings|methods) agree/i.test(l) && !/disagree/i.test(l));
    if (!hit) return null;
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
  if (needsMeasuring(a, ctx.question, ctx.file)) return measureFirstHtml(a, ctx);
  const h = headline(a, ctx);
  const ag = agreement(a, ctx.file);
  let c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  // two methods that agree are not a 'rough guess … not found on the drawing', whatever the calibrated number says
  if (ag?.ok && c.cls.includes("guess")) c = { cls: "est", word: "Best estimate", hint: "" };
  const assumption = c.cls.startsWith("est") ? (a.assumptions ?? [])[0] : "";
  const wall = a.cost?.wall_seconds && a.cost.wall_seconds > 0 ? a.cost.wall_seconds : ctx.roundTripMs / 1000;
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
  const imperial = iq?.metric && iq.label !== "mm" && iq.label !== "cm" && !set ? iq.metric.replace(/^(\d+)(\.\d)/, (_m, x: string, y: string) => `${Number(x).toLocaleString("en")}${y}`) : "";
  const imperialUnit = iq?.label === "SF" ? "square feet" : iq?.label === "LF" ? "linear feet" : "feet";
  const givenTotal = set?.total ? `<div class="hs-total"><span>Total</span><b>${esc(set.total)}${quantityOf(set.total)?.metric ? `<small> · ${esc(quantityOf(set.total)!.metric!)}</small>` : ""}</b></div>` : "";
  const setRows = set ? `<div class="hero-set">${set.head ? `<div class="hs-head">${esc(plainify(set.head))}</div>` : ""}${set.rows.slice(0, shownRows).map((r, k) =>
    `<button type="button" class="hs-row"${r.ev >= 0 ? ` data-ev="${r.ev}"` : ""}><span class="hs-name"><i class="hs-sw" style="background:${SWATCH[k % SWATCH.length]}"></i>${esc(r.name)}</span><b>${esc(r.detail)}${qty[k]?.metric ? `<small> · ${esc(qty[k]!.metric!)}</small>` : ""}</b></button>`).join("")}${set.rows.length > shownRows ? `<div class="hs-more">+ ${set.rows.length - shownRows} more</div>` : ""}${
    givenTotal ? givenTotal : total !== null ? `<div class="hs-total"><span>Total <small>(sum of the rows)</small></span><b>${esc(fmtQty(total, qty[0]!.label))}${qty[0]!.toMetric ? `<small> · ${esc(fmtQty(total * qty[0]!.toMetric, qty[0]!.kind === "area" ? "m²" : "m"))}</small>` : ""}</b></div>`
    : totalCount !== null ? `<div class="hs-total"><span>Total</span><b>${totalCount.toLocaleString("en")}</b></div>` : ""}</div>` : "";
  return `
    ${h.big ? `<div class="hero-value"><span class="big${h.name ? " name" : ""}${!h.name && h.big.length > 8 ? " long" : ""}">${esc(h.big)}</span>${h.unit ? `<span class="unit">${esc(h.unit)}</span>` : ""}</div>` : ""}
    ${imperial ? `<div class="hero-metric">= ${esc(imperial)} <small>(drawing in ${esc(imperialUnit)})</small></div>` : ""}
    ${h.sentence ? `<div class="hero-sentence">${esc(h.sentence)}</div>` : ""}
    ${h.next?.length ? `<div class="hero-next">${h.nextLabel === "" ? "" : "<span>Next:</span> "}${h.next.map(esc).join(" &nbsp;·&nbsp; ")}</div>` : ""}
    ${text}${setRows}
    <div class="hero-badges">
      <span class="conf ${c.cls}" title="Our calibrated confidence: ${Math.round((a.confidence ?? 0) * 100)} %">${esc(c.word)}</span>${c.hint ? `<span class="conf-hint">${esc(c.hint)}</span>` : ""}
      ${ag ? `<span class="agree ${ag.ok ? "ok" : "bad"}" title="${esc(ag.detail)}">${ag.ok ? "" : "⚠ "}${esc(ag.text)}${ag.which ? `<small> · ${esc(ag.which)}</small>` : ""}</span>` : ""}
      ${tierHtml(a)}
      ${secondOpinions(a)}
    </div>
    ${assumption ? `<div class="hero-assume">Assumed: ${esc(assumption)}</div>` : ""}
    ${unnamedAlt(a)}
    <div class="hero-value-strip" title="Manual time is our rough estimate for this kind of question (${esc(mt.work)}), not a measurement">
      <span>Answered in <b>${wall < 1 ? `${Math.max(0.1, wall).toFixed(1)} s` : `${wall.toFixed(1)} s`}</b></span>
      <span>about <b>${mt.minutes} min</b> by hand <small>(estimate)</small></span>
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

/** The hero when nothing was measured (needsMeasuring). Never an empty answer (captain 14:39): the reader's number as
 *  a rough guess ('a typical size, not measured on this drawing'), and main.ts measureFirst replaces it with the room
 *  traced at the drawing's scale, or at an estimated one (door swings, else the usual 1:100), with 'Scale unknown —
 *  set it' next to the number until the scale is known. */
function measureFirstHtml(a: Answer, ctx: HeroCtx): string {
  const length = a.intent?.task === "length" || (a.intent?.task !== "area" && /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|breite|länge|leveys|pituus|largeur|longueur)\b/i.test(ctx.question));
  const v = typeof a.value === "number" && a.value > 0 ? a.value : null;
  const unit = unitLabel(a.unit) || (length ? "m" : "m²");
  const big = v !== null ? v.toLocaleString("en", { maximumFractionDigits: v >= 100 ? 0 : v >= 10 ? 1 : 2 }) : "";
  const sentence = big
    ? readerGuess(a) === "typical" ? `a typical ${length ? "length" : "size"} for this — not measured on this drawing yet`
      : `read off a picture of the drawing without marking where — not measured yet`
    : `We could not measure this on the drawing yet.`;
  return `
    <div class="hero-value">${big ? `<span class="big guess-val">≈ ${esc(big)}</span><span class="unit">${esc(unit)}</span>` : `<span class="big name">${ctx.scaleKnown ? "Not found" : "Scale unknown"}</span>`}</div>
    <div class="hero-sentence">${esc(sentence)}</div>
    <div class="hero-badges"><span class="conf est guess" title="Nothing on this drawing gave the ${length ? "length" : "area"} yet: the number is our usual guess (see 'How we worked it out')">Rough guess</span>${ctx.scaleKnown ? "" : SCALE_PILL}${tierHtml(a)}</div>`;
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
  return `<span class="tier t-${t.tier}" title="Cost tier: ${esc(t.why)}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= dots ? "on" : ""}"></i>`).join("")}</span>Cost: <b>${t.word.toLowerCase()}</b></span>`;
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
  cad_sheet: "Architectural drawing sheet", schedule_sheet: "Schedule sheet", cubicasa_plan: "Apartment floor plan (image)",
  plan_render: "Floor plan image", ifc_model: "3D building model (IFC)", dxf_plan: "CAD floor plan (DXF)",
  svg_bim: "Floor plan from a 3D model (SVG)", pdf_scan: "Scanned drawing (PDF)",
  bim_plan: "Floor plan from a 3D model", ifc_plan: "Floor plan from a 3D model", ifcconvert: "Floor plan from a 3D model",
  cad_plan: "CAD floor plan", swiss: "CAD floor plan", raster_plan: "Floor plan image", cubicasa: "Apartment floor plan (image)",
  schedule: "Schedule sheet", scan: "Scanned or historical drawing", historical: "Historical drawing (scan)", photo: "Photo of a printed drawing",
  model: "3D building model (IFC)", ifc: "3D building model (IFC)", other: "Drawing",
};
/** the reader that answered → what that says about the file */
const KIND_BY_SOLVER: Record<string, string> = {
  plan_rooms: "Floor plan from a 3D model", ifcplan_pdf: "Floor plan from a 3D model", svg_ifc: "Floor plan from a 3D model (SVG)",
  pdf_vector: "CAD floor plan", dxf: "CAD floor plan with its DXF file", raster: "Floor plan image",
  raster_cc5k: "Apartment floor plan (image)", vlm_lora: "Apartment floor plan (image)", schedule: "Schedule sheet",
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
