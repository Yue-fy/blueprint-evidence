/**
 * Feature plugin API (captain 19:54, prompts/FEATURES.md). Feature agents F1–F8 (and W5's right-click menu) plug into
 * the web app through THIS file only; they never edit main.ts / viewer2d.ts / style.css.
 *
 *   // web/src/features/<name>/index.ts
 *   import { registerFeature } from "../registry";
 *   registerFeature("takeoff", (api) => {
 *     api.addToolbarButton({ id: "takeoff", label: "Generate takeoff", onClick: () => panel.open() });
 *     const panel = api.addPanel({ id: "takeoff", title: "Takeoff", render: (el) => { el.textContent = "…"; } });
 *     api.onAnswer((a) => { … });
 *   });
 *   // then append ONE line to web/src/features/index.ts:  import "./takeoff";
 *
 * FLAGS: a feature's setup runs only when it is switched on: `?features=takeoff,region` (or `?features=all`; `?features=`
 * with nothing switches all off) — remembered in this browser (localStorage 'be.features') — or from the console
 * `beFeatures.on("takeoff")` / `beFeatures.off()`, or the hidden toggle Alt+Shift+F (all on / all off, reloads).
 * Default OFF on the captain's stable demo until QA has rehearsed it and S0 has gated it.
 * FAIL CLOSED: an error in setup or in any callback is caught, logged once ('[feature takeoff] …') and the feature's
 * buttons / menu items / panels are removed; the existing flow never breaks.
 *
 * COORDINATES: drawing points are [x, y] in the evidence space of the open file (docs/CONTRACT.md): 'pdf_pt' for PDFs
 * (PDF points, y down from the top of the page as the API's evidence uses them), 'image_px' for images. Overlays draw
 * in the viewer's content pixels: use overlay.toContent(pt) for every point (handles /Rotate), and divide stroke widths
 * by overlay.zoom() for a constant on-screen size.
 */
import type { Answer, FileInfo } from "../types";

export type Space = "pdf_pt" | "image_px";
export type DrawingPt = [number, number];
export type ViewMode = "2d" | "3d" | "split";

/** What is under the pointer (right-click / long-press), for context-menu predicates and askWith(). */
export type PointerCtx = {
  file: FileInfo | null;
  page: number; // 0-based page of a multi-page PDF (0 for images)
  view: ViewMode;
  point: DrawingPt | null; // drawing coordinates under the pointer (null: outside the drawing / in 3D)
  space: Space;
  client: { x: number; y: number }; // screen position (for popovers)
  label: { text: string; at: DrawingPt; dist: number } | null; // the printed text nearest the pointer (PDF text layer)
  room: { name: string; at: DrawingPt } | null; // the room label the pointer is in / nearest (when the sheet has room names)
  guid: string | null; // IFC element under the pointer in 3D (or on an IFC sheet), when known
  answer: Answer | null; // the answer on screen
  question: string; // the text in the question box
  /** captain 23:47: the THING under the pointer, selected and highlighted at once (right-click / long-press); null =
   *  nothing recognised there. Set before the menu's when() predicates run; may be refined once (async trace). */
  selection?: Selection | null;
};

/** What a right-click selected (captain 23:47). Hit-test priority: a counted symbol (door / window box of the answer on
 *  screen, else the sheet's door / window finder) > a room (its outline from the answer, the sheet's takeoff or a trace)
 *  > a tag or printed label > a dimension string > a slab zone. bbox / poly are in the drawing space (= Evidence.bbox). */
export type SelectionType = "door" | "window" | "symbol" | "room" | "tag" | "dimension" | "wall" | "slab";
export type Selection = {
  type: SelectionType;
  bbox: [number, number, number, number] | null;
  poly?: DrawingPt[] | null;
  /** = poly (F1 00:36 reads ctx.selection.polygon for 'Edit outline'): the room's / zone's outline, >= 3 points */
  polygon?: DrawingPt[] | null;
  space: Space;
  page: number;
  /** where it came from: 'answer' = a mark of the answer on screen (index = its place in answer.evidence); 'detector' =
   *  the sheet's door / window finder (takeoff); 'takeoff' = a room outline of the sheet's takeoff; 'trace' = traced now
   *  from the wall lines around the room's label; 'label' = printed text only */
  source: "answer" | "detector" | "template" | "takeoff" | "trace" | "label";
  index?: number;
  /** the selection's short label on the drawing: 'Kitchen · 35.6 m²' / 'Door 12' / 'FP-3' */
  label: string;
  /** the room name / tag text for questions ('Kitchen', 'FP-3'); for rooms with a number 'Zimmer 2.01' */
  name?: string;
  /** a value already known for it (the takeoff's area of the room) */
  value?: { value: number; unit: string } | null;
  /** the label point of a room (for ctx.room) */
  at?: DrawingPt;
};

/** Extra context sent with a question (API side: G1 'point' / 'view' / 'region' in /ask). */
export type AskContext = {
  point?: DrawingPt; // one click
  region?: DrawingPt[]; // a polygon (box = 4 points), drawing coordinates
  space?: Space;
  page?: number;
  view?: string; // e.g. the sheet view / floor the user is looking at
  file?: string; // default: the open file
  solver?: string;
  solver_args?: Record<string, unknown>;
  show?: boolean; // default true: the answer is shown on the card + drawing like any answer
};

export type MeasureEv = {
  kind: "area" | "length";
  value: number;
  unit: "m²" | "m";
  poly?: DrawingPt[]; // area: the traced outline
  line?: [DrawingPt, DrawingPt]; // length: the two clicks
  name?: string; // the room's printed name, when the card traced a named room
  manual: boolean;
  scale: string; // '1:200' / '1 m = 51 px'
  file: string;
  page: number;
  space: Space;
};

/** addPanel's options. group + tab put the panel in ONE floating sheet with tabs (captain 21:51: the takeoff's
 *  Quantities · Conventions · Order list · Check list); order sorts the tabs (lower = left). The features 'conventions',
 *  'orderlist' and 'checklist' (and F2's takeoff panel as 'Quantities') join the 'takeoff' group by name. */
export type PanelOpts = { id: string; title: string; render: (el: HTMLElement) => void; onClose?: () => void; group?: string; tab?: string; order?: number };
export type PanelHandle = { el: HTMLElement; open(): void; close(): void; toggle(): void; isOpen(): boolean };
/** A quiet button on the answer card (F8 'Report'): shown when when(a, q) is true (default: every answer with a value). */
export type CardAction = { id: string; label: string; title?: string; icon?: string; when?: (a: Answer, question: string) => boolean; run: (a: Answer, question: string) => void; order?: number };

export type Overlay2D = {
  g: SVGGElement; // your layer inside the drawing's SVG overlay (content pixels)
  toContent(p: DrawingPt): [number, number]; // drawing point → content px
  toClient(p: DrawingPt): { x: number; y: number } | null; // drawing point → screen
  zoom(): number; // content px per screen px factor: strokeWidth = 2 / zoom() → 2 screen px
  clear(): void;
  remove(): void;
};

/** What main.ts gives the registry (one object, set by initFeatures). Features use FeatureApi below. */
export type Host = {
  file(): FileInfo | null;
  answer(): Answer | null;
  question(): string;
  page(): number;
  view(): ViewMode;
  isCustomer(): boolean;
  pointerCtx(clientX: number, clientY: number): PointerCtx;
  /** The room under the pointer from its traced outline (the printed name INSIDE it), when that differs from the nearest
   *  label; undefined = keep ctx.room. Optional (async: the menu opens at once and is refreshed when this lands). */
  refineRoom?(ctx: PointerCtx): Promise<PointerCtx["room"] | undefined>;
  /** captain 23:47: the thing under the pointer, from cached geometry at once (now), and when that found no room, a
   *  slower trace (later: resolves to a better selection, null = none, undefined = keep now). */
  selectAt?(ctx: PointerCtx): { now: Selection | null; later?: Promise<Selection | null | undefined> };
  overlayLayer(): SVGGElement | null;
  toContent(p: DrawingPt): [number, number];
  toClient(p: DrawingPt): { x: number; y: number } | null;
  zoom(): number;
  ask(question: string, ctx: AskContext): Promise<Answer>;
  askShown(question: string, ctx: AskContext): Promise<Answer>; // through the question box: waiting state, card, marks
  showAnswer(a: Answer, question?: string): Promise<void>;
  setQuestion(q: string): void;
  viewer3d(): Promise<unknown | null>; // the Viewer3D instance (lazy-loaded), null when no model is open
  openPage(k: number): Promise<void>; // page k of the open PDF, or sheet k of a set of files dropped together
  openFile(path: string, page?: number): Promise<void>;
  focus(bbox: number[], space?: Space): void;
  openMeasure(tool?: "scale" | "length" | "area", at?: DrawingPt): void; // the measure tool on the 2D drawing (at: first click)
  uploads(): string[]; // the visitor's uploaded files open now: the open upload + the files dropped with it
  toolbar(): HTMLElement | null; // where toolbar buttons go
  panelHost(): HTMLElement | null; // where panels go
};

export type FeatureApi = {
  name: string;
  /** A pill button in the viewer's toolbar (next to Measure). Returns remove(). */
  addToolbarButton(o: { id: string; label: string; title?: string; icon?: string; onClick: () => void; when?: () => boolean }): () => void;
  /** An item in the right-click / long-press menu, shown when when(ctx) is true (max ~5 items show at once). */
  addContextMenuItem(o: { id: string; label: string | ((ctx: PointerCtx) => string); when: (ctx: PointerCtx) => boolean; run: (ctx: PointerCtx) => void; order?: number }): () => void;
  /** A side panel (floating sheet on the right of the viewer); one panel is open at a time. With group/tab it is a tab
   *  of a shared sheet (see PanelOpts). */
  addPanel(o: PanelOpts): PanelHandle;
  /** A tab in the takeoff sheet (captain 21:51: Quantities · Conventions · Order list · Check list): addPanel in the
   *  'takeoff' group + one toolbar entry that folds into the takeoff's (only the first visible one of the group shows). */
  addTakeoffTab(o: { id: string; label: string; title?: string; render: (el: HTMLElement) => void; onClose?: () => void; order?: number; when?: () => boolean }): PanelHandle;
  /** A quiet button on the answer card, e.g. 'Report' (F8); at most 3 show. Returns remove(). */
  addCardAction(o: CardAction): () => void;
  /** = addCardAction (the name F8's plug-in feature-detects). */
  addAnswerAction(o: CardAction): () => void;
  /** A 2D overlay layer; redraw it in onDrawing() (the layer is emptied when another drawing or page opens). */
  addOverlay2D(id: string): Overlay2D;
  /** The 3D viewer (three.js; Viewer3D), or null when the open file has no model. */
  viewer3d(): Promise<unknown | null>;
  /** Called after every answer is shown (also cached replays and the visitor's choices). Returns unsubscribe. */
  onAnswer(cb: (a: Answer, question: string) => void): () => void;
  /** Called when a drawing or another page of it has opened (redraw overlays here). */
  onDrawing(cb: (file: FileInfo | null, page: number) => void): () => void;
  /** Ask a question with extra context (a point, a region, a view); shows the answer like any other unless show=false. */
  askWith(question: string, ctx?: AskContext): Promise<Answer>;
  /** Show an answer you got with askWith({show: false}) (e.g. after adding a trace line), like any answer. */
  showAnswer(a: Answer, question?: string): Promise<void>;
  /** Turn to page k (0-based) of the open PDF, or to sheet k of a set of files dropped together (F8). */
  openPage(k: number): Promise<void>;
  /** Open another file (an upload or a dataset path), optionally at page k (T3). */
  openFile(path: string, page?: number): Promise<void>;
  /** Pan / zoom the drawing to a box in drawing coordinates (F2). */
  focus(bbox: number[], space?: Space): void;
  /** Files the visitor just uploaded (after linking them into one set), once per upload (T3). */
  onUpload(cb: (files: FileInfo[]) => void): () => void;
  /** Answer a question from the main question box yourself: return a Promise<Answer> to take it (it is shown like any
   *  answer), or null to let the normal ask run (T3's 'This building' scope). The first feature that returns one wins. */
  beforeAsk(fn: (question: string, file: FileInfo) => Promise<Answer> | null): () => void;
  /** Extra solver_args merged into EVERY /ask (return null for none), e.g. {learn: <store revision>} (F7). */
  addAskArgs(fn: (path: string, question: string) => Record<string, unknown> | null): () => void;
  /** The visitor set a scale (two clicks / typed ratio) or reset it (null) (F7). */
  onScale(cb: (s: { unitsPerM: number; space: Space; label: string; user: boolean; file: string | null; page: number } | null) => void): () => void;
  /** The visitor picked an option of a clarify question (clarify = Answer.clarify or its trace-line form) (F7). */
  onClarifyPick(cb: (clarify: unknown, option: unknown, question: string) => void): () => void;
  /** A measurement finished on the drawing (F7): a traced room or a two-click length, in m² / m at the scale in use.
   *  manual = the visitor clicked it (false: the answer card traced a named room by itself). */
  onMeasure(cb: (m: MeasureEv) => void): () => void;
  /** Open the measure tool on the 2D drawing (switches to Plan), on 'scale' / 'length' / 'area' (F6); at = its first
   *  click (a length's first end, the room to trace). */
  openMeasure(tool?: "scale" | "length" | "area", at?: DrawingPt): void;
  /** The visitor's uploaded files open now ('upload/<id>/<name>'): the open one + the files dropped with it (T2). */
  uploads(): string[];
  /** The visitor changed what a drawing is read as in the recognition menu (F7): {file, solver, label}, or null for
   *  'Read it as detected'. */
  onReadAs(cb: (r: { file: string; solver: string; label: string } | null) => void): () => void;
  /** One plain-text line (no HTML) under the answer card's badges, recomputed for every answer; null = nothing (F6). */
  addAnswerNote(fn: (a: Answer, question: string) => string | null): () => void;
  /** Read-only state. */
  file(): FileInfo | null;
  answer(): Answer | null;
  question(): string;
  page(): number;
  view(): ViewMode;
  isCustomer(): boolean;
  /** Put text in the question box (does not ask). */
  setQuestion(q: string): void;
  /** Is another feature on? */
  enabled(name: string): boolean;
};

// --- flags --------------------------------------------------------------------------------------------------------
const FLAG_KEY = "be.features";
function readFlags(): Set<string> {
  let raw: string | null = null;
  try {
    const q = new URLSearchParams(location.search);
    if (q.has("features")) {
      raw = q.get("features") ?? "";
      try {
        localStorage.setItem(FLAG_KEY, raw);
      } catch {
        /* private window: this page only */
      }
    } else raw = localStorage.getItem(FLAG_KEY);
  } catch {
    raw = null;
  }
  return new Set((raw ?? "").split(/[\s,]+/).map((s) => s.trim().toLowerCase()).filter(Boolean));
}
const flags = readFlags();
/** On unless switched off (?features=-rightclick or none): captain 21:02, right-click on any 2D sheet with its
 *  'Ask about this area…' item (F1's region). */
const DEFAULT_ON = new Set(["rightclick", "region"]);
/** captain 00:31: the features the API serves (GET /api/feat: 'mounted', or 'web-only' when STOA_FEATURES names a
 *  web-only one) are on by default, so the final swap only sets STOA_FEATURES on :8100. An explicit list
 *  (?features=a,b or a remembered one) decides alone; '-name' / 'none' still switch off. */
const apiOn = new Set<string>();
export function enabled(name: string): boolean {
  if (flags.has("none") || flags.has("-" + name.toLowerCase())) return false;
  return flags.has("all") || flags.has("*") || flags.has(name.toLowerCase()) || DEFAULT_ON.has(name.toLowerCase()) || apiOn.has(name.toLowerCase());
}
const explicitList = () => [...flags].some((f) => !f.startsWith("-") && f !== "none");
async function servedFeatures() {
  if (explicitList() || flags.has("none")) return;
  let st: Record<string, string> = {};
  try {
    const r = await fetch("/api/feat");
    if (!r.ok) return;
    st = ((await r.json()) as { features?: Record<string, string> }).features ?? {};
  } catch {
    return;
  }
  const late: string[] = [];
  for (const name of features.keys()) {
    const v = String(st[name] ?? st[name.toLowerCase()] ?? "");
    if ((v === "mounted" || v === "web-only") && !enabled(name)) {
      apiOn.add(name.toLowerCase());
      start(name);
      if (!failed.has(name)) late.push(name);
    }
  }
  if (!late.length || !host) return;
  // the late ones catch up: the sheet already open, the answer already shown
  const f = host.file(), a = host.answer(), q = host.question();
  for (const e of [...drawingCbs]) if (late.includes(e.feature) && f) guard(e.feature, () => e.cb(f, host!.page()), undefined)();
  for (const e of [...answerCbs]) if (late.includes(e.feature) && a) guard(e.feature, () => e.cb(a, q), undefined)();
  console.info(`[features] on from the API's list: ${late.join(", ")}`);
}
const setFlags = (v: string) => {
  try {
    localStorage.setItem(FLAG_KEY, v);
  } catch {
    /* ignore */
  }
  const u = new URL(location.href);
  u.searchParams.delete("features");
  location.href = u.toString();
};
(window as unknown as { beFeatures: unknown }).beFeatures = {
  list: () => [...features.keys()].map((n) => `${n}: ${enabled(n) ? "on" : "off"}${failed.has(n) ? " (failed: " + failed.get(n) + ")" : ""}`),
  on: (...names: string[]) => setFlags([...new Set([...flags, ...(names.length ? names : ["all"])])].filter((x) => x !== "none").join(",")),
  off: (...names: string[]) => setFlags(names.length ? [...flags].filter((x) => !names.includes(x)).join(",") : ""),
};
document.addEventListener("keydown", (e) => {
  if (e.altKey && e.shiftKey && (e.key === "F" || e.key === "f" || e.code === "KeyF")) setFlags(flags.size ? "" : "all");
});

// --- registry -----------------------------------------------------------------------------------------------------
type Item = { feature: string; id: string; label: string | ((c: PointerCtx) => string); when: (c: PointerCtx) => boolean; run: (c: PointerCtx) => void; order: number };
const features = new Map<string, (api: FeatureApi) => void>();
const failed = new Map<string, string>();
const menuItems: Item[] = [];
const answerCbs: { feature: string; cb: (a: Answer, q: string) => void }[] = [];
const drawingCbs: { feature: string; cb: (f: FileInfo | null, p: number) => void }[] = [];
type ScaleEv = { unitsPerM: number; space: Space; label: string; user: boolean; file: string | null; page: number } | null;
const askArgFns: { feature: string; fn: (path: string, q: string) => Record<string, unknown> | null }[] = [];
const scaleCbs: { feature: string; cb: (s: ScaleEv) => void }[] = [];
const pickCbs: { feature: string; cb: (c: unknown, o: unknown, q: string) => void }[] = [];
const uploadCbs: { feature: string; cb: (files: FileInfo[]) => void }[] = [];
const measureCbs: { feature: string; cb: (m: MeasureEv) => void }[] = [];
const readAsCbs: { feature: string; cb: (r: { file: string; solver: string; label: string } | null) => void }[] = [];
const noteFns: { feature: string; fn: (a: Answer, q: string) => string | null }[] = [];
const beforeAskFns: { feature: string; fn: (q: string, f: FileInfo) => Promise<Answer> | null }[] = [];
const cardActs: (CardAction & { feature: string })[] = [];
/** Panels that share one sheet with tabs (captain 21:51). Keyed by '<feature>/<panel id>' first, then '<feature>'. */
const GROUP_TITLE: Record<string, string> = { takeoff: "Takeoff" };
const KNOWN_TABS: Record<string, { group: string; tab: string; order: number }> = {
  "takeoff/takeoff": { group: "takeoff", tab: "Quantities", order: 0 },
  conventions: { group: "takeoff", tab: "Conventions", order: 10 },
  orderlist: { group: "takeoff", tab: "Order list", order: 20 },
  checklist: { group: "takeoff", tab: "Check list", order: 30 },
};
const groupOf = (feature: string, id: string) => KNOWN_TABS[`${feature}/${id}`] ?? KNOWN_TABS[feature];
type Tab = { key: string; feature: string; group: string; tab: string; order: number; open: () => void; isOpen: () => boolean };
const tabs: Tab[] = [];
function listen<T extends { feature: string }>(list: T[], e: T): () => void {
  list.push(e);
  return onClean(e.feature, () => {
    const i = list.indexOf(e);
    if (i >= 0) list.splice(i, 1);
  });
}
const cleanups = new Map<string, (() => void)[]>();
let host: Host | null = null;

/** Register a feature; its setup runs at start-up when enabled(name). Call it at module top level. */
export function registerFeature(name: string, setup: (api: FeatureApi) => void) {
  features.set(name, setup);
  if (host) start(name);
}

function fail(name: string, e: unknown) {
  if (failed.has(name)) return;
  const msg = String((e as Error)?.message ?? e).slice(0, 200);
  failed.set(name, msg);
  console.warn(`[feature ${name}] switched off after an error:`, e);
  for (const c of cleanups.get(name) ?? []) {
    try {
      c();
    } catch {
      /* already gone */
    }
  }
  cleanups.delete(name);
}
const guard = <A extends unknown[], R>(name: string, fn: (...a: A) => R, dflt: R) => (...a: A): R => {
  if (failed.has(name)) return dflt;
  try {
    return fn(...a);
  } catch (e) {
    fail(name, e);
    return dflt;
  }
};
const onClean = (name: string, c: () => void) => {
  const l = cleanups.get(name) ?? [];
  l.push(c);
  cleanups.set(name, l);
  return c;
};

let openPanel: { id: string; close(): void; group?: string } | null = null;

// --- the toolbar on the drawing: at most 3 pills, the rest under 'More tools' (QA 21:47: with every flag on, ten pills
// sat in three rows over the drawing and its sheet pager). A tab of the takeoff sheet folds into the takeoff's pill.
const PRIORITY: Record<string, number> = { takeoff: 0, region: 1, "project/project": 2, measure: 3, batch: 4, revcompare: 5, "project/newproject": 20 };
const rankOfBtn = (b: HTMLElement) => PRIORITY[`${b.dataset.feature}/${b.dataset.id}`] ?? PRIORITY[b.dataset.feature ?? ""] ?? 50;
const watched = new WeakSet<HTMLElement>();
const overflow = new WeakMap<HTMLElement, HTMLButtonElement[]>(); // the bar's entries under 'More tools', in order
let lastMoreClose = 0;
function watchBar(bar: HTMLElement) {
  if (watched.has(bar)) return layoutBar(bar);
  watched.add(bar);
  let queued = false;
  const later = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      layoutBar(bar);
    });
  };
  new MutationObserver(later).observe(bar, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
  window.addEventListener("resize", later);
  later();
}
function layoutBar(bar: HTMLElement) {
  const all = [...bar.querySelectorAll<HTMLButtonElement>(".feat-btn:not(.feat-more)")];
  const shown = all.filter((b) => !b.hidden);
  // a group (the takeoff's tabs): only its first visible pill, in tab order
  const lead = new Map<string, HTMLButtonElement>();
  for (const b of shown) {
    const g = b.dataset.group;
    if (!g) continue;
    const cur = lead.get(g);
    if (!cur || Number(b.dataset.gorder ?? 50) < Number(cur.dataset.gorder ?? 50)) lead.set(g, b);
  }
  const live = shown.filter((b) => !b.dataset.group || lead.get(b.dataset.group) === b);
  live.sort((x, y) => rankOfBtn(x) - rankOfBtn(y) || all.indexOf(x) - all.indexOf(y));
  // one row: 3 pills on a wide drawing, 2 at 1440 px with the answer card open (the drawing is ~900 px wide)
  const max = (bar.parentElement?.clientWidth ?? 1200) < 1100 ? 2 : 3;
  const pills = live.length <= max + 1 ? live : live.slice(0, max);
  const rest = live.slice(pills.length);
  overflow.set(bar, rest);
  for (const b of all) {
    b.classList.toggle("feat-folded", !pills.includes(b));
    const k = String(Math.max(0, pills.indexOf(b)));
    if (b.style.order !== k) b.style.order = k;
  }
  let more = bar.querySelector<HTMLButtonElement>(".feat-more");
  if (!rest.length) {
    if (more && !more.hidden) more.hidden = true;
    return;
  }
  if (!more) {
    more = document.createElement("button");
    more.type = "button";
    more.className = "feat-btn feat-more";
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    more.innerHTML = `<span>More tools</span><span class="feat-caret" aria-hidden="true">▾</span>`;
    more.addEventListener("click", () => {
      if (performance.now() - lastMoreClose < 350) return; // the press that closed it
      openMore(bar, more!);
    });
    bar.append(more);
  }
  more.style.order = "99";
  more.title = rest.map(btnLabel).filter(Boolean).join(" · ");
  if (more.hidden) more.hidden = false;
}
/** A pill's label as shown (features rewrite it: 'Learned (2)' replaces the spans with plain text). */
function btnLabel(b: HTMLElement): string {
  const c = b.cloneNode(true) as HTMLElement;
  c.querySelectorAll(".feat-ic").forEach((x) => x.remove());
  return (c.textContent ?? "").replace(/\s+/g, " ").trim();
}
function openMore(bar: HTMLElement, more: HTMLButtonElement) {
  closeMenu();
  layoutBar(bar);
  const list = (overflow.get(bar) ?? []).filter((b) => b.isConnected && !b.hidden);
  if (!list.length) return;
  const m = document.createElement("div");
  m.className = "feat-menu feat-more-menu";
  m.dataset.kind = "more";
  m.setAttribute("role", "menu");
  for (const b of list) {
    const it = document.createElement("button");
    it.type = "button";
    it.setAttribute("role", "menuitem");
    const ic = b.querySelector(".feat-ic");
    it.innerHTML = `<span class="feat-ic" aria-hidden="true">${ic ? ic.innerHTML : ""}</span><span></span>`;
    it.querySelector("span:last-child")!.textContent = btnLabel(b) || b.dataset.id || "";
    if (b.dataset.warn === "1") it.classList.add("warn");
    it.title = b.title;
    it.addEventListener("click", () => {
      closeMenu();
      b.click();
    });
    m.append(it);
  }
  document.body.append(m);
  menuEl = m;
  more.setAttribute("aria-expanded", "true");
  const r = more.getBoundingClientRect(), mr = m.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - mr.width - 8))}px`;
  m.style.top = `${Math.max(8, r.top - mr.height - 8)}px`;
  (m.querySelector("button") as HTMLButtonElement | null)?.focus();
}
function apiFor(name: string): FeatureApi {
  const h = () => host!;
  return {
    name,
    addToolbarButton(o) {
      const bar = h().toolbar();
      if (!bar) return () => undefined;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "feat-btn";
      b.dataset.feature = name;
      b.dataset.id = o.id;
      b.title = o.title ?? o.label;
      b.innerHTML = `${o.icon ? `<span class="feat-ic" aria-hidden="true">${o.icon}</span>` : ""}<span></span>`;
      b.querySelector("span:last-child")!.textContent = o.label;
      const g = (o as { group?: { group: string; order: number } }).group ?? groupOf(name, o.id);
      if (g) {
        b.dataset.group = g.group;
        b.dataset.gorder = String(g.order);
      }
      b.addEventListener("click", guard(name, () => o.onClick(), undefined));
      bar.append(b);
      bar.hidden = false;
      watchBar(bar);
      const sync = guard(name, () => void (b.hidden = !!o.when && !o.when()), undefined);
      sync();
      const offA = apiFor(name).onDrawing(sync);
      const offB = apiFor(name).onAnswer(sync); // when() may depend on the answer on screen (F8 22:18)
      return onClean(name, () => {
        offA();
        offB();
        b.remove();
        bar.hidden = !bar.querySelector(".feat-btn:not(.feat-more)");
      });
    },
    addContextMenuItem(o) {
      const it: Item = { feature: name, id: o.id, label: o.label, when: o.when, run: o.run, order: o.order ?? 50 };
      menuItems.push(it);
      return onClean(name, () => {
        const i = menuItems.indexOf(it);
        if (i >= 0) menuItems.splice(i, 1);
      });
    },
    addPanel(o) {
      const root = h().panelHost();
      const el = document.createElement("section");
      el.className = "feat-panel";
      el.dataset.feature = name;
      el.hidden = true;
      el.setAttribute("aria-label", o.title);
      el.innerHTML = `<header><h3></h3><button type="button" class="feat-x" aria-label="Close">✕</button></header><div class="feat-tabs" role="tablist" hidden></div><div class="feat-body"></div>`;
      const h3 = el.querySelector("h3")!;
      h3.textContent = o.title;
      const body = el.querySelector<HTMLElement>(".feat-body")!;
      const key = `${name}/${o.id}`;
      const g = o.group ? { group: o.group, tab: o.tab ?? o.title, order: o.order ?? 50 } : groupOf(name, o.id);
      if (g) el.dataset.group = g.group;
      // one sheet, several tabs: the strip lists the group's panels (of features that are on and did not fail)
      const drawTabs = () => {
        if (!g) return;
        const list = tabs.filter((t) => t.group === g.group && !failed.has(t.feature)).sort((x, y) => x.order - y.order);
        const strip = el.querySelector<HTMLElement>(".feat-tabs")!;
        if (list.length < 2) {
          strip.hidden = true;
          h3.textContent = o.title;
          return;
        }
        h3.textContent = GROUP_TITLE[g.group] ?? o.title;
        strip.hidden = false;
        strip.replaceChildren(...list.map((t) => {
          const tb = document.createElement("button");
          tb.type = "button";
          tb.setAttribute("role", "tab");
          tb.setAttribute("aria-selected", String(t.key === key));
          tb.className = t.key === key ? "on" : "";
          tb.textContent = t.tab;
          tb.addEventListener("click", () => { if (t.key !== key) t.open(); });
          return tb;
        }));
      };
      root?.append(el);
      const p = {
        el: body,
        open: () => {
          // a tab switch inside one sheet: no fade-in (the sheet stays where it is)
          el.classList.toggle("tab-switch", !!g && !!openPanel && openPanel.id !== key && openPanel.group === g.group);
          if (openPanel && openPanel.id !== key) openPanel.close();
          drawTabs();
          guard(name, () => o.render(body), undefined)();
          el.hidden = false;
          if (root) root.hidden = false;
          openPanel = { id: key, close: p.close, group: g?.group };
        },
        close: () => {
          el.hidden = true;
          if (root) root.hidden = !root.querySelector(".feat-panel:not([hidden])");
          if (openPanel?.id === key) openPanel = null;
          if (o.onClose) guard(name, o.onClose, undefined)();
        },
        toggle: () => (el.hidden ? p.open() : p.close()),
        isOpen: () => !el.hidden,
      };
      el.querySelector(".feat-x")!.addEventListener("click", () => p.close());
      const tab: Tab | null = g ? { key, feature: name, group: g.group, tab: g.tab, order: g.order, open: p.open, isOpen: p.isOpen } : null;
      if (tab) tabs.push(tab);
      onClean(name, () => {
        el.remove();
        if (tab && tabs.indexOf(tab) >= 0) tabs.splice(tabs.indexOf(tab), 1);
        if (root) root.hidden = !root.querySelector(".feat-panel:not([hidden])");
      });
      return p;
    },
    addTakeoffTab(o) {
      const api = apiFor(name);
      const order = o.order ?? 50;
      const p = api.addPanel({ id: o.id, title: o.label, render: o.render, onClose: o.onClose, group: "takeoff", tab: o.label, order });
      // its own entry, folded into the takeoff's while that one shows (the group's first visible button wins)
      api.addToolbarButton({ id: o.id, label: o.label, title: o.title, when: o.when, onClick: () => p.toggle(), group: { group: "takeoff", order } } as Parameters<FeatureApi["addToolbarButton"]>[0]);
      return p;
    },
    addCardAction: (o) => listen(cardActs, { ...o, feature: name, order: o.order ?? 50 }),
    addAnswerAction: (o) => listen(cardActs, { ...o, feature: name, order: o.order ?? 50 }),
    addOverlay2D(id) {
      const parent = h().overlayLayer();
      const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.classList.add("feat-layer");
      g.dataset.feature = name;
      g.dataset.id = id;
      parent?.append(g);
      const ov: Overlay2D = {
        g,
        toContent: (p) => h().toContent(p),
        toClient: (p) => h().toClient(p),
        zoom: () => h().zoom(),
        clear: () => g.replaceChildren(),
        remove: () => g.remove(),
      };
      onClean(name, () => g.remove());
      return ov;
    },
    viewer3d: () => h().viewer3d(),
    onAnswer(cb) {
      const e = { feature: name, cb };
      answerCbs.push(e);
      return onClean(name, () => {
        const i = answerCbs.indexOf(e);
        if (i >= 0) answerCbs.splice(i, 1);
      });
    },
    onDrawing(cb) {
      const e = { feature: name, cb };
      drawingCbs.push(e);
      return onClean(name, () => {
        const i = drawingCbs.indexOf(e);
        if (i >= 0) drawingCbs.splice(i, 1);
      });
    },
    showAnswer: (a, q) => h().showAnswer(a, q),
    openPage: (k) => h().openPage(k),
    openFile: (p, k) => h().openFile(p, k),
    focus: (b, sp) => guard(name, () => h().focus(b, sp), undefined)(),
    onUpload: (cb) => listen(uploadCbs, { feature: name, cb }),
    beforeAsk: (fn) => listen(beforeAskFns, { feature: name, fn }),
    addAskArgs: (fn) => listen(askArgFns, { feature: name, fn }),
    onScale: (cb) => listen(scaleCbs, { feature: name, cb }),
    onClarifyPick: (cb) => listen(pickCbs, { feature: name, cb }),
    onMeasure: (cb) => listen(measureCbs, { feature: name, cb }),
    openMeasure: (tool, at) => guard(name, () => h().openMeasure(tool, at), undefined)(),
    uploads: () => guard(name, () => h().uploads(), [] as string[])(),
    onReadAs: (cb) => listen(readAsCbs, { feature: name, cb }),
    addAnswerNote: (fn) => listen(noteFns, { feature: name, fn }),
    async askWith(question, ctx = {}) {
      if (ctx.show === false) return h().ask(question, ctx);
      return h().askShown(question, ctx);
    },
    file: () => h().file(),
    answer: () => h().answer(),
    question: () => h().question(),
    page: () => h().page(),
    view: () => h().view(),
    isCustomer: () => h().isCustomer(),
    setQuestion: (q) => h().setQuestion(q),
    enabled,
  };
}

function start(name: string) {
  if (!host || !enabled(name) || failed.has(name)) return;
  const setup = features.get(name);
  if (!setup) return;
  try {
    setup(apiFor(name));
  } catch (e) {
    fail(name, e);
  }
}

/** main.ts: once, when the app is ready. */
export function initFeatures(h: Host) {
  host = h;
  for (const name of features.keys()) start(name);
  installContextMenu();
  void servedFeatures();
}
/** main.ts: after an answer is shown. */
export function emitAnswer(a: Answer, q: string) {
  clearSelection(); // the answer's own marks take over
  for (const e of [...answerCbs]) guard(e.feature, () => e.cb(a, q), undefined)();
}
/** main.ts: after a drawing (or another page of it) has opened. */
let lastDrawing = "";
export function emitDrawing(f: FileInfo | null, page: number) {
  // a selection never survives a sheet / floor change, and a menu about the old sheet goes too; the SAME sheet drawn
  // again (a warm-up refresh, a re-render) keeps them (UX N-7: the menu closed by itself ~4 s after opening)
  const key = `${f?.path ?? ""}|${page}`;
  if (key !== lastDrawing) {
    closeMenu();
    clearSelection();
  }
  lastDrawing = key;
  for (const e of [...drawingCbs]) guard(e.feature, () => e.cb(f, page), undefined)();
}
/** api.ts: the solver_args every enabled feature adds to a question ({} when none). */
export function extraAskArgs(path: string, q: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const e of askArgFns) Object.assign(out, guard(e.feature, () => e.fn(path, q), null)() ?? {});
  return out;
}
/** main.ts: the recognition menu's kind pick (F7). */
export function emitReadAs(r: { file: string; solver: string; label: string } | null) {
  for (const e of [...readAsCbs]) guard(e.feature, () => e.cb(r), undefined)();
}
/** main.ts renderAnswer: the enabled features' plain-text notes for this answer (F6), at most 3. */
export function answerNotes(a: Answer, q: string): string[] {
  const out: string[] = [];
  for (const e of noteFns) {
    const t = guard(e.feature, () => e.fn(a, q), null)();
    if (typeof t === "string" && t.trim()) out.push(t.trim().slice(0, 240));
  }
  return out.slice(0, 3);
}
/** main.ts (measure.ts onMeasure): a measurement finished on the drawing. */
export function emitMeasure(m: MeasureEv) {
  for (const e of [...measureCbs]) guard(e.feature, () => e.cb(m), undefined)();
}
/** main.ts: after an upload (the files as stored, linked when several). */
export function emitUpload(files: FileInfo[]) {
  for (const e of [...uploadCbs]) guard(e.feature, () => e.cb(files), undefined)();
}
/** main.ts runAsk: a feature's own answer for this question, or null. */
export function featureAnswer(q: string, f: FileInfo): Promise<Answer> | null {
  for (const e of beforeAskFns) {
    const p = guard(e.feature, () => e.fn(q, f), null)();
    if (p && typeof (p as Promise<Answer>).then === "function") return (p as Promise<Answer>).catch((err) => { fail(e.feature, err); throw err; });
  }
  return null;
}
/** main.ts: the visitor set (or reset) a scale. */
export function emitScale(s: ScaleEv) {
  for (const e of [...scaleCbs]) guard(e.feature, () => e.cb(s), undefined)();
}
/** main.ts: the visitor picked a clarify option. */
export function emitClarifyPick(c: unknown, o: unknown, q: string) {
  for (const e of [...pickCbs]) guard(e.feature, () => e.cb(c, o, q), undefined)();
}
/** main.ts renderHero: the card's feature buttons for this answer (F8 'Report'), at most 3. */
export function cardActions(a: Answer, q: string): { id: string; feature: string; label: string; title: string; icon: string; run: () => void }[] {
  return cardActs
    .filter((x) => !failed.has(x.feature) && guard(x.feature, () => (x.when ? x.when(a, q) : a.value !== null && a.value !== undefined), false)())
    .sort((x, y) => (x.order ?? 50) - (y.order ?? 50))
    .slice(0, 3)
    .map((x) => ({ id: x.id, feature: x.feature, label: x.label, title: x.title ?? x.label, icon: x.icon ?? "", run: guard(x.feature, () => x.run(a, q), undefined) }));
}
export const anyFeatureOn = () => [...features.keys()].some((n) => enabled(n) && !failed.has(n));

// --- the selection (captain 23:47): right-click selects the thing under the pointer and highlights it at once -------
// One selection at a time. It goes on: Esc, a left-click on empty paper, 'Clear selection' (bottom of the menu), the ×
// on its label, 'Clear marks', a new answer, and any sheet / floor / page / Plan-3D change (checked every frame).
type SelState = { sel: Selection; file: string; page: number; view: ViewMode; g: SVGGElement; tag: HTMLElement; raf: number; z: number;
  /** which top corner the label hangs from: away from the menu (UX N-1: the menu covered the label's ×) */
  align: "center" | "left" | "right"; below: boolean;
  /** the open menu's rectangle: the label moves aside so the menu never covers it or its × (UX N-1) */
  avoid: { x: number; y: number; w: number; h: number } | null };
let cur: SelState | null = null;
let finding: HTMLElement | null = null;
const SVGNS = "http://www.w3.org/2000/svg";
/** The selection on the drawing now (null: none). */
export const currentSelection = (): Selection | null => cur?.sel ?? null;
export const hasSelection = () => !!cur;
function stopFinding() {
  finding?.remove();
  finding = null;
}
export function clearSelection() {
  stopFinding();
  if (!cur) return;
  cancelAnimationFrame(cur.raf);
  cur.g.remove();
  cur.tag.remove();
  cur = null;
  document.getElementById("v2d")?.classList.remove("has-selection");
  window.dispatchEvent(new CustomEvent("stoa:selection", { detail: null }));
}
/** captain 01:18 'Start over' (window 'stoa:reset'): the shell's part here: the right-click menu, the selection and the
 *  open feature panel; every plug-in clears itself on the same event. */
window.addEventListener("stoa:reset", () => {
  closeMenu();
  clearSelection();
  openPanel?.close();
});
const boxOf = (s: Selection): [number, number, number, number] | null => {
  if (s.bbox) return s.bbox;
  const p = s.poly ?? [];
  if (!p.length) return null;
  const xs = p.map((q) => q[0]), ys = p.map((q) => q[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};
function drawSelection(sel: Selection | null) {
  clearSelection();
  if (sel && sel.poly && !sel.polygon) sel.polygon = sel.poly;
  const h0 = host;
  const parent = h0?.overlayLayer();
  if (!h0 || !sel || !parent) return;
  const g = document.createElementNS(SVGNS, "g");
  g.classList.add("feat-sel");
  g.dataset.type = sel.type;
  parent.append(g);
  const tag = document.createElement("div");
  tag.className = "feat-sel-tag";
  tag.dataset.type = sel.type;
  tag.setAttribute("role", "status");
  const txt = document.createElement("span");
  txt.textContent = sel.label.length > 60 ? sel.label.slice(0, 59) + "…" : sel.label;
  const x = document.createElement("button");
  x.type = "button";
  x.textContent = "×";
  x.title = "Clear selection (Esc)";
  x.setAttribute("aria-label", "Clear selection");
  x.addEventListener("click", (e) => {
    e.stopPropagation();
    closeMenu();
    clearSelection();
  });
  tag.addEventListener("pointerdown", (e) => e.stopPropagation());
  tag.addEventListener("contextmenu", (e) => e.preventDefault());
  tag.append(txt, x);
  document.body.append(tag);
  const st: SelState = { sel, file: h0.file()?.path ?? "", page: h0.page(), view: h0.view(), g, tag, raf: 0, z: 0, align: "center", below: false, avoid: null };
  cur = st;
  document.getElementById("v2d")?.classList.add("has-selection");
  const paint = (z: number) => {
    g.replaceChildren();
    const shape = (cls: string, w: number) => {
      let el: SVGElement | null = null;
      if (sel.poly && sel.poly.length >= 3) {
        el = document.createElementNS(SVGNS, "polygon");
        el.setAttribute("points", sel.poly.map((q) => h0.toContent(q).join(",")).join(" "));
      } else {
        const b = boxOf(sel);
        if (!b) return;
        const [x0, y0] = h0.toContent([b[0], b[1]]);
        const [x1, y1] = h0.toContent([b[2], b[3]]);
        const padC = 3 / z; // a few screen px around a small symbol, so the outline does not hide it
        el = document.createElementNS(SVGNS, "rect");
        el.setAttribute("x", String(Math.min(x0, x1) - padC));
        el.setAttribute("y", String(Math.min(y0, y1) - padC));
        el.setAttribute("width", String(Math.abs(x1 - x0) + 2 * padC));
        el.setAttribute("height", String(Math.abs(y1 - y0) + 2 * padC));
        el.setAttribute("rx", String(4 / z));
      }
      el.classList.add(cls);
      el.setAttribute("stroke-width", String(w / z));
      g.append(el);
    };
    shape("feat-sel-halo", 10);
    shape("feat-sel-under", 4);
    shape("feat-sel-line", 2.5);
    // UX S-B3: distinct from the answer's solid marks — the selection's outline is dashed ('marching' unless reduced motion)
    const line = g.querySelector<SVGElement>(".feat-sel-line");
    line?.setAttribute("stroke-dasharray", `${7 / z} ${4 / z}`);
  };
  const tick = () => {
    if (cur !== st) return;
    const h1 = host!;
    if ((h1.file()?.path ?? "") !== st.file || h1.page() !== st.page || h1.view() !== st.view || !g.isConnected) {
      closeMenu();
      return clearSelection();
    }
    const z = h1.zoom() || 1;
    if (z !== st.z || !g.childElementCount) { // zoomed, or the layer was emptied by a redraw of the same sheet
      st.z = z;
      paint(z);
    }
    const b = boxOf(sel);
    const a = b ? h1.toClient([b[0], b[1]]) : null, c = b ? h1.toClient([b[2], b[3]]) : null;
    const p = a && c ? { x: st.align === "left" ? Math.min(a.x, c.x) : st.align === "right" ? Math.max(a.x, c.x) : (a.x + c.x) / 2, y: st.below ? Math.max(a.y, c.y) : Math.min(a.y, c.y) } : null;
    const r = document.getElementById("v2d")?.getBoundingClientRect();
    const show = !!p && !!r && p.x >= r.left - 40 && p.x <= r.right + 40 && p.y >= r.top - 4 && p.y <= r.bottom;
    tag.hidden = !show;
    if (show) {
      // explicit corner: x0 / y0 of the label, moved aside from an open menu
      const w = tag.offsetWidth, h = tag.offsetHeight;
      let x0 = st.align === "left" ? p!.x : st.align === "right" ? p!.x - w : p!.x - w / 2;
      let y0 = st.below ? p!.y + 8 : Math.max(p!.y, r!.top + 36 + h + 8) - 8 - h;
      const m = st.avoid;
      const hits = (x: number, y: number) => !!m && x + w > m.x && x < m.x + m.w && y + h > m.y && y < m.y + m.h;
      if (m && hits(x0, y0)) {
        const left = m.x - 8 - w, right = m.x + m.w + 8;
        if (left >= 8 && (x0 + w / 2 <= m.x + m.w / 2 || right + w > window.innerWidth - 8)) x0 = left;
        else if (right + w <= window.innerWidth - 8) x0 = right;
        else y0 = Math.max(8, m.y - 8 - h);
      }
      // UX NEW-R4: the label stays over the drawing (it sat over the left panel); kept clear of the menu by moving up
      const cx = Math.min(Math.max(x0, r!.left + 4), Math.max(r!.left + 4, r!.right - w - 4));
      if (cx !== x0) {
        x0 = cx;
        if (hits(x0, y0)) y0 = Math.max(r!.top + 4, m!.y - 8 - h);
      }
      tag.style.transform = "none";
      tag.style.left = `${Math.round(x0)}px`;
      tag.style.top = `${Math.round(y0)}px`;
    }
    st.raf = requestAnimationFrame(tick);
  };
  tick();
  window.dispatchEvent(new CustomEvent("stoa:selection", { detail: sel }));
}
/** 'Finding what's here…' at the pointer while the room is being traced (no cached outline there). */
function showFinding(x: number, y: number) {
  stopFinding();
  const t = document.createElement("div");
  t.className = "feat-sel-tag finding";
  t.setAttribute("role", "status");
  t.textContent = "Finding what's here…";
  t.style.left = `${Math.round(x)}px`;
  t.style.top = `${Math.round(y - 10)}px`;
  document.body.append(t);
  finding = t;
}
/** ctx.room follows the selection: a room's name, else none (the menu is about the door / tag / spot selected). */
function withSelection(ctx: PointerCtx, sel: Selection | null): PointerCtx {
  if (sel && sel.poly && !sel.polygon) sel.polygon = sel.poly;
  const room = sel?.type === "room" && sel.name ? { name: sel.name, at: sel.at ?? ctx.point ?? [0, 0] as DrawingPt } : null;
  return { ...ctx, selection: sel, room };
}

// --- the right-click / long-press menu (items from every enabled feature) ------------------------------------------
let menuEl: HTMLElement | null = null;
function closeMenu() {
  if (menuEl?.dataset.kind === "more") {
    lastMoreClose = performance.now();
    document.querySelector(".feat-more")?.setAttribute("aria-expanded", "false");
  }
  menuEl?.remove();
  menuEl = null;
  if (cur) cur.avoid = null;
}
function itemsFor(ctx: PointerCtx) {
  return menuItems
    .filter((it) => !failed.has(it.feature) && guard(it.feature, () => it.when(ctx), false)())
    .sort((a, b) => a.order - b.order);
}
/** Items shown before 'More…' (QA 22:09 / S0 22:30: a fixed cap of 5 hid later plug-ins' items inside a named room). */
const MENU_FIRST = 5;
/** captain 23:47: a selected room's menu is his list (Area · Skirting · Doors · Windows · Perimeter · Edit outline / Trace
 *  it yourself): up to 7 rows before 'More…' when something is selected. */
const MENU_SELECTED = 7;
/** Items a plug-in registered under this id (e.g. F1's 'trace-yourself'), enabled and not failed: another plug-in's
 *  item can then step aside instead of showing the same action twice. */
export const menuHas = (id: string, ctx?: PointerCtx) =>
  menuItems.some((it) => it.id === id && !failed.has(it.feature) && enabled(it.feature) && (!ctx || guard(it.feature, () => it.when(ctx), false)()));
function showMenu(ctx: PointerCtx, all = false, pending = false): boolean {
  closeMenu();
  // while the room is being traced: only 'Finding what's here…' (UX S-M5: the items must not change under the pointer)
  const every = pending ? [] : itemsFor(ctx);
  const cap = ctx.selection ? MENU_SELECTED : MENU_FIRST;
  // one 'More…' row instead of a 6th / 7th item; it opens the full list in place
  const found = all || every.length <= cap + 1 ? every : [...every.slice(0, cap),
    { feature: "", id: "more", order: 999, when: () => true, run: () => void showMenu(ctx, true), label: `More… (${every.length - cap})` } as Item];
  // captain 21:02: never an empty menu — say that nothing was recognised under the pointer
  const items: Item[] = found.length || pending ? found : [{ feature: "", id: "nothing", order: 99, when: () => true, run: () => undefined,
    label: ctx.point ? "Nothing recognised here: try a room name, a tag or a note" : "Right-click on the drawing to ask about a spot" }];
  const m = document.createElement("div");
  m.className = "feat-menu";
  m.setAttribute("role", "menu");
  const sel = ctx.selection;
  const TYPE_WORD: Partial<Record<SelectionType, string>> = { room: "Room", tag: "Tag", symbol: "Symbol" };
  const typed = sel ? (TYPE_WORD[sel.type] && !sel.label.toLowerCase().startsWith(TYPE_WORD[sel.type]!.toLowerCase()) ? `${TYPE_WORD[sel.type]} · ${sel.label}` : sel.label) : "";
  const head = pending ? "Finding what's here…" : sel ? typed : ctx.selection === null ? "" : ctx.room?.name || (ctx.label && ctx.label.dist < 40 ? ctx.label.text : "");
  if (head) {
    const t = document.createElement("div");
    t.className = "feat-menu-head";
    if (pending) t.classList.add("pending");
    t.textContent = head.length > 48 ? head.slice(0, 47) + "…" : head;
    m.append(t);
  }
  for (const it of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.textContent = guard(it.feature, () => (typeof it.label === "function" ? it.label(ctx) : it.label), it.id)();
    if (it.id === "nothing" && !it.feature) {
      b.disabled = true;
      b.classList.add("feat-menu-none");
    }
    if (it.id === "more" && !it.feature) b.classList.add("feat-menu-more");
    b.addEventListener("click", () => {
      if (it.id === "more" && !it.feature) return void showMenu(ctx, true, pending);
      closeMenu();
      guard(it.feature, () => it.run(ctx), undefined)();
    });
    m.append(b);
  }
  if (cur || sel) { // captain 23:47: 'Clear selection' at the bottom of every menu with a selection
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.className = "feat-menu-clear";
    b.textContent = "Clear selection";
    b.addEventListener("click", () => {
      closeMenu();
      clearSelection();
    });
    m.append(b);
  }
  document.body.append(m);
  menuEl = m;
  const r = m.getBoundingClientRect();
  let px = ctx.client.x + 12, py = ctx.client.y;
  // UX S-B3: beside the selection, not over it (small things: a door, a tag, a room that fits on screen)
  const b = sel && host ? boxOf(sel) : null;
  let box: { L: number; R: number; T: number } | null = null;
  if (b && host) {
    const a = host.toClient([b[0], b[1]]), c = host.toClient([b[2], b[3]]);
    if (a && c) {
      const L = Math.min(a.x, c.x), R = Math.max(a.x, c.x), T = Math.min(a.y, c.y);
      box = { L, R, T };
      if (R - L < window.innerWidth * 0.4) {
        if (R + 12 + r.width <= window.innerWidth - 8) px = R + 12;
        else if (L - 12 - r.width >= 8) px = L - 12 - r.width;
        py = Math.max(T, ctx.client.y - 40);
      }
      // the selection's label hangs from the corner AWAY from the menu (UX N-1)
      if (cur && cur.sel === sel && px >= (L + R) / 2) cur.align = "left";
      else if (cur && cur.sel === sel) cur.align = "right";
    }
  }
  const x = Math.max(8, Math.min(px, window.innerWidth - r.width - 8));
  const y = Math.max(8, Math.min(py, window.innerHeight - r.height - 8));
  m.style.left = `${x}px`;
  m.style.top = `${y}px`;
  // the label steps aside from the menu (UX N-1: the menu covered the label and its ×)
  if (cur && (cur.sel === sel || !sel)) cur.avoid = { x, y, w: r.width, h: r.height };
  void box;
  // UX N-2: no item is pre-selected (Enter must not teach 'This is a door'): the menu takes focus; arrows / Tab move
  m.tabIndex = -1;
  m.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const bs = [...m.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
    if (!bs.length) return;
    e.preventDefault();
    const i = bs.indexOf(document.activeElement as HTMLButtonElement);
    bs[(i + (e.key === "ArrowDown" ? 1 : -1) + bs.length + (i < 0 && e.key === "ArrowUp" ? 1 : 0)) % bs.length].focus();
  });
  m.focus({ preventScroll: true });
  return true;
}
/** UX S-M3: while Measure is drawing, a right-click belongs to the tool: its own controls (Undo point / Done / Clear
 *  selection), never the room under the shape (whose Esc would also discard the corners). */
function showToolMenu(x: number, y: number, panel: HTMLElement): boolean {
  closeMenu();
  const ctl = [...panel.querySelectorAll<HTMLButtonElement>(".m-ctl button")].filter((b) => !b.disabled && (/undo|done|clear selection/i.test(b.textContent ?? "") || b.dataset.act === "cancel")); // F1 01:05: 'Cancel shape' carries data-act="cancel"
  const m = document.createElement("div");
  m.className = "feat-menu";
  m.dataset.kind = "tool";
  m.setAttribute("role", "menu");
  const t = document.createElement("div");
  t.className = "feat-menu-head";
  t.textContent = "Measuring";
  m.append(t);
  for (const src of ctl) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.textContent = src.dataset.act === "cancel" ? "Discard this shape" : (src.textContent ?? "").replace(/^Clear selection$/, "Discard this shape");
    b.addEventListener("click", () => {
      closeMenu();
      src.click();
    });
    m.append(b);
  }
  const keep = document.createElement("button");
  keep.type = "button";
  keep.setAttribute("role", "menuitem");
  keep.className = "feat-menu-clear";
  keep.textContent = "Keep measuring";
  keep.addEventListener("click", closeMenu);
  m.append(keep);
  document.body.append(m);
  menuEl = m;
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(x + 12, window.innerWidth - r.width - 8))}px`;
  m.style.top = `${Math.max(8, Math.min(y, window.innerHeight - r.height - 8))}px`;
  m.tabIndex = -1;
  m.focus({ preventScroll: true });
  return true;
}
let installed = false;
function installContextMenu() {
  if (installed) return;
  installed = true;
  const target = () => document.getElementById("v2d");
  const open = (x: number, y: number) => {
    if (!host || !menuItems.length) return false;
    // a tool that is drawing owns the right-click (UX S-M3 / S-M4): Measure gets its own short menu; a region being
    // dragged gets none (and no browser menu either)
    const meas = document.querySelector<HTMLElement>(".measure.on");
    if (meas && meas.querySelector(".m-ctl button")) return showToolMenu(x, y, meas);
    if (document.querySelector(".feat-region-capture")) return true;
    const ctx0 = host.pointerCtx(x, y);
    if (host.selectAt && ctx0.point && ctx0.view !== "3d") {
      // captain 23:47: select the thing under the pointer at once (cached geometry), highlight it, menu by its type;
      // when no cached outline holds the point, 'Finding what's here…' until the room is traced from its label
      let pick: ReturnType<NonNullable<Host["selectAt"]>> = { now: null };
      try { pick = host.selectAt(ctx0); } catch (e) { console.warn("[features] selectAt failed", e); }
      const ctx = withSelection(ctx0, pick.now);
      drawSelection(pick.now);
      const pending = !pick.now && !!pick.later;
      if (pending) showFinding(x, y);
      if (!showMenu(ctx, false, pending)) return false;
      const shown = menuEl;
      if (pick.later) {
        void pick.later.then((s) => {
          stopFinding();
          if (menuEl !== shown) {
            if (s && !menuEl && !cur && host?.file()?.path === ctx0.file?.path) drawSelection(s); // menu closed meanwhile: still show it
            return;
          }
          const sel = s === undefined ? pick.now : s;
          if (sel !== pick.now) drawSelection(sel);
          showMenu(withSelection(ctx0, sel ?? null));
        }).catch(() => {
          stopFinding();
          if (menuEl === shown) showMenu(ctx);
        });
      }
      return true;
    }
    if (!showMenu(ctx0)) return false;
    // the nearest printed name can be the room next door: once the room under the pointer is traced, the menu names
    // the room whose label lies inside that outline (same place, items rebuilt)
    const shown = menuEl;
    void host.refineRoom?.(ctx0).then((room) => {
      if (room === undefined || menuEl !== shown || (room?.name ?? "") === (ctx0.room?.name ?? "")) return;
      showMenu({ ...ctx0, room });
    }).catch(() => undefined);
    return true;
  };
  document.addEventListener("contextmenu", (e) => {
    const t = target();
    if (!t || !t.contains(e.target as Node)) return;
    if (open(e.clientX, e.clientY)) e.preventDefault();
  });
  // long-press on touch: 550 ms without moving more than 8 px
  let lp: { id: number; x: number; y: number; timer: number } | null = null;
  document.addEventListener("pointerdown", (e) => {
    if (menuEl && !menuEl.contains(e.target as Node)) closeMenu();
    const t = target();
    if (e.pointerType !== "touch" || !t || !t.contains(e.target as Node)) return;
    lp = { id: e.pointerId, x: e.clientX, y: e.clientY, timer: window.setTimeout(() => { if (lp) open(lp.x, lp.y); lp = null; }, 550) };
  }, true);
  const cancel = (e: PointerEvent) => {
    if (!lp || e.pointerId !== lp.id) return;
    if (e.type === "pointermove" && Math.hypot(e.clientX - lp.x, e.clientY - lp.y) < 8) return;
    clearTimeout(lp.timer);
    lp = null;
  };
  document.addEventListener("pointermove", cancel, true);
  document.addEventListener("pointerup", cancel, true);
  document.addEventListener("pointercancel", cancel, true);
  // Esc: the open menu / the selection first, and ONLY that (UX S-M3: the Esc that closed the menu also discarded
  // Measure's corners): window capture runs before every document listener (main.ts, measure.ts)
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || (!menuEl && !cur)) return;
    closeMenu();
    clearSelection();
    e.preventDefault();
    e.stopPropagation();
  }, true);
  // a left-click on the paper (no drag) clears the selection; its label's × and the menu stop their own clicks
  let down: { x: number; y: number } | null = null;
  document.addEventListener("pointerdown", (e) => {
    const t = target();
    down = cur && e.button === 0 && t && t.contains(e.target as Node) ? { x: e.clientX, y: e.clientY } : null;
  }, true);
  document.addEventListener("pointerup", (e) => {
    // (not the end of a long-press: that one has just opened the menu)
    if (down && !menuEl && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 6) clearSelection();
    down = null;
  }, true);
  window.addEventListener("stoa:clear-marks", () => clearSelection());
  // F1 00:36: Measure took the selected room (Edit outline) or its Esc / Clear selection: drop the highlight only
  window.addEventListener("stoa:deselect", () => clearSelection());
  window.addEventListener("blur", closeMenu);
  document.addEventListener("wheel", closeMenu, { passive: true });
}
