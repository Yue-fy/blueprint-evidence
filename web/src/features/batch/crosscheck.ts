// F4 batch Q&A: a whole-sheet question (total floor area, how many rooms / doors / windows, the largest room) is checked
// against this sheet's own takeoff (F2's, summarised by W5's GET /api/glance: it never starts one), so a list never says
// 9,965 m² next to a 'This floor at a glance' card that says 1,717 m² without telling (UX M19). No answer logic here:
// the row keeps the reader's answer; the takeoff's number is shown beside it ('use it' makes it the row's number).
import type { Answer } from "../../types";

export type CheckKind = "floor-area" | "rooms" | "doors" | "windows" | "largest-room";

export interface GlanceRow { section?: string; quantity?: string; item?: string; value?: unknown; unit?: string | null; review?: unknown; status?: string; i?: number }
export interface Glance {
  ok: boolean;
  why?: string;
  rooms?: number;
  area_m2?: number;
  doors?: { value?: number | null; leaves?: number | null; sets?: boolean } | null;
  windows?: { value?: number | null } | null;
  rows?: GlanceRow[];
  /** the card's room groups ('Teaching', 'Circulation', …) with the indices of their rows in `rows` */
  types?: { type?: string; n?: number; m2?: number; rows?: number[] }[];
}
// corridors, lobbies, stairs: spaces, not 'the largest room' (as G1's pipeline/takeoff answer and glance.py's group)
const CIRCULATION = new RegExp(String.raw`(?<!\p{L})(?:corridor|lobby|stair\p{L}*|circulation|entrance|foyer|vestibule|lift|hallway|passage|landing|walkway|korr?idor\p{L}*|flur|diele|vorplatz|eingang\p{L}*|treppenhaus|aufzug|esik|eeskoda|fuajee\p{L}*|trepp\p{L}*|trepikoda|porras\p{L}*|kaytava\p{L}*|käytävä\p{L}*|eteinen|tuulikaappi)(?!\p{L})`, "iu");
// not rooms by name, the same words as G1's pipeline/takeoff answer (night-G1 125d5a4c _NOT_A_ROOM_NAME: the IFC duplex's
// 'Roof R301 (Roof)' 135.15 m² was its 'largest room')
const NOT_A_ROOM = /(?<![\p{L}])(?:roofs?|attic|void|shaft|riser|terrace|balcon(?:y|ies)|canopy|plenum|crawl\s*space|patio|porch|carport)(?![\p{L}])/iu;
/** The measured room rows (status ok, not sent to review), in the card's room groups when it has them. */
export function measuredRoomRows(g: Glance): GlanceRow[] {
  const groups = (g.types ?? []).filter((t) => Array.isArray(t.rows));
  const inRooms = groups.length ? new Set(groups.flatMap((t) => t.rows!)) : null;
  return (g.rows ?? []).filter((r, k) => r.section === "room" && r.quantity === "area" && !r.review && typeof r.value === "number"
    && (r.status === undefined || r.status === "ok") && (!inRooms || inRooms.has(typeof r.i === "number" ? r.i : k)));
}
/** The rows that count as rooms for 'the largest room': measured (status ok), not sent to review, in one of the card's
 *  room groups other than Circulation (a sheet's corridor is often its biggest outline: DfE L01 Corridor 78.52 vs the
 *  D&T Studio 72.06; Esplanades KORIDOR on every floor), never an estimate (DfE L01 'Stair (Roof)' 55.72 'estimate') or a
 *  label the card leaves out of its rooms (the scan's 'Encloued' wall, 112.57). Without groups: the same by name. */
export function largestRoomRows(g: Glance): GlanceRow[] {
  const rows = g.rows ?? [];
  const groups = (g.types ?? []).filter((t) => Array.isArray(t.rows));
  const inRooms = groups.length ? new Set(groups.filter((t) => !/^circulation$/i.test(String(t.type ?? "").trim())).flatMap((t) => t.rows!)) : null;
  return rows.filter((r, k) => r.section === "room" && r.quantity === "area" && !r.review && typeof r.value === "number"
    && (r.status === undefined || r.status === "ok")
    && (inRooms ? inRooms.has(typeof r.i === "number" ? r.i : k) : !CIRCULATION.test(String(r.item ?? "")))
    && !NOT_A_ROOM.test(String(r.item ?? "")));
}

// words around the core question that do not change what is asked: 'on this plan', 'are there', 'in total'
const FILLER = new RegExp(String.raw`\b(?:please|in total|altogether|are there|are shown|are drawn|there are|is there|` +
  String.raw`(?:does|do) (?:this|the) (?:floor ?plan|plan|drawing|sheet|floor|level) (?:have|show|contain)|` +
  String.raw`(?:in|on|of|for) (?:this|the) (?:whole )?(?:floor ?plan|plan|drawing|sheet|floor|level|storey))\b`, "g");

const CORES: [CheckKind, RegExp][] = [
  ["floor-area", /^(?:(?:what(?: is|'s)|give me|how (?:large|big) is) )?(?:the )?(?:(?:total|overall|whole) )?(?:net )?floor area$/],
  ["floor-area", /^(?:what(?: is|'s) )?(?:the )?(?:total|overall) (?:net )?area$/],
  ["floor-area", /^how (?:large|big) is (?:this|the) (?:floor|plan|level|storey)(?: in square met(?:re|er)s)?$/],
  ["rooms", /^(?:how many|number of|count the) (?:rooms|spaces)$/],
  ["doors", /^(?:how many|number of|count the) doors$/],
  ["windows", /^(?:how many|number of|count the) windows$/],
  ["largest-room", /^(?:(?:what(?: is|'s)|how (?:large|big) is) )?(?:the )?(?:(?:floor )?area of the |size of the )?(?:largest|biggest) room(?:'s area| area)?$/],
  ["largest-room", /^which room is (?:the )?(?:largest|biggest)$/],
];
// Finnish whole-sheet wordings ('Kuinka monta ovea koko pohjapiirustuksessa on?', 'Mikä on tämän pohjapiirustuksen
// kokonaispinta-ala?', 'Mikä huone on suurin?'); 'kerrosala' / 'kokonaisala' / 'bruttoala' are gross: never checked
const FI_WHERE = String.raw`(?: (?:koko |tässä |tällä )?(?:piirustuksessa|kerroksessa|pohjassa|tasossa|tasolla|pohjapiirustuksessa|pohjapiirroksessa|pohjakuvassa|kuvassa))?`;
const FI_OF = String.raw`(?:(?:tämän|koko) )?(?:pohjapiirustuksen|pohjapiirroksen|piirustuksen|kerroksen|tason|pohjan|pohjakuvan)`;
const FI_COUNT = String.raw`(?:kuinka monta|montako)`;
const fiCount = (n: string, gen: string) => [
  new RegExp(String.raw`^${FI_COUNT} ${n}(?: on)?${FI_WHERE}(?: on)?(?: yhteensä)?(?: on)?$`),
  new RegExp(String.raw`^(?:mikä on |paljonko on )?(?:${FI_OF} )?${gen} (?:luku)?määrä${FI_WHERE}(?: yhteensä)?$`),
];
const FI: [CheckKind, RegExp][] = [
  ...fiCount("(?:ovea|ovia)", "ovien").map((re): [CheckKind, RegExp] => ["doors", re]),
  ...fiCount("(?:ikkunaa|ikkunoita)", "ikkunoiden").map((re): [CheckKind, RegExp] => ["windows", re]),
  ...fiCount("(?:huonetta|tilaa)", "huoneiden").map((re): [CheckKind, RegExp] => ["rooms", re]),
  // the area needs a word that makes it the whole sheet's ('pinta-ala' alone is not)
  ["floor-area", new RegExp(String.raw`^(?:mikä on |kuinka suuri on |paljonko on )?(?:${FI_OF} )(?:koko )?(?:kokonais|lattia|kokonaislattia)?pinta[- ]?ala(?: yhteensä)?$`)],
  ["floor-area", new RegExp(String.raw`^(?:mikä on |kuinka suuri on |paljonko on )?(?:koko |kokonais|kokonaislattia)pinta[- ]?ala(?: yhteensä)?${FI_WHERE}$`)],
  ["floor-area", new RegExp(String.raw`^(?:mikä on |kuinka suuri on |paljonko on )?(?:lattia)?pinta[- ]?ala yhteensä${FI_WHERE}$`)],
  ["largest-room", new RegExp(String.raw`^(?:mikä on |kuinka suuri on )?(?:${FI_OF} )?suurin huone${FI_WHERE}$`)],
  ["largest-room", new RegExp(String.raw`^mikä huone on suurin${FI_WHERE}$`)],
  ["largest-room", new RegExp(String.raw`^(?:mikä on |kuinka suuri on |paljonko on )?(?:${FI_OF} )?suurimman huoneen (?:pinta[- ]?ala|koko)${FI_WHERE}$`)],
];

// German / Swedish whole-sheet wordings ('Wie viele Türen gibt es auf diesem Plan?', 'Hur många fönster finns det?')
const DE_SV_WHERE = String.raw`(?: (?:auf|in|im) (?:diesem|dem|der|dieser) (?:plan|grundriss|zeichnung|geschoss|stockwerk|etage)| (?:der|dieser|die|diese|das|dieses) (?:plan|grundriss|zeichnung|geschoss|stockwerk|etage)| (?:des|dieses) (?:plans|grundrisses|geschosses|stockwerks)| (?:der|dieser) (?:zeichnung|etage)| (?:på|i) (?:denna|den här|ritningen|planen|våningen|plan(?:ritning)?en))?`;
const DE_SV: [CheckKind, RegExp][] = [
  ["doors", new RegExp(String.raw`^wie viele türen(?: gibt es| hat| sind| sind es)?${DE_SV_WHERE}(?: gibt es| es)?$`)],
  ["windows", new RegExp(String.raw`^wie viele fenster(?: gibt es| hat| sind)?${DE_SV_WHERE}(?: gibt es| es)?$`)],
  ["rooms", new RegExp(String.raw`^wie viele (?:räume|zimmer)(?: gibt es| hat| sind)?${DE_SV_WHERE}(?: gibt es| es)?$`)],
  ["floor-area", new RegExp(String.raw`^(?:wie groß ist |was ist )?die (?:gesamte )?(?:netto)?(?:grundfläche|nutzfläche|gesamtfläche|bodenfläche)${DE_SV_WHERE}$`)],
  ["doors", new RegExp(String.raw`^hur många dörrar(?: finns(?: det)?| har)?${DE_SV_WHERE}(?: finns(?: det)?)?$`)],
  ["windows", new RegExp(String.raw`^hur många fönster(?: finns(?: det)?| har)?${DE_SV_WHERE}(?: finns(?: det)?)?$`)],
  ["rooms", new RegExp(String.raw`^hur många rum(?: finns(?: det)?| har)?${DE_SV_WHERE}(?: finns(?: det)?)?$`)],
  ["floor-area", new RegExp(String.raw`^(?:vad är |hur stor är )?den (?:totala )?golvytan${DE_SV_WHERE}$`)],
  ["floor-area", new RegExp(String.raw`^(?:vad är |hur stor är )?(?:den totala (?:golv)?ytan${DE_SV_WHERE}|(?:planens|våningens|ritningens) (?:totala )?(?:golv)?yta)$`)],
];

/** Which whole-sheet quantity a question asks for, or null (a named room, a gross / GIA area, anything else). */
export function checkKind(question: string): CheckKind | null {
  const q = String(question).toLowerCase().replace(/[’`]/g, "'").replace(/[?.!:;,]+/g, " ").replace(/\s+/g, " ").trim();
  if (/\b(?:gross|gia|gifa|gea|external|brutto\w*|kerrosala|kokonaisala|bgf)\b/.test(q)) return null; // gross ≠ the takeoff's net sum
  for (const [k, re] of FI) if (re.test(q)) return k;
  for (const [k, re] of DE_SV) if (re.test(q)) return k;
  const c = q.replace(FILLER, " ").replace(/\s+/g, " ").trim();
  for (const [k, re] of CORES) if (re.test(c)) return k;
  return null;
}

const FT2 = 0.09290304;
const m2Of = (v: number, unit: string | null | undefined) => (/^(?:ft2|ft²|sf|sq ?ft)$/i.test(String(unit ?? "").trim()) ? v * FT2 : v);
const fmt = (v: number, d = 1) => v.toLocaleString("en-US", { maximumFractionDigits: d });

export interface CrossCheck {
  kind: CheckKind;
  /** the takeoff's number (m² for areas) */
  their: number;
  unit: "m²" | "";
  /** what the takeoff says, in words ('1,717.2 m² net, the sum of 77 measured rooms') */
  text: string;
  /** true = agrees (counts exact, areas within 2 %), false = differs, null = the answer has no number to compare */
  agree: boolean | null;
  /** how far apart, in words ('+480 %'), when it differs */
  diff?: string;
  /** the answer's number ÷ the takeoff's (same unit), when both are numbers */
  ratio?: number;
  /** the takeoff's number as 'This floor at a glance' writes it ('1,717.2'), for a row that takes it */
  shown?: string;
}

/** The takeoff's reading of the same quantity and whether the answer agrees with it; null when the takeoff has none. */
export function crossCheck(kind: CheckKind, a: Answer | undefined, g: Glance | null | undefined): CrossCheck | null {
  if (!g || !g.ok || !a) return null;
  const v = typeof a.value === "number" ? a.value : null;
  // one decimal under 10 % (a −2.1 % that 'differs' must not read '−2 %', the tolerance itself)
  const pct = (ours: number, theirs: number) => {
    if (!theirs) return "";
    const r = Math.abs((ours - theirs) / theirs) * 100;
    return `${ours >= theirs ? "+" : "−"}${fmt(r, r < 10 ? 1 : 0)} %`;
  };
  const count = (their: number | null | undefined, text: string, alt?: number | null): CrossCheck | null => {
    if (typeof their !== "number" || !(their >= 0)) return null;
    if (v === null) return { kind, their, unit: "", text, agree: null, shown: fmt(their, 0) };
    const ok = Math.round(v) === Math.round(their) || (typeof alt === "number" && Math.round(v) === Math.round(alt));
    return { kind, their, unit: "", text, agree: ok, shown: fmt(their, 0), ...(ok ? {} : { diff: pct(v, their) }), ...(their > 0 ? { ratio: v / their } : {}) };
  };
  const area = (their: number, text: string, name?: string, digits = 1): CrossCheck => {
    const shown = fmt(their, digits);
    // a text answer naming the takeoff's largest room agrees too
    if (v === null) {
      const said = String(a.value ?? "").toLowerCase();
      return { kind, their, unit: "m²", text, shown, agree: name && said && said.includes(name.toLowerCase()) ? true : null };
    }
    const ours = m2Of(v, a.unit);
    const ok = Math.abs(ours - their) <= 0.02 * Math.max(their, 1e-9);
    return { kind, their, unit: "m²", text, shown, agree: ok, ...(ok ? {} : { diff: pct(ours, their) }), ...(their > 0 ? { ratio: ours / their } : {}) };
  };
  switch (kind) {
    case "floor-area": {
      // the net area is the sum of the MEASURED rooms (DfE L01: 816.8 m² = 36 rooms; the card counts 38)
      const m = measuredRoomRows(g).length, n = g.rooms ?? 0;
      const k = m > 0 && Math.abs(m - n) <= Math.max(2, 0.25 * n) ? m : n;
      return g.rooms && typeof g.area_m2 === "number" && g.area_m2 > 0
        ? area(g.area_m2, `${fmt(g.area_m2)} m² net, the sum of ${k} measured rooms (inner wall faces)`) : null;
    }
    case "rooms": {
      // the card counts F2's rooms per type (a room to check by hand included: DfE L01 38), G1's d32 answer the
      // MEASURED ones (36, the rooms whose areas make the floor's total): either agrees
      const m = measuredRoomRows(g).length;
      const n = typeof g.rooms === "number" ? g.rooms : 0;
      if (!(n > 0)) return null; // no room measured (a printed-table sheet: AFCEC A-2 glance rooms 0): nothing to check
      const alt = m > 0 && m !== n && Math.abs(m - n) <= Math.max(2, 0.25 * n) ? m : undefined; // rows cut short: no aside
      return count(g.rooms, `${g.rooms} rooms${alt !== undefined ? ` (${alt} measured)` : ""}`, alt);
    }
    case "doors": {
      const d = g.doors;
      return count(d?.value, `${d?.value} doors${typeof d?.leaves === "number" && d.leaves !== d.value ? ` (${d.leaves} leaves)` : ""}`, d?.leaves);
    }
    case "windows":
      return count(g.windows?.value, `${g.windows?.value} windows`);
    case "largest-room": {
      let best: { item: string; m2: number } | null = null;
      for (const r of largestRoomRows(g)) {
        const m2 = m2Of(r.value as number, r.unit);
        if (!best || m2 > best.m2) best = { item: String(r.item ?? "a room"), m2 };
      }
      return best ? area(best.m2, `the largest traced room is ${best.item}, ${fmt(best.m2, 2)} m²`, best.item, 2) : null;
    }
  }
}

/** Kinds whose takeoff number replaces a reader's GUESS by itself: the floor's net area, its largest room and its room
 *  count are what the takeoff measures room by room (DfE L00 PNG: 'total floor area' ≈ 9,965 m² = the whole A2 sheet at
 *  1:200, takeoff 1,717.2 m²; 'largest room' ≈ 365.49 vs the traced Multi-Purpose Hall 247.73, hand gold 244.6; rooms 7
 *  vs 77). Doors / windows never: the takeoff's counts are no surer than the readers' (DfE doors: takeoff 91, gold 115). */
export const AUTO_KINDS: ReadonlySet<CheckKind> = new Set(["floor-area", "largest-room", "rooms"]);
/** below this confidence a reader's number counts as a guess here (the card's 'Rough guess') */
export const AUTO_SURE = 0.3;
/** a reader more than 2× off the room-by-room takeoff (rooms 7 vs 77 traced) gives way unless it is at least this sure */
export const AUTO_FAR_SURE = 0.7;
export type AutoWhy = "guess" | "far";
/** Use the takeoff's number instead of the reader's without being asked: the takeoff differs (or the reader gave no
 *  number), the kind is one the takeoff measures, and the reader only guessed (not measured on the drawing, or unsure:
 *  'guess') or is more than 2× off and not very sure ('far': a different scope, e.g. 7 'rooms' vs 77 traced). */
export function autoWhy(x: CrossCheck | null, reader: { guess: boolean; confidence?: number | null; value?: unknown; printed?: boolean }): AutoWhy | null {
  if (!x || !AUTO_KINDS.has(x.kind) || !(x.their > 0)) return null;
  const none = reader.value === null || reader.value === undefined || reader.value === "";
  if (x.agree === true || (x.agree === null && !none)) return null;
  if (none || reader.guess || (reader.confidence ?? 0) < AUTO_SURE) return "guess";
  // a number the sheet itself prints (a GIFA in its area table) is the drawing's own word: never overridden as 'far'
  if (!reader.printed && typeof x.ratio === "number" && (x.ratio < 0.5 || x.ratio > 2) && (reader.confidence ?? 0) < AUTO_FAR_SURE) return "far";
  return null;
}
/** the answer says its number is printed on the sheet (an area table, a stated total), not measured or estimated */
export function printedAnswer(a: Pick<Answer, "assumptions" | "missing"> | undefined): boolean {
  return /as printed|printed (?:table|total|area|count|gifa|gia)\b|the sheet prints|stated on the sheet|taken as stated/i.test([...(a?.assumptions ?? []), ...(a?.missing ?? [])].join(" | "));
}
export function autoUse(x: CrossCheck | null, reader: { guess: boolean; confidence?: number | null; value?: unknown; printed?: boolean }): boolean {
  return autoWhy(x, reader) !== null;
}

/** One plain line for the ANSWER CARD of a whole-sheet question (registry addAnswerNote, ≤ 240 chars): the same check as
 *  a list row, so the card and 'This floor at a glance' beside it never show two totals without saying so (UX M19). */
export function cardNote(x: CrossCheck, why: AutoWhy | null): string {
  if (x.agree === true) return `✓ Agrees with this sheet's takeoff: ${x.text}.`;
  if (x.agree === null) return `This sheet's takeoff: ${x.text}.`;
  const d = x.diff ? ` (this answer ${x.diff})` : "";
  if (why === "guess") return `This sheet's takeoff measured it room by room: ${x.text}${d}. This answer is only a guess.`;
  if (why === "far") return `This sheet's takeoff measured it room by room: ${x.text}${d}. This answer is over 2× off it.`;
  return `This sheet's takeoff: ${x.text}${d}.`;
}
