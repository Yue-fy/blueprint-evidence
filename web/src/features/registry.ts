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
  /** A side panel (floating sheet on the right of the viewer); one panel is open at a time. */
  addPanel(o: { id: string; title: string; render: (el: HTMLElement) => void; onClose?: () => void }): { el: HTMLElement; open(): void; close(): void; toggle(): void; isOpen(): boolean };
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
export function enabled(name: string): boolean {
  if (flags.has("none") || flags.has("-" + name.toLowerCase())) return false;
  return flags.has("all") || flags.has("*") || flags.has(name.toLowerCase()) || DEFAULT_ON.has(name.toLowerCase());
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
const beforeAskFns: { feature: string; fn: (q: string, f: FileInfo) => Promise<Answer> | null }[] = [];
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

let openPanel: { id: string; close(): void } | null = null;
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
      b.addEventListener("click", guard(name, () => o.onClick(), undefined));
      bar.append(b);
      bar.hidden = false;
      const sync = guard(name, () => void (b.hidden = !!o.when && !o.when()), undefined);
      sync();
      const offA = apiFor(name).onDrawing(sync);
      return onClean(name, () => {
        offA();
        b.remove();
        bar.hidden = !bar.querySelector(".feat-btn");
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
      el.innerHTML = `<header><h3></h3><button type="button" class="feat-x" aria-label="Close">✕</button></header><div class="feat-body"></div>`;
      el.querySelector("h3")!.textContent = o.title;
      const body = el.querySelector<HTMLElement>(".feat-body")!;
      root?.append(el);
      const p = {
        el: body,
        open: () => {
          if (openPanel && openPanel.id !== `${name}/${o.id}`) openPanel.close();
          guard(name, () => o.render(body), undefined)();
          el.hidden = false;
          if (root) root.hidden = false;
          openPanel = { id: `${name}/${o.id}`, close: p.close };
        },
        close: () => {
          el.hidden = true;
          if (root) root.hidden = !root.querySelector(".feat-panel:not([hidden])");
          if (openPanel?.id === `${name}/${o.id}`) openPanel = null;
          if (o.onClose) guard(name, o.onClose, undefined)();
        },
        toggle: () => (el.hidden ? p.open() : p.close()),
        isOpen: () => !el.hidden,
      };
      el.querySelector(".feat-x")!.addEventListener("click", () => p.close());
      onClean(name, () => {
        el.remove();
        if (root) root.hidden = !root.querySelector(".feat-panel:not([hidden])");
      });
      return p;
    },
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
}
/** main.ts: after an answer is shown. */
export function emitAnswer(a: Answer, q: string) {
  for (const e of [...answerCbs]) guard(e.feature, () => e.cb(a, q), undefined)();
}
/** main.ts: after a drawing (or another page of it) has opened. */
export function emitDrawing(f: FileInfo | null, page: number) {
  for (const e of [...drawingCbs]) guard(e.feature, () => e.cb(f, page), undefined)();
}
/** api.ts: the solver_args every enabled feature adds to a question ({} when none). */
export function extraAskArgs(path: string, q: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const e of askArgFns) Object.assign(out, guard(e.feature, () => e.fn(path, q), null)() ?? {});
  return out;
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
export const anyFeatureOn = () => [...features.keys()].some((n) => enabled(n) && !failed.has(n));

// --- the right-click / long-press menu (items from every enabled feature) ------------------------------------------
let menuEl: HTMLElement | null = null;
function closeMenu() {
  menuEl?.remove();
  menuEl = null;
}
function itemsFor(ctx: PointerCtx) {
  return menuItems
    .filter((it) => !failed.has(it.feature) && guard(it.feature, () => it.when(ctx), false)())
    .sort((a, b) => a.order - b.order)
    .slice(0, 5);
}
function showMenu(ctx: PointerCtx): boolean {
  closeMenu();
  const found = itemsFor(ctx);
  // captain 21:02: never an empty menu — say that nothing was recognised under the pointer
  const items: Item[] = found.length ? found : [{ feature: "", id: "nothing", order: 99, when: () => true, run: () => undefined,
    label: ctx.point ? "Nothing recognised here: try a room name, a tag or a note" : "Right-click on the drawing to ask about a spot" }];
  const m = document.createElement("div");
  m.className = "feat-menu";
  m.setAttribute("role", "menu");
  const head = ctx.room?.name || (ctx.label && ctx.label.dist < 40 ? ctx.label.text : "");
  if (head) {
    const t = document.createElement("div");
    t.className = "feat-menu-head";
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
    b.addEventListener("click", () => {
      closeMenu();
      guard(it.feature, () => it.run(ctx), undefined)();
    });
    m.append(b);
  }
  document.body.append(m);
  menuEl = m;
  const r = m.getBoundingClientRect();
  const x = Math.min(ctx.client.x, window.innerWidth - r.width - 8);
  const y = Math.min(ctx.client.y, window.innerHeight - r.height - 8);
  m.style.left = `${Math.max(8, x)}px`;
  m.style.top = `${Math.max(8, y)}px`;
  (m.querySelector("button") as HTMLButtonElement | null)?.focus();
  return true;
}
let installed = false;
function installContextMenu() {
  if (installed) return;
  installed = true;
  const target = () => document.getElementById("v2d");
  const open = (x: number, y: number) => {
    if (!host || !menuItems.length) return false;
    const ctx = host.pointerCtx(x, y);
    if (!showMenu(ctx)) return false;
    // the nearest printed name can be the room next door: once the room under the pointer is traced, the menu names
    // the room whose label lies inside that outline (same place, items rebuilt)
    const shown = menuEl;
    void host.refineRoom?.(ctx).then((room) => {
      if (room === undefined || menuEl !== shown || (room?.name ?? "") === (ctx.room?.name ?? "")) return;
      showMenu({ ...ctx, room });
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
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  window.addEventListener("blur", closeMenu);
  document.addEventListener("wheel", closeMenu, { passive: true });
}
